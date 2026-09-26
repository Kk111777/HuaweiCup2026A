"""Offline official replay and optional historical comparison; never a solver input."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
import hashlib
from pathlib import Path
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from q1 import official
from q1.plan import normalized
from q1.unified.api import source_signature
from q1.unified.io import read,write,digest


def official_fingerprint():
    return hashlib.sha256('\n'.join(f'{p.name}:{digest(p)}' for p in sorted(official.CODE.glob('*.py'))).encode()).hexdigest()


def replay_one(folder):
    req=read(folder/'request.json');stamp=official_fingerprint()
    if req.get('problem_id', 'q1') != 'q1':
        raise ValueError('audit_unified only replays Q1; select the corresponding problem evaluator')
    assert digest(req['graph'])==req['graph_sha256']
    assert digest(req['config'])==req['config_sha256']
    ph,rh=digest(folder/'plan.json'),digest(folder/'evaluation.json')
    saved=read(folder/'evaluation.json')
    approximate=saved.get('evaluation_source')=='approximate'
    if (folder/'verified.json').exists():
        m=read(folder/'verified.json')
        if (m.get('independent_official_replay') is True and m.get('plan_sha256')==ph
            and m.get('source_signature')==req['source_signature']
            and (not approximate or m.get('official_replay_sha256'))
            and m.get('result_sha256')==rh and m.get('official_fingerprint')==stamp
            and m.get('graph_sha256')==req['graph_sha256'] and m.get('config_sha256')==req['config_sha256']
            and (not m.get('official_replay_sha256') or ((folder/'official_replay.json').exists()
                 and digest(folder/'official_replay.json')==m['official_replay_sha256']))):
            return str(folder),m['replay_seconds']
    started=time.perf_counter()
    actual=official.evaluate(read(req['graph']),read(folder/'plan.json'),official.Settings.read(req['config']))
    if not approximate:
        assert normalized(actual)==normalized(saved),(str(folder),'official replay mismatch')
    # Realized score is an audit artifact; preserve the solver's original score
    # and timeline so an approximate output is never relabelled as measured.
    write(folder/'official_replay.json',actual)
    elapsed=time.perf_counter()-started
    marker=dict(plan_sha256=ph,result_sha256=rh,replay_seconds=elapsed,
        source_signature=req['source_signature'],official_fingerprint=stamp,
        graph_sha256=req['graph_sha256'],config_sha256=req['config_sha256'],independent_official_replay=True)
    if 'problem_id' in req:
        marker.update(problem_id=req['problem_id'],backend_version=req.get('backend_version'))
    marker.update(official_replay_sha256=digest(folder/'official_replay.json'),
        solver_score_source='approximate' if approximate else 'official',
        solver_makespan=saved['makespan'],official_makespan=actual['makespan'],
        prediction_error_fraction=saved['makespan']/actual['makespan']-1 if actual['makespan'] else 0,
        exact_result_match=not approximate)
    write(folder/'verified.json',marker)
    return str(folder),elapsed


def mean(values):
    values=[v for v in values if v is not None]
    return statistics.mean(values) if values else None


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--personal-reference',type=Path)
    p.add_argument('--base-speedups',type=Path)
    p.add_argument('--replay',action='store_true')
    p.add_argument('--local-test-workers',type=int,default=1)
    p.add_argument('--require-current-source',action='store_true')
    a=p.parse_args();base={};rows=[];durations={};replay_errors={}
    if not 1<=a.local_test_workers<=24:p.error('workers must be 1..24')
    if a.base_speedups:
        with a.base_speedups.open(encoding='utf-8-sig',newline='') as s:
            base={(r['case'],int(r['num_cores'])):int(r['makespan_cycles']) for r in csv.DictReader(s)}
    files=sorted(a.run.glob('case_*/N*/summary.json'))
    folders=[f.parent for f in files if (f.parent/'evaluation.json').exists()]
    if a.require_current_source:
        sig=source_signature()
        assert all(read(f)['source_signature']==sig for f in files),'mixed/old solver source in audited run'
    if a.replay:
        with ProcessPoolExecutor(max_workers=a.local_test_workers) as pool:
            jobs={pool.submit(replay_one,folder):folder for folder in folders}
            for future in as_completed(jobs):
                try:
                    folder,seconds=future.result();durations[folder]=seconds
                except Exception as error:
                    replay_errors[str(jobs[future])]=dict(type=type(error).__name__,error=str(error))
                    write(a.run/'replay_errors.json',replay_errors)
                    continue
                if len(durations)%20==0 or len(durations)==len(folders):
                    print(f'official replay {len(durations)}/{len(folders)} passed',flush=True)
                write(a.run/'replay_progress.json',dict(verified=len(durations),expected=len(folders)))
    for file in files:
        folder=file.parent;case=folder.parent.name;cores=int(folder.name[1:]);result=read(file)
        if not (folder/'evaluation.json').exists():
            rows.append(dict(case=case,cores=cores,status=result['status'],wall_seconds=result['wall_seconds'],
                accounted_wall_seconds=result['accounted_wall_seconds'],cpu_seconds=result.get('cpu_seconds'),
                hard_timeout=result['hard_timeout'],exceeded_target=result['exceeded_target'],
                timed_out_phase=result.get('timed_out_phase'),source_signature=result['source_signature']))
            continue
        saved=read(folder/'evaluation.json');ev=saved;original=None
        source=saved.get('evaluation_source','official')
        if (folder/'verified.json').exists():
            m=read(folder/'verified.json')
            if (m.get('official_replay_sha256') and m.get('plan_sha256')==digest(folder/'plan.json')
                and m.get('result_sha256')==digest(folder/'evaluation.json')
                and digest(folder/'official_replay.json')==m['official_replay_sha256']):
                ev=read(folder/'official_replay.json');source='official_replay'
        if str(folder) in replay_errors:source='replay_failed'
        cycles=ev['makespan'] if source not in ('approximate','replay_failed') else None
        if a.personal_reference:
            old=read(a.personal_reference/case/f'N{cores}'/'metrics.json')
            original=old['personal_makespan_cycles']
        bc=base.get((case,cores))
        rows.append(dict(case=case,cores=cores,status=result['status'],cycles=cycles,
            solver_score_source=saved.get('evaluation_source','official'),score_source=source,
            solver_makespan=saved['makespan'],
            replay_error=replay_errors.get(str(folder)),
            base_cycles=bc,personal_cycles=original,
            change_vs_personal=cycles/original-1 if cycles and original else None,
            change_vs_base=cycles/bc-1 if cycles and bc else None,
            wall_seconds=result['wall_seconds'],accounted_wall_seconds=result['accounted_wall_seconds'],
            cpu_seconds=result.get('cpu_seconds'),exceeded_target=result['exceeded_target'],
            extended=result['extended'],exceeded_extension=result['exceeded_extension'],
            hard_timeout=result['hard_timeout'],speedup=(result.get('baseline',{}).get('cycles')/cycles
                if cycles and result.get('baseline',{}).get('cycles') else None),
            replay_seconds=durations.get(str(folder)),source_signature=result['source_signature']))
    eligible=[r for r in rows if r.get('cycles') is not None];stats={}
    for n in sorted({r['cores'] for r in eligible}):
        rr=[r for r in eligible if r['cores']==n]
        ratios=[r['change_vs_personal'] for r in rr if r['change_vs_personal'] is not None]
        stats[n]=dict(count=len(rr),mean_speedup=mean(r['speedup'] for r in rr if r['speedup'] is not None),
            mean_change_vs_personal=mean(ratios),worst_change_vs_personal=max(ratios) if ratios else None,
            personal_regressions_above_one_percent=sum(v>.01 for v in ratios),
            base_regressions_above_one_percent=sum(r['change_vs_base']>.01 for r in rr if r['change_vs_base'] is not None),
            mean_wall_seconds=mean(r['wall_seconds'] for r in rr),
            maximum_wall_seconds=max(r['wall_seconds'] for r in rr),
            exceeded_target=sum(r['exceeded_target'] for r in rr),hard_timeouts=sum(r['hard_timeout'] for r in rr))
    write(a.run/'comparison.json',dict(stats=stats,rows=rows,independent_replay=a.replay and not replay_errors,
        replay_requested=a.replay,replay_errors=replay_errors,
        source_signatures=sorted({r.get('source_signature') for r in eligible}),
        note='Independent official replay; an optional baseline is a reporting denominator, never a search input.'))
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with (a.run/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as s:
        w=csv.DictWriter(s,fieldnames=fields);w.writeheader();w.writerows(rows)
    print(stats)
    return bool(replay_errors)


if __name__=='__main__':raise SystemExit(main())
