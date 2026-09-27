"""Stratified, reproducible Q3v3 evaluation from complete Q1v3 warm plans.

Sampling uses case identifiers and core counts only, never saved performance.
Each selected case runs at exactly one core count; no cross-count carry is used.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time
import traceback

from q3v3 import ALGORITHM_NAME
from q3v3.official import ROOT, _validate_replay, read_settings, sha256, write_json
from q3v3.q1_compat import DEFAULT_Q1_SOURCE, snapshot_source
from q3v3.search import solve_one
from report_q3v3 import report
from solve_q3v3 import hardware, save_report

CORES = (2, 3, 4, 5)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def stratified_sample(keys, per_core, seed):
    """Balanced core strata, globally distinct cases, equal case inclusion."""
    if type(per_core) is not int or per_core < 1:
        raise ValueError('per_core must be a positive integer')
    keys = tuple(keys)
    available = {n: {case for case, core in keys if core == n} for n in CORES}
    population = sorted(set.intersection(*available.values()))
    if len(population) < per_core * len(CORES):
        raise ValueError('not enough distinct cases with all requested core counts')
    draws = random.Random(seed).sample(population, per_core * len(CORES))
    pairs = [(case, core) for index, core in enumerate(CORES)
             for case in sorted(draws[index*per_core:(index+1)*per_core])]
    return population, pairs


def load_references(folders, warm_root, config):
    catalog, files = {}, {}
    official = {str(p.resolve()): sha256(p) for p in (ROOT/'official/code').glob('*.py')}
    official[str(config.resolve())] = sha256(config)
    for folder in folders:
        manifest = read(folder/'run_info.json')
        if manifest['status'] != 'complete' or Path(manifest['source_run']).resolve() != warm_root:
            raise ValueError(f'incomplete or mismatched reference source: {folder}')
        for path, digest in official.items():
            if manifest['source_sha256'].get(path) != digest:
                raise ValueError(f'reference used different official source/config: {path}')
        for name in ('run_info.json', 'summary.json'):
            files[str(folder/name)] = sha256(folder/name)
        for row in read(folder/'summary.json'):
            key = row['case'], row['num_cores']
            if key in catalog or row['status'] != 'ok' or row['evaluations']['3']['status'] != 'ok':
                raise ValueError(f'duplicate or failed reference: {key}')
            catalog[key] = row
    return catalog, files


def check_reference(row, warm_root, config, settings):
    case, cores = row['case'], row['num_cores']
    graph = ROOT/'official/data'/f'{case}.json'
    plan = warm_root/case/f'N{cores}'/'plan.json'
    directory = Path(row['output_directory'])
    replay = read(directory/'q3/replay.json')
    evidence = row['evaluations']['3']
    hashes = dict(graph=sha256(graph), config=sha256(config), plan=sha256(plan))
    if Path(row['source_plan']).resolve() != plan:
        raise ValueError(f'wrong warm plan path for {case}/{cores}')
    for key, digest in hashes.items():
        if replay[f'{key}_sha256'] != digest or evidence[f'{key}_sha256'] != digest:
            raise ValueError(f'reference {key} hash changed: {case}/{cores}')
    if sha256(directory/'plan.json') != hashes['plan'] or row['plan_sha256'] != hashes['plan']:
        raise ValueError('reference is not the exact supplied Q1v3 plan')
    if replay['status'] != 'ok' or not replay['trace_matches']:
        raise ValueError('reference replay was not validated')
    for name in ('result', 'trace'):
        digest = sha256(directory/'q3'/f'{name}.json')
        if digest != replay[f'{name}_sha256'] or digest != evidence[f'{name}_sha256']:
            raise ValueError(f'reference {name} changed')
    result = read(directory/'q3/result.json')
    _validate_replay(result, read(directory/'q3/trace.json'),
                     (directory/'q3/official.log').read_text(encoding='utf-8'),
                     graph.name, 'plan.json', settings)
    if result['num_cores'] != cores or result['makespan'] != evidence['makespan_cycles']:
        raise ValueError('reference identity or score mismatch')
    return dict(case=case, num_cores=cores, graph=str(graph), original_warm_plan=str(plan),
                reference_directory=str(directory), graph_sha256=hashes['graph'],
                config_sha256=hashes['config'], plan_sha256=hashes['plan'],
                reference_result_sha256=sha256(directory/'q3/result.json'),
                reference_trace_sha256=sha256(directory/'q3/trace.json'),
                reference_log_sha256=sha256(directory/'q3/official.log'),
                q1_cycles=row['source_q1_cycles'], reference_cycles=result['makespan'],
                reference_added_copy_bytes=result['data_movement_bytes']['added_copy_bytes'])


def run_pair(request):
    item = request['item']
    output = Path(request['output'])/item['case']/f"N{item['num_cores']}"
    try:
        summary, _ = solve_one(item['graph'], item['num_cores'], request['config'], output,
            request['q1_source'], warm_plan=item['warm_plan'], previous_plan=None,
            **request['settings'])
        return summary
    except Exception as exc:
        row = dict(case=item['case'], num_cores=item['num_cores'], status='failed',
                   error=str(exc), traceback=traceback.format_exc())
        write_json(output/'failure.json', row)
        return row


def check_warm_comparison(row, item, inherited):
    """A structural seed must not silently replace the requested baseline."""
    if row['source_hashes']['warm_plan'] != item['plan_sha256']:
        raise ValueError('search did not use the selected complete Q1v3 plan')
    if (row['supplied_plan_makespan'] != item['reference_cycles'] or
            inherited['makespan_cycles'] != item['reference_cycles'] or
            inherited['added_copy_bytes'] != item['reference_added_copy_bytes']):
        raise ValueError('fresh Q3 warm evaluation differs from saved Q1v3-in-Q3 reference')
    if (row['makespan_cycles'], row['added_copy_bytes']) > (
            item['reference_cycles'], item['reference_added_copy_bytes']):
        raise ValueError('Q3v3 regressed from the inherited complete plan')


def finish_report(output, rows, manifest, items, reference_files):
    generic = report(output)
    if any(sha256(path) != digest for path, digest in reference_files.items()):
        raise ValueError('source reference metadata changed during the experiment')
    lookup = {(v['case'], v['num_cores']): v for v in items}
    comparisons = []
    for row in sorted(rows, key=lambda r: (r['num_cores'], r['case'])):
        item = lookup[row['case'], row['num_cores']]
        directory = output/row['case']/f"N{row['num_cores']}"
        history = read(directory/'history.json')
        inherited = next(r for r in history if r['name']=='provided_q1_plan' and r['status']=='ok')
        check_warm_comparison(row, item, inherited)
        for path, digest in (
            (item['original_warm_plan'], item['plan_sha256']),
            (item['warm_plan'], item['plan_sha256']),
            (Path(item['reference_directory'])/'q3/result.json', item['reference_result_sha256']),
            (Path(item['reference_directory'])/'q3/trace.json', item['reference_trace_sha256']),
            (Path(item['reference_directory'])/'q3/official.log', item['reference_log_sha256']),
        ):
            if sha256(path) != digest:
                raise ValueError(f'original/captured/reference artifact changed: {path}')
        old, final = item['reference_cycles'], row['makespan_cycles']
        comparisons.append(dict(case=row['case'], cores=row['num_cores'],
            inherited_q3_cycles=old, best_seed_cycles=row['seed_makespan'], final_cycles=final,
            reduction_percent=100*(1-final/old), search_only_reduction_percent=row['reduction_from_seed_percent'],
            speedup=old/final, preparation_seconds=row['preparation_seconds'],
            search_seconds=row['search_seconds'], final_replay_seconds=row['final_replay_seconds'],
            total_seconds=row['total_seconds'], official_calls=row['official_calls'],
            winner=row['winner'], stop_reason=row['stop_reason'],
            inherited_added_copy_bytes=item['reference_added_copy_bytes'],
            final_added_copy_bytes=row['added_copy_bytes'],
            initial_cache_hit_rate_bytes=inherited['cache_hit_rate_bytes'],
            final_cache_hit_rate_bytes=row['cache_hit_rate_bytes'], plan_sha256=row['plan_sha256']))
    groups = []
    for core in (*CORES, 'all'):
        group = [r for r in comparisons if core=='all' or r['cores']==core]
        groups.append(dict(cores=core, count=len(group),
            faster=sum(r['final_cycles']<r['inherited_q3_cycles'] for r in group),
            equal=sum(r['final_cycles']==r['inherited_q3_cycles'] for r in group),
            slower=sum(r['final_cycles']>r['inherited_q3_cycles'] for r in group),
            mean_reduction_percent=statistics.mean(r['reduction_percent'] for r in group),
            median_reduction_percent=statistics.median(r['reduction_percent'] for r in group),
            geometric_mean_speedup=math.exp(statistics.mean(math.log(r['speedup']) for r in group)),
            mean_search_only_reduction_percent=statistics.mean(r['search_only_reduction_percent'] for r in group),
            mean_total_seconds=statistics.mean(r['total_seconds'] for r in group)))
    audit = dict(status='passed', algorithm=ALGORITHM_NAME, expected=len(items), verified=len(rows),
        reference_matches=len(rows), source_files_unchanged=True, original_plans_unchanged=True,
        complete_trace_and_log_validated=True, sampling=manifest['sampling'],
        benchmark_wall_seconds=manifest['elapsed_seconds'], groups=groups, comparisons=comparisons,
        note='Same Q3 hardware on both sides; existing Q1 solving time excluded from incremental Q3 time.')
    write_json(output/'STRATIFIED_AUDIT.json', audit)
    total = groups[-1]
    lines = [f'# {ALGORITHM_NAME}：Q1v3完整解热启动，按核数随机分层20组试验', '',
        f"完成{len(rows)}/{len(items)}组；更快{total['faster']}、持平{total['equal']}、更慢{total['slower']}。",
        f"逐组平均耗时降低{total['mean_reduction_percent']:.4f}%，中位数{total['median_reduction_percent']:.4f}%；几何平均加速比{total['geometric_mean_speedup']:.6f}。", '',
        '## 抽样与口径', '',
        f"随机种子{manifest['sampling']['seed']}；从共同覆盖2/3/4/5核的{len(manifest['sampling']['population'])}张图抽取{len(items)}张不同图，随机分配到四个核数层，每层{manifest['sampling']['per_core']}组。抽样未使用成绩、下界或图特征。",
        '本轮是20个case/核数组合，不是20张图各跑四种核数。每组独立运行，不继承其他核数的Q3搜索结果。',
        f"完整划分来源：`{manifest['warm_start']}`。冻结并核对原始plan字节，重新用Q3官方程序评估，20组初值须与已保存的直接移植结果完全一致。",
        'Q1结构种子仍是现有算法的备用候选；报告同时列相对完整Q1v3方案、相对最好起点的改善。没有新跑完整Q1，也没有接入另一套Q2起点Q3算法的结果。',
        f"每组搜索{manifest['settings']['seconds']:g}秒、最多{manifest['settings']['max_evaluations']}次评价（含起点评价，不含最终回放）；{manifest['workers']}个主机进程并行。",
        f"批次墙钟{manifest['elapsed_seconds']:.2f}秒，含本轮来源检查/快照/准备/搜索/最终回放，不含历史Q1求解时间与本轮事后汇总审计。", '',
        '## 逐组结果', '',
        '|核数|case|Q1v3直接移植Q3 cycles|最好起点 cycles|Q3v3 cycles|相对完整方案降低|相对最好起点降低|本轮求解秒|胜出动作|',
        '|---:|---|---:|---:|---:|---:|---:|---:|---|']
    for r in comparisons:
        lines.append(f"|{r['cores']}|{r['case']}|{r['inherited_q3_cycles']:,}|{r['best_seed_cycles']:,}|{r['final_cycles']:,}|{r['reduction_percent']:.3f}%|{r['search_only_reduction_percent']:.3f}%|{r['total_seconds']:.2f}|{r['winner']}|")
    lines += ['', '## 按核数汇总', '',
        '|核数|组数|更快/持平/更慢|平均降低|中位数降低|几何平均加速比|平均求解秒|',
        '|---|---:|---|---:|---:|---:|---:|']
    for r in groups:
        lines.append(f"|{r['cores']}|{r['count']}|{r['faster']}/{r['equal']}/{r['slower']}|{r['mean_reduction_percent']:.3f}%|{r['median_reduction_percent']:.3f}%|{r['geometric_mean_speedup']:.5f}|{r['mean_total_seconds']:.2f}|")
    lines += ['', '## 验证与限制', '',
        '- 最终方案逐份通过独立官方CLI回放、完整Trace/日志及哈希检查；同时核对继承原方案和已保存Q3重评值。',
        '- Q3v3名称标识本目录对应的现有搜索器。本轮仅增加版本标识和抽样/审计工具，没有改变候选或接纳逻辑。',
        '- 样本为公开题目中的随机子集，不是未见图族。20组小样本不能代表完整400组表现，不能证明全局最优。',
        '- 固定秒预算下，系统负载影响候选覆盖。L2家族胜出不能单独证明全部改善来自L2。',
        '- RESULT.md是本报告；REPORT.md/COMPARISON.md是通用输出；sampling.json、STRATIFIED_AUDIT.json、history.json保留完整来源与过程。', '',
        '复现命令（将输出目录改为新目录）：', '', '```powershell',
        ' '.join(f'"{v}"' if ' ' in v else v for v in manifest['command']), '```', '']
    (output/'RESULT.md').write_text('\n'.join(lines), encoding='utf-8')
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--warm-start', type=Path, required=True)
    parser.add_argument('--reference-runs', nargs='+', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=20260927)
    parser.add_argument('--per-core', type=int, default=5)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--max-evaluations', type=int, default=64)
    parser.add_argument('--evaluation-timeout', type=float, default=600)
    parser.add_argument('--q1-source', type=Path, default=DEFAULT_Q1_SOURCE)
    parser.add_argument('--config', type=Path, default=ROOT/'official/data/config.txt')
    args = parser.parse_args(argv)
    if (args.workers < 1 or args.per_core < 1 or args.max_evaluations < 3 or
            not math.isfinite(args.seconds) or args.seconds <= 0 or
            not math.isfinite(args.evaluation_timeout) or args.evaluation_timeout <= 0):
        parser.error('invalid count or evaluation budget')
    warm, config, output = args.warm_start.resolve(), args.config.resolve(), args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    catalog, reference_files = load_references([p.resolve() for p in args.reference_runs], warm, config)
    population, selected = stratified_sample(catalog, args.per_core, args.seed)
    sampling = dict(seed=args.seed, per_core=args.per_core, population=population,
        population_pairs=len(catalog), selected=[dict(case=c, cores=n) for c,n in selected],
        rule='Random(seed).sample(sorted(common_cases), 4*per_core); successive chunks assigned to N2,N3,N4,N5; no duplicate cases; no score access')
    write_json(output/'sampling.json', sampling)
    print('SAMPLE '+json.dumps(sampling['selected']), flush=True)
    settings = dict(seconds=args.seconds, max_evaluations=args.max_evaluations,
        evaluation_timeout=args.evaluation_timeout, enable_l2=True, near_bound_fraction=.02)
    hardware_settings, official_settings = hardware(), read_settings(config)
    items = []
    for pair in selected:
        item = check_reference(catalog[pair], warm, config, official_settings)
        copied = output/'inherited_plans'/item['case']/f"N{item['num_cores']}"/'plan.json'
        copied.parent.mkdir(parents=True)
        copied.write_bytes(Path(item['original_warm_plan']).read_bytes())
        if sha256(copied) != item['plan_sha256']:
            raise ValueError('warm source changed while being captured')
        item['warm_plan'] = str(copied)
        items.append(item)
    snapshot = snapshot_source(args.q1_source, output/'q1_source_snapshot')
    write_json(output/'reference_inputs.json', dict(items=items, reference_metadata_sha256=reference_files))
    sources = list((ROOT/'src/q3v3').glob('*.py')) + list((ROOT/'official/code').glob('*.py'))
    sources += [Path(__file__), ROOT/'src/solve_q3v3.py', ROOT/'src/report_q3v3.py']
    hashes = {str(p.resolve().relative_to(ROOT)):sha256(p) for p in sources}
    workers = min(args.workers, len(items))
    manifest = dict(algorithm=ALGORITHM_NAME, status='running', started_at=datetime.now(timezone.utc).isoformat(),
        graphs=sorted({i['graph'] for i in items}), warm_start=str(warm), config=str(config),
        q1_source=snapshot['snapshot_source_root'], q1_source_original=str(args.q1_source.resolve()),
        q1_snapshot=snapshot, hardware=hardware_settings, workers=workers, simulated_cores=list(CORES),
        settings=settings, sampling=sampling, source_hashes=hashes, singlecore_score_used=False,
        command=[sys.executable,str(Path(__file__).resolve()),*(sys.argv[1:] if argv is None else argv)],
        expected=len(items), completed=0, preparation_wall_seconds=time.perf_counter()-started)
    write_json(output/'run_info.json', manifest)
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_pair, dict(item=i,output=str(output),config=str(config),
                   q1_source=manifest['q1_source'],settings=settings)):i for i in items}
        for future in as_completed(futures):
            item=futures[future]
            try:
                row=future.result()
            except Exception as exc:
                row=dict(case=item['case'],num_cores=item['num_cores'],status='failed',error=str(exc))
            rows.append(row)
            save_report(output, rows)
            manifest['completed']=sum(r['status']=='ok' for r in rows)
            write_json(output/'run_info.json',manifest)
            print(json.dumps(dict(completed=len(rows),case=row['case'],cores=row['num_cores'],
                status=row['status'],initial=row.get('supplied_plan_makespan'),final=row.get('makespan_cycles'),
                seconds=row.get('total_seconds'))),flush=True)
    unchanged = all(sha256(ROOT/name)==digest for name,digest in hashes.items())
    success=len(rows)==len(items) and all(r['status']=='ok' for r in rows) and unchanged
    manifest.update(status='complete' if success else 'failed', elapsed_seconds=time.perf_counter()-started,
                    sources_unchanged=unchanged)
    write_json(output/'run_info.json',manifest)
    write_json(output/'timing.json',dict(batch_wall_seconds=manifest['elapsed_seconds'],workers=workers,
        snapshot_seconds=snapshot['capture_seconds'],
        cases=[dict(case=r['case'],cores=r['num_cores'],total_seconds=r.get('total_seconds')) for r in rows]))
    if not success:
        return 1
    audit=finish_report(output,rows,manifest,items,reference_files)
    print(json.dumps(dict(audit=audit['status'],groups=audit['groups'],output=str(output))),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
