"""Matched cold/warm regression study for the Q3v3 global-joint revision.

Reuses an already frozen stratified sample, never selects by measured gain.
Previous Q3 final plans are comparators only, not new solver warm inputs.
"""
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import shutil
import statistics
import sys
import time

from q3v3 import ALGORITHM_NAME, ALGORITHM_REVISION
from q3v3.official import ROOT, _validate_replay, metrics, objective, read_settings, sha256, write_json
from q3v3.search import solve_one
from solve_q3v3 import hardware


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def worker(job):
    row, _ = solve_one(job['graph'], job['cores'], job['config'], job['output'], job['q1_source'],
        warm_plan=job['warm_plan'], seconds=job['seconds'], max_evaluations=job['max_evaluations'],
        evaluation_timeout=600, near_bound_fraction=.02)
    return dict(row, mode=job['mode'])


def verify(folder, graph, config, settings):
    replay = read(folder/'verified/replay.json')
    result, trace = read(folder/'verified/result.json'), read(folder/'verified/trace.json')
    log = (folder/'verified/official.log').read_text(encoding='utf-8-sig')
    if replay['status'] != 'ok' or not replay['complete_trace_validated'] or not replay['official_log_validated']:
        raise ValueError(f'incomplete replay: {folder}')
    for name, path in (('graph', graph), ('config', config), ('plan', folder/'plan.json')):
        if replay[f'{name}_sha256'] != sha256(path):
            raise ValueError(f'{name} hash mismatch: {folder}')
    for name, digest in replay['official_source_sha256'].items():
        if sha256(ROOT/'official/code'/name) != digest:
            raise ValueError(f'official source mismatch: {name}')
    _validate_replay(result, trace, log, Path(graph).name, 'plan.json', settings)
    summary = read(folder/'summary.json')
    if summary['makespan_cycles'] != result['makespan'] or summary['added_copy_bytes'] != objective(result)[1]:
        raise ValueError(f'summary mismatch: {folder}')
    return summary, metrics(result), {name:sha256(folder/'verified'/name)
                                    for name in ('result.json','trace.json','official.log','replay.json')}


