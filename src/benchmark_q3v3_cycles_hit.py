"""Fixed 20-pair Q1-plan-only regression: Q3 cycles first, byte hit rate second."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import statistics
import time

from q3v3 import ALGORITHM_NAME, ALGORITHM_REVISION
from q3v3.objective import key, describe
from q3v3.official import ROOT, metrics, read_settings, sha256, write_json
from q3v3.search import solve_one
from benchmark_q3v3_global_joint import verify
from solve_q3v3 import hardware


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def worker(job):
    row, _ = solve_one(job['graph'], job['cores'], job['config'], job['output'],
        warm_plan=job['warm_plan'], seconds=job['seconds'], max_evaluations=job['max_evaluations'],
        evaluation_timeout=600, near_bound_fraction=.02, objective_policy='cycles-hit', seed_policy='auto',
        enable_structural=job.get('enable_structural', True))
    return row


def aggregate(rows, before):
    a, b = [r[before] for r in rows], [r['new'] for r in rows]
    def sums(data):
        values = {k:sum(r[k] for r in data) for k in ('cache_hit_bytes','cache_miss_bytes',
                  'cache_hits','cache_misses','physical_ddr_bytes','added_copy_bytes')}
        values['byte_hit_rate'] = values['cache_hit_bytes']/max(1,values['cache_hit_bytes']+values['cache_miss_bytes'])
        values['request_hit_rate'] = values['cache_hits']/max(1,values['cache_hits']+values['cache_misses'])
        return values
    gains = [100*(1-y['makespan_cycles']/x['makespan_cycles']) for x,y in zip(a,b)]
    return dict(count=len(rows),faster=sum(x>0 for x in gains),equal=sum(x==0 for x in gains),
        slower=sum(x<0 for x in gains),mean_cycle_reduction_percent=statistics.mean(gains),
        median_cycle_reduction_percent=statistics.median(gains),before=sums(a),after=sums(b),
        hit_better=sum(y['cache_hit_rate_bytes']>x['cache_hit_rate_bytes'] for x,y in zip(a,b)),
        hit_equal=sum(y['cache_hit_rate_bytes']==x['cache_hit_rate_bytes'] for x,y in zip(a,b)),
        hit_worse=sum(y['cache_hit_rate_bytes']<x['cache_hit_rate_bytes'] for x,y in zip(a,b)),
        equal_cycles_hit_better=sum(y['makespan_cycles']==x['makespan_cycles'] and
            y['cache_hit_rate_bytes']>x['cache_hit_rate_bytes'] for x,y in zip(a,b)))


def report(output):
    manifest=read(output/'run_info.json')
    if manifest['status']!='complete' or manifest['completed']!=manifest['expected']:
        raise ValueError('incomplete benchmark')
    for name,digest in manifest['source_hashes'].items():
        if sha256(output/'source_snapshot'/name)!=digest:
            raise ValueError(f'changed source snapshot: {name}')
    settings=read_settings(manifest['config'])
    rows=[]
    for job in manifest['jobs']:
        folder=Path(job['output'])
        new,new_metrics,hashes=verify(folder,job['graph'],Path(job['config']),settings)
        old_folder=Path(manifest['previous'])/new['case']/f"N{new['num_cores']}"
        old,old_metrics,old_hashes=verify(old_folder,job['graph'],Path(job['config']),settings)
        history=read(folder/'history.json')
        supplied=next(r for r in history if r['name']=='provided_q1_plan' and r['status']=='ok')
        initial=read(folder/supplied['evaluation_directory']/'result.json')
        final=read(folder/'verified/result.json')
        if (initial['makespan']!=job['reference_cycles'] or new['q1_source_provenance'] is not None
                or new['source_hashes']['warm_plan']!=job['plan_sha256']
                or sha256(job['warm_plan'])!=job['plan_sha256']
                or key(final)>key(initial) or new['singlecore_score_used']):
            raise ValueError('warm boundary, identity, or objective regression')
        if any(new[k]!=v for k,v in metrics(final).items()):
            raise ValueError('metric mismatch after replay')
        rows.append(dict(case=new['case'],num_cores=new['num_cores'],q1=metrics(initial),old=old_metrics,
            new=new_metrics,new_total_seconds=new['total_seconds'],old_total_seconds=old['total_seconds'],
            search_seconds=new['search_seconds'],official_calls=new['official_calls'],winner=new['winner'],
            cache_only_improvements=new['cache_only_improvements'],stop_reason=new['stop_reason'],
            structural_calls=new.get('structural_calls',0),
            structural_improvements=new.get('structural_improvements',0),
            structural_scales=new.get('structural_scales',[]),old_revision=old['algorithm_revision'],
            new_source=str(folder),old_source=str(old_folder),artifact_hashes=hashes,old_artifact_hashes=old_hashes))
    aggs={name:aggregate(rows,name) for name in ('q1','old')}
    result=dict(status='passed',revision=manifest['revision'],verified=len(rows),aggregates=aggs,rows=rows,
        batch_wall_seconds=manifest['elapsed_seconds'],
        mean_total_seconds=statistics.mean(r['new_total_seconds'] for r in rows),
        old_mean_total_seconds=statistics.mean(r['old_total_seconds'] for r in rows),
        cache_only_improvements=sum(r['cache_only_improvements'] for r in rows))
    write_json(output/'comparison.json',result)
    lines=[f"# Q3v3 {manifest['revision']} 实测",'',
        '同一批20组分层样本、相同Q1v3完整方案JSON。Q1方案在本次Q3重新评价；旧Q3结果仅作对照，不作为起点。',
        '主目标为 cycles 最小，次目标为 L2 字节命中率最大；仅在 cycles 相同时比较命中率。', '',
        '|对照|cycles 更快/持平/更慢|平均 cycles 降低|加权字节命中率|物理DDR字节|',
        '|---|---|---:|---|---|']
    old_revision = ', '.join(sorted({r['old_revision'] for r in rows}))
    for name,title in (('q1','Q1v3直接移植到Q3'),('old',f'旧Q3v3 {old_revision}')):
        a=aggs[name]
        lines.append(f"|{title}|{a['faster']}/{a['equal']}/{a['slower']}|{a['mean_cycle_reduction_percent']:.3f}%|"
            f"{a['before']['byte_hit_rate']:.2%} → {a['after']['byte_hit_rate']:.2%}|"
            f"{a['before']['physical_ddr_bytes']:,} → {a['after']['physical_ddr_bytes']:,}|")
    lines += ['', '|case|N|Q1移植 cycles|旧Q3 cycles|新Q3 cycles|Q1→新字节命中率|旧→新字节命中率|新物理DDR|新 added COPY|求解秒|',
              '|---|---:|---:|---:|---:|---|---|---:|---:|---:|']
    for r in sorted(rows,key=lambda r:(r['num_cores'],r['case'])):
        a,b,c=r['q1'],r['old'],r['new']
        lines.append(f"|{r['case']}|{r['num_cores']}|{a['makespan_cycles']:,}|{b['makespan_cycles']:,}|{c['makespan_cycles']:,}|"
            f"{a['cache_hit_rate_bytes']:.2%} → {c['cache_hit_rate_bytes']:.2%}|{b['cache_hit_rate_bytes']:.2%} → {c['cache_hit_rate_bytes']:.2%}|"
            f"{c['physical_ddr_bytes']:,}|{c['added_copy_bytes']:,}|{r['new_total_seconds']:.2f}|")
    a=aggs['q1']
    lines += ['', f"20/20官方独立CLI重放与完整Trace/日志核验。批次墙钟 {manifest['elapsed_seconds']:.2f} 秒，{manifest['workers']} 个主机进程并行。",
        f"每组准备、搜索、最终复评平均总用时 {result['mean_total_seconds']:.2f} 秒；旧版 {result['old_mean_total_seconds']:.2f} 秒。",
        f"对Q1起点：命中率上升/持平/下降 {a['hit_better']}/{a['hit_equal']}/{a['hit_worse']}；cycles持平且命中提高 {a['equal_cycles_hit_better']} 组。",
        f"本次搜索累计接受 {result['cache_only_improvements']} 次 cycles 相同、字节命中率严格提高的动作。", '',
        f"结构重构官方调用 {sum(r['structural_calls'] for r in rows)} 次，直接改善 {sum(r['structural_improvements'] for r in rows)} 次；逐例搜索半径和履历见 JSON。", '',
        '## 口径与复现', '',
        f"每组搜索预算{manifest['jobs'][0]['seconds']:g}秒、至多{manifest['jobs'][0]['max_evaluations']}次官方调用（含起点）。准备和最终复评另计。历史Q1求解时间不计入本次增量优化。",
        '命中率由总命中字节/总读取字节计算，请求命中率另存 comparison.json。平均周期降低是20个配对百分比的算术平均，不是比赛加速比。',
        '严格优先级保证相对本次输入起点 cycles 不退化、cycles相同时字节命中率不退化；cycles下降时允许命中率下降。不能保证优于另一预算搜索轨迹的旧Q3最终结果。',
        '这20组是开发回归样本，并非未见测试集。当前为一次配对运行，墙钟负载可改变限时搜索覆盖。',
        '单个搜索进程串行评价候选；本批并行用时不构成CPU绑单核条件下的5–10分钟保证。未运行Q2四格对照或全100例。',
        '复现命令：`.venv/Scripts/python.exe -B -X utf8 src/benchmark_q3v3_cycles_hit.py --output outputs/q3v3_q1warm_new_run --workers 4`', '']
    (output/'RESULT.md').write_text('\n'.join(lines),encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference',type=Path,default=ROOT/'outputs/q3v3_stratified20_20260927')
    parser.add_argument('--previous',type=Path,default=ROOT/'outputs/q3v3_global_joint20_r2_20260927/warm')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--seconds',type=float,default=120)
    parser.add_argument('--max-evaluations',type=int,default=64)
    parser.add_argument('--report-only',action='store_true')
    parser.add_argument('--no-structural',action='store_true')
    args=parser.parse_args()
    output=args.output.resolve()
    if args.report_only:
        print(json.dumps(report(output)['aggregates'],ensure_ascii=False,indent=2))
        return
    if args.workers<1 or args.seconds<=0 or args.max_evaluations<3:
        parser.error('invalid workers or budget')
    reference=read(args.reference/'run_info.json')
    items=read(args.reference/'reference_inputs.json')['items']
    if reference['status']!='complete' or len(items)!=20:
        raise ValueError('reference sample must be the completed frozen20')
    output.mkdir(parents=True,exist_ok=False)
    sources=sorted((ROOT/'src/q3v3').glob('*.py'))+sorted((ROOT/'official/code').glob('*.py'))+[
        Path(__file__),ROOT/'src/solve_q3v3.py',ROOT/'src/report_q3v3.py',ROOT/'src/benchmark_q3v3_global_joint.py']
    hashes={str(p.relative_to(ROOT)):sha256(p) for p in sources}
    for p in sources:
        target=output/'source_snapshot'/p.relative_to(ROOT)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(p,target)
    jobs=[]
    for item in items:
        for label,path in (('graph',item['graph']),('config',reference['config']),('plan',item['warm_plan'])):
            if sha256(path)!=item[label+'_sha256']:
                raise ValueError(f'changed reference {label}')
        target=output/'inherited_plans'/item['case']/f"N{item['num_cores']}"/'plan.json'
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(item['warm_plan'],target)
        jobs.append(dict(graph=item['graph'],cores=item['num_cores'],config=reference['config'],
            output=str(output/item['case']/f"N{item['num_cores']}"),warm_plan=str(target),
            plan_sha256=item['plan_sha256'],reference_cycles=item['reference_cycles'],
            seconds=args.seconds,max_evaluations=args.max_evaluations,enable_structural=not args.no_structural))
    manifest=dict(algorithm=ALGORITHM_NAME,revision=ALGORITHM_REVISION,status='running',expected=len(jobs),completed=0,
        started_at=datetime.now(timezone.utc).isoformat(),workers=args.workers,hardware=hardware(),
        previous=str(args.previous.resolve()),config=reference['config'],sampling=reference['sampling'],
        objective=describe(),q1_execution=False,source_hashes=hashes,jobs=jobs)
    write_json(output/'run_info.json',manifest)
    rows,failures=[],[]
    started=time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(worker,job):job for job in jobs}
        for future in as_completed(futures):
            job=futures[future]
            try:
                row=future.result()
                rows.append(row)
                print(json.dumps(dict(completed=len(rows),case=row['case'],N=row['num_cores'],cycles=row['makespan_cycles'],
                    hit=row['cache_hit_rate_bytes'],winner=row['winner'],seconds=round(row['total_seconds'],2)),ensure_ascii=False),flush=True)
            except Exception as exc:
                failures.append(dict(job=job,error=repr(exc)))
                print(json.dumps(failures[-1],ensure_ascii=False),flush=True)
            manifest['completed']=len(rows)
            write_json(output/'summary.json',rows)
            write_json(output/'run_info.json',manifest)
    manifest.update(elapsed_seconds=time.perf_counter()-started,failures=failures,
        sources_unchanged=all(sha256(ROOT/p)==d for p,d in hashes.items()))
    manifest['status']='complete' if len(rows)==len(jobs) and not failures and manifest['sources_unchanged'] else 'failed'
    write_json(output/'run_info.json',manifest)
    if manifest['status']!='complete':
        raise RuntimeError('benchmark failed; see run_info.json')
    print(json.dumps(report(output)['aggregates'],ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    main()
