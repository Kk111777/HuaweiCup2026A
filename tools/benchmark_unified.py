"""Local case-parallel benchmark; each case shares one serial all-core budget."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import shutil
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from common.baseline import load_baseline_catalog
from q1 import official
from q1.unified.api import solve_case, source_signature, default_parameters
from q1.unified.io import read, write, digest
from scheduling.registry import resolve_problem
from problem_1_baseline import output_lock


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,required=True)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--cases',type=int,nargs='+',default=list(range(1,101)))
    p.add_argument('--cores',type=int,nargs='+',choices=[2,3,4,5],default=[2,3,4,5])
    p.add_argument('--local-test-workers',type=int,default=1)
    p.add_argument('--report-baseline-archive','--local-test-baseline-archive',
        dest='report_baseline_archive',type=Path,
        help='Validated singlecore archive used only after solving to report speedups; never solver input')
    p.add_argument('--archive-repo-root',type=Path)
    p.add_argument('--seconds',type=float,default=480,
        help='Target wall-clock budget for ONE CASE and ALL requested core counts combined')
    p.add_argument('--resume',action='store_true')
    p.add_argument('--reuse-verified-run',type=Path,nargs='*',default=[])
    a=p.parse_args()
    if not 1<=a.local_test_workers<=24:p.error('workers must be 1..24')
    if len(set(a.cases))!=len(a.cases):p.error('duplicate cases')
    if len(set(a.cores))!=len(a.cores):p.error('duplicate core counts')
    a.output.mkdir(parents=True,exist_ok=True)
    with output_lock(a.output):
        return run(a)


def run(a):
    paths=[(a.data/f'case_{i:03d}.json').resolve() for i in a.cases]
    # Local batch scheduling only: start large independent inputs early to
    # reduce the tail. This does not affect a solver's candidate decisions.
    paths.sort(key=lambda path:(-path.stat().st_size,path.name))
    a.output.mkdir(parents=True,exist_ok=True)
    signature=source_signature()
    identity=resolve_problem('q1').metadata()
    baseline={}
    if a.report_baseline_archive:
        archive=a.report_baseline_archive.resolve()
        catalog=load_baseline_catalog(archive,paths,a.config,official.CODE,repo_root=a.archive_repo_root or archive.parent.parent)
        for path in paths:
            entry=catalog[path.stem]
            baseline[path.stem]=dict(mode='reporting_only',cycles=entry['makespan_cycles'],
                reference=entry['baseline_ref'],included_in_solver=False)
    started=time.perf_counter()
    manifest=dict(source_signature=signature,**identity,cases=a.cases,cores=a.cores,
        local_test_workers=a.local_test_workers,within_case_parallel=False,
        job_order=[path.stem for path in paths],job_order_policy='largest_input_first',
        test_baseline_cache=False,report_baseline_archive=bool(baseline),target_seconds=a.seconds,
        budget_scope='case_all_cores',singlecore_in_solver=False,
        note='All core counts share the case budget. Singlecore results are optional reporting-only denominators; no historical time is charged.')
    write(a.output/'benchmark.json',manifest)
    def reusable(folder,path):
        required=('summary.json','request.json','verified.json','plan.json','evaluation.json')
        if not all((folder/name).is_file() for name in required):return False
        result=read(folder/'summary.json');req=read(folder/'request.json');marker=read(folder/'verified.json')
        return (result['status']=='ok' and result['source_signature']==signature
            and all(req.get(k)==v and result.get(k)==v for k,v in identity.items())
            and req.get('source_signature')==signature
            and req['graph_sha256']==digest(path) and req['config_sha256']==digest(a.config)
            and not req.get('test_baseline')
            and marker.get('independent_official_replay') is True
            and marker.get('source_signature')==signature
            and (result.get('score_source')!='approximate' or marker.get('official_replay_sha256'))
            and (not marker.get('official_replay_sha256') or ((folder/'official_replay.json').exists()
                 and marker['official_replay_sha256']==digest(folder/'official_replay.json')))
            and marker.get('plan_sha256')==digest(folder/'plan.json')
            and marker.get('result_sha256')==digest(folder/'evaluation.json'))
    def case_contract(path):
        return dict(source_signature=signature,**identity,graph_sha256=digest(path),
            config_sha256=digest(a.config),cores=a.cores,seconds=a.seconds,
            parameters=default_parameters(),budget_scope='case_all_cores',singlecore_in_solver=False)
    def reusable_case(folder,path):
        # This stamp is only emitted by the all-core runner. Old per-core runs
        # cannot be combined into a result under the new case-wide budget.
        if not all((folder/name).exists() for name in ('benchmark_case.json','request.json','summary.json')):
            return False
        expected=case_contract(path)
        if read(folder/'benchmark_case.json') != expected:return False
        request=read(folder/'request.json')
        if any(request.get(k)!=expected[k] for k in ('source_signature','graph_sha256',
                'config_sha256','cores','seconds','parameters','budget_scope','problem_id','backend_version')):return False
        result=read(folder/'summary.json')
        rr=result.get('rows',[])
        return (result.get('status')=='ok' and result.get('coverage_complete') is True
            and all(result.get(k)==v for k,v in identity.items())
            and result.get('source_signature')==signature
            and result.get('budget_scope')=='case_all_cores' and result.get('target_seconds')==a.seconds
            and len(rr)==len(a.cores) and {r.get('cores') for r in rr}==set(a.cores)
            and all(reusable(folder/f'N{n}',path) for n in a.cores))
    def run_case(path):
        folder=a.output/path.stem
        reused=False
        if not folder.exists():
            for previous in a.reuse_verified_run:
                old=previous/path.stem
                if reusable_case(old,path):
                    folder.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copytree(old,folder)
                    write(folder/'reuse.json',dict(source=str(old.resolve()),
                        source_signature=signature,reason='complete all-core case; same budget and independently replayed'))
                    reused=True
                    break
        if a.resume and (folder/'summary.json').exists():
            reused=reusable_case(folder,path)
        if reused:
            result=read(folder/'summary.json')
        else:
            if folder.exists() and a.resume:
                root=a.output.resolve()
                archived=root/'attempts'/path.stem/str(time.time_ns())
                if not folder.resolve().is_relative_to(root) or not archived.resolve().is_relative_to(root):
                    raise ValueError('attempt archival escaped the output directory')
                archived.parent.mkdir(parents=True,exist_ok=True)
                shutil.move(str(folder.resolve()),str(archived))
            result=solve_case(path,a.config,a.cores,folder,seconds=a.seconds)
        rows=[]
        for item in result['rows']:
            cores=item['cores']
            # Reporting happens after the blocking solver has returned. Neither
            # the denominator nor its historical runtime enters the search.
            item=dict(item)
            item.pop('baseline',None)
            item.pop('speedup',None)
            if path.stem in baseline:
                item['baseline']=baseline[path.stem]
                item['speedup']=baseline[path.stem]['cycles']/item['makespan'] if item.get('makespan') else None
            item['baseline_reporting_only']=path.stem in baseline
            write(folder/f'N{cores}'/'summary.json',item)
            row=dict(item,case=path.stem,reused=reused)
            rows.append(row)
            print(json.dumps({k:row.get(k) for k in ('case','cores','status','makespan','score_source','wall_seconds','accounted_wall_seconds','cpu_seconds','hard_timeout','reused')}),flush=True)
        result=dict(result,rows=[{k:v for k,v in row.items() if k not in ('case','reused')} for row in rows])
        write(folder/'summary.json',result)
        write(folder/'benchmark_case.json',case_contract(path))
        case_row=dict({k:v for k,v in result.items() if k!='rows'},case=path.stem,reused=reused)
        print(json.dumps(dict(case=path.stem,scope='all_cores',status=result['status'],
            wall_seconds=result['wall_seconds'],hard_timeout=result.get('hard_timeout',False)),ensure_ascii=False),flush=True)
        return rows,case_row
    rows=[];case_rows=[];errors=[]
    with ThreadPoolExecutor(max_workers=a.local_test_workers) as pool:
        jobs={pool.submit(run_case,path):path for path in paths}
        for future in as_completed(jobs):
            try:
                result_rows,case_row=future.result()
                rows.extend(result_rows);case_rows.append(case_row)
            except Exception as e:errors.append(dict(case=jobs[future].stem,error=str(e)))
            write(a.output/'results.json',dict(status='running',rows=rows,case_rows=case_rows,errors=errors))
    rows.sort(key=lambda r:(r['case'],r['cores']))
    case_rows.sort(key=lambda r:r['case'])
    fields=['case','cores','status','makespan','score_source','speedup','wall_seconds','accounted_wall_seconds',
        'cpu_seconds','cached_baseline_charge_seconds','exceeded_target','extended','exceeded_extension',
        'hard_timeout','timed_out_phase','history_stop','local_test_cache','reused','budget_scope','baseline_reporting_only']
    with (a.output/'timings.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        w=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
    case_fields=['case','status','wall_seconds','cpu_seconds','shared_preparation_seconds',
        'target_seconds','extension_seconds','hard_limit_seconds','exceeded_target','extended',
        'exceeded_extension','hard_timeout','coverage_complete','budget_scope','reused','source_signature']
    with (a.output/'case_timings.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        w=csv.DictWriter(stream,fieldnames=case_fields,extrasaction='ignore');w.writeheader();w.writerows(case_rows)
    expected=len(paths)*len(a.cores)
    successful=sum(r['status']=='ok' for r in rows)
    complete=not errors and len(rows)==expected and len(case_rows)==len(paths) and all(r.get('coverage_complete') for r in case_rows)
    write(a.output/'results.json',dict(status='complete' if complete else 'incomplete',rows=rows,case_rows=case_rows,
        coverage_complete=complete,expected_results=expected,successful_results=successful,
        hard_timeouts=sum(r.get('hard_timeout',False) for r in rows),
        case_hard_timeouts=sum(r.get('hard_timeout',False) for r in case_rows),
        failed_or_timed_out=[dict(case=r['case'],cores=r['cores'],status=r['status']) for r in rows if r['status']!='ok'],
        errors=errors,elapsed_seconds=time.perf_counter()-started,manifest=manifest))
    return bool(errors or successful!=expected)


if __name__=='__main__':raise SystemExit(main())