def report(output):
    manifest = read(output/'run_info.json')
    settings = read_settings(manifest['config'])
    comparisons = []
    for job in manifest['jobs']:
        new, new_metrics, hashes = verify(Path(job['output']), job['graph'], Path(job['config']), settings)
        if job['mode']=='warm' and new['supplied_plan_makespan'] != job['reference_cycles']:
            raise ValueError('supplied Q1 plan did not reproduce the fixed Q3 reference')
        old_folder = Path(manifest[f"old_{job['mode']}"])/new['case']/f"N{new['num_cores']}"
        old, old_metrics, old_hashes = verify(old_folder, job['graph'], Path(job['config']), settings)
        comparisons.append(dict(case=new['case'], num_cores=new['num_cores'], mode=job['mode'],
            q1_direct_cycles=job['reference_cycles'], old=old_metrics, new=new_metrics,
            old_total_seconds=old['total_seconds'], new_total_seconds=new['total_seconds'],
            old_search_seconds=old['search_seconds'], new_search_seconds=new['search_seconds'],
            reduction_percent=100*(1-new['makespan_cycles']/old['makespan_cycles']),
            old_source=str(old_folder), new_source=job['output'], new_artifact_hashes=hashes,
            old_artifact_hashes=old_hashes, winner=new['winner'], official_calls=new['official_calls']))
    aggregates = {}
    for mode in manifest['modes']:
        rows = [r for r in comparisons if r['mode']==mode]
        def total(which,key):
            return sum(r[which][key] for r in rows)
        aggregates[mode] = dict(count=len(rows), faster=sum(r['reduction_percent']>0 for r in rows),
            equal=sum(r['reduction_percent']==0 for r in rows), slower=sum(r['reduction_percent']<0 for r in rows),
            mean_reduction_percent=statistics.mean(r['reduction_percent'] for r in rows),
            median_reduction_percent=statistics.median(r['reduction_percent'] for r in rows),
            vs_q1_faster=sum(r['new']['makespan_cycles']<r['q1_direct_cycles'] for r in rows),
            vs_q1_equal=sum(r['new']['makespan_cycles']==r['q1_direct_cycles'] for r in rows),
            vs_q1_slower=sum(r['new']['makespan_cycles']>r['q1_direct_cycles'] for r in rows),
            mean_total_seconds=statistics.mean(r['new_total_seconds'] for r in rows),
            old_mean_total_seconds=statistics.mean(r['old_total_seconds'] for r in rows))
        for which in ('old','new'):
            aggregates[mode][which] = {key:total(which,key) for key in (
                'added_copy_bytes','physical_ddr_bytes','cache_hit_bytes','cache_miss_bytes','cache_hits','cache_misses')}
            sums = aggregates[mode][which]
            sums['byte_hit_rate'] = sums['cache_hit_bytes']/max(1,sums['cache_hit_bytes']+sums['cache_miss_bytes'])
            sums['request_hit_rate'] = sums['cache_hits']/max(1,sums['cache_hits']+sums['cache_misses'])
    audit = dict(status='passed', algorithm=ALGORITHM_NAME, revision=manifest['revision'],
                 verified=len(comparisons), aggregates=aggregates, rows=comparisons,
                 caveat='Paired single runs; search wall budget may change coverage under host load. Historical Q1 solve time excluded.')
    write_json(output/'comparison.json',audit)
    lines = [f"# Q3v3 {manifest['revision']} 对照结果", '',
        '沿用既有随机分层20组，不读取旧Q3最终方案作为起点。新旧方案均核验官方结果、Trace、日志及来源。', '',
        '|模式|更快/相同/更慢|平均周期降低|中位数降低|平均求解秒（旧→新）|', '|---|---|---:|---:|---|']
    for mode,r in aggregates.items():
        lines.append(f"|{mode}|{r['faster']}/{r['equal']}/{r['slower']}|{r['mean_reduction_percent']:.3f}%|"
                     f"{r['median_reduction_percent']:.3f}%|{r['old_mean_total_seconds']:.2f} → {r['mean_total_seconds']:.2f}|")
    lines += ['', '|模式|added COPY（旧→新）|物理DDR（旧→新）|加权字节命中率（旧→新）|加权请求命中率（旧→新）|',
              '|---|---:|---:|---|---|']
    for mode,r in aggregates.items():
        a,b = r['old'],r['new']
        lines.append(f"|{mode}|{a['added_copy_bytes']:,} → {b['added_copy_bytes']:,}|{a['physical_ddr_bytes']:,} → {b['physical_ddr_bytes']:,}|"
                     f"{a['byte_hit_rate']:.2%} → {b['byte_hit_rate']:.2%}|{a['request_hit_rate']:.2%} → {b['request_hit_rate']:.2%}|")
    lines += ['', '|模式|case|N|Q1直接移植|旧Q3v3|新Q3v3|周期降低|added COPY|物理DDR|字节命中率|求解秒|',
              '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in sorted(comparisons,key=lambda r:(r['mode'],r['num_cores'],r['case'])):
        new = r['new']
        lines.append(f"|{r['mode']}|{r['case']}|{r['num_cores']}|{r['q1_direct_cycles']:,}|{r['old']['makespan_cycles']:,}|"
                     f"{new['makespan_cycles']:,}|{r['reduction_percent']:.3f}%|{new['added_copy_bytes']:,}|{new['physical_ddr_bytes']:,}|"
                     f"{new['cache_hit_rate_bytes']:.2%}|{r['new_total_seconds']:.2f}|")
    lines += ['', '命中率按总字节/总请求加权，不能由各case命中率简单平均。优化目标为周期、added COPY的字典序；命中率不保证单调。',
              f"每组合搜索{manifest['jobs'][0]['seconds']:g}秒、至多{manifest['jobs'][0]['max_evaluations']}次官方调用（含初始方案），{manifest['workers']}个主机worker；准备与独立复评另计。未计入历史Q1求解时间。",
              '本报告是配对单次实验，不是全100例结论；墙钟差异含机器负载和不同停止条件。', '']
    (output/'RESULT.md').write_text('\n'.join(lines),encoding='utf-8')
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference',type=Path,default=ROOT/'outputs/q3v3_stratified20_20260927')
    parser.add_argument('--old-cold',type=Path,default=ROOT/'outputs/q3v3_cold20_control_20260927')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--modes',nargs='+',choices=('cold','warm'),default=['cold','warm'])
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--seconds',type=float,default=120)
    parser.add_argument('--max-evaluations',type=int,default=64)
    parser.add_argument('--report-only',action='store_true')
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if args.report_only:
        print(json.dumps(report(output)['aggregates'],ensure_ascii=False,indent=2))
        return 0
    output.mkdir(parents=True,exist_ok=False)
    reference = read(args.reference/'run_info.json')
    items = read(args.reference/'reference_inputs.json')['items']
    if reference['status']!='complete' or len(items)!=20 or read(args.old_cold/'run_info.json')['status']!='complete':
        raise ValueError('requires complete matched reference runs')
    for name,digest in reference['q1_snapshot']['files'].items():
        if sha256(args.reference/'q1_source_snapshot'/name)!=digest:
            raise ValueError('Q1 snapshot changed')
    sources = sorted((ROOT/'src/q3v3').glob('*.py')) + sorted((ROOT/'official/code').glob('*.py')) + [Path(__file__),ROOT/'src/solve_q3v3.py']
    hashes = {str(p.relative_to(ROOT)):sha256(p) for p in sources}
    for p in sources:
        target = output/'source_snapshot'/p.relative_to(ROOT)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(p,target)
    jobs = []
    for mode in args.modes:
        for item in items:
            for name,path in (('graph',Path(item['graph'])),('config',Path(reference['config'])),('plan',Path(item['warm_plan']))):
                if sha256(path)!=item[f'{name}_sha256']:
                    raise ValueError(f'reference {name} changed')
            jobs.append(dict(mode=mode,graph=item['graph'],cores=item['num_cores'],config=reference['config'],
                q1_source=reference['q1_source'],output=str(output/mode/item['case']/f"N{item['num_cores']}"),
                warm_plan=item['warm_plan'] if mode=='warm' else None, reference_cycles=item['reference_cycles'],
                seconds=args.seconds,max_evaluations=args.max_evaluations))
    manifest = dict(algorithm=ALGORITHM_NAME,revision=ALGORITHM_REVISION,status='running',
        started_at=datetime.now(timezone.utc).isoformat(),expected=len(jobs),completed=0,workers=args.workers,
        hardware=hardware(),config=reference['config'],old_warm=str(args.reference.resolve()),old_cold=str(args.old_cold.resolve()),
        modes=args.modes,source_hashes=hashes,jobs=jobs,q1_snapshot=reference['q1_snapshot'],sampling=reference['sampling'])
    write_json(output/'run_info.json',manifest)
    rows,failures = [],[]
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(worker,job):job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                row = future.result()
                rows.append(row)
                print(json.dumps(dict(completed=len(rows),mode=job['mode'],case=row['case'],N=row['num_cores'],
                    final=row['makespan_cycles'],winner=row['winner'],seconds=round(row['total_seconds'],2),
                    calls=row['official_calls']),ensure_ascii=False),flush=True)
            except Exception as exc:
                failures.append(dict(job=job,error=repr(exc)))
                print(json.dumps(failures[-1],ensure_ascii=False),flush=True)
            manifest['completed'] = len(rows)
            write_json(output/'summary.json',rows)
            write_json(output/'run_info.json',manifest)
    manifest.update(elapsed_seconds=time.perf_counter()-started,failures=failures,
        sources_unchanged=all(sha256(ROOT/p)==digest for p,digest in hashes.items()))
    manifest['status'] = 'complete' if len(rows)==len(jobs) and not failures and manifest['sources_unchanged'] else 'failed'
    write_json(output/'run_info.json',manifest)
    if manifest['status']!='complete':
        return 1
    result = report(output)
    print(json.dumps(result['aggregates'],ensure_ascii=False,indent=2),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
