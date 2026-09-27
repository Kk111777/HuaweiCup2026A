"""Q3 full-solution portfolio, capacity repair and guarded time/traffic tradeoffs."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import ctypes
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback

from q3v3.official import ROOT, sha256, write_json
from q3v3 import ALGORITHM_NAME, ALGORITHM_REVISION
from q3v3.objective import DEFAULT_POLICY, POLICIES, describe
from q3v3.q1_compat import DEFAULT_Q1_SOURCE, snapshot_source
from q3v3.search import find_warm_plan, solve_one


def hardware():
    info = dict(logical_cpus=os.cpu_count() or 1, available_memory_gib=None)
    if os.name == 'nt':
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ('total', 'available', 'page', 'available_page',
                                                       'virtual', 'available_virtual', 'extended')]
        memory = Memory()
        memory.length = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            info.update(total_memory_gib=memory.total/2**30, available_memory_gib=memory.available/2**30)
    return info


def solve_case(request):
    graph, output = Path(request['graph']), Path(request['output'])
    previous, rows = None, []
    for n in request['cores']:
        try:
            summary, previous = solve_one(graph, n, request['config'], output/graph.stem/f'N{n}',
                request['q1_source'], warm_plan=find_warm_plan(request['warm_start'], graph.stem, n),
                previous_plan=previous, seconds=request['seconds'], max_evaluations=request['max_evaluations'],
                enable_l2=request['enable_l2'], near_bound_fraction=request['near_bound_fraction'],
                evaluation_timeout=request['evaluation_timeout'],
                enable_global=request.get('enable_global', True), enable_joint=request.get('enable_joint', True),
                enable_structural=request.get('enable_structural', True),
                objective_policy=request.get('objective_policy', DEFAULT_POLICY),
                seed_policy=request.get('seed_policy', 'auto'),
                q2_warm_plan=find_warm_plan(request.get('q2_warm_start'),graph.stem,n),
                reference_plan=find_warm_plan(request.get('reference_start'),graph.stem,n),
                cycle_tolerance=request.get('cycle_tolerance',.01),
                traffic_weight=request.get('traffic_weight',.05),added_share=request.get('added_share',.25))
            rows.append(summary)
        except Exception as exc:
            row = dict(case=graph.stem, num_cores=n, status='failed', error=str(exc), traceback=traceback.format_exc())
            write_json(output/graph.stem/f'N{n}'/'failure.json', row)
            rows.append(row)
    return rows


def save_report(output, rows):
    rows.sort(key=lambda r: (r['case'], r['num_cores']))
    write_json(output/'summary.json', rows)
    lines = [f'# {ALGORITHM_NAME} 核调度与 L2 搜索结果', '',
        '每一行的初始方案与最终方案均在相同官方 Q3 配置下重新评估。单核成绩不参与特征、预算或决策。', '',
        '|case|核数|状态|起点 cycles|最终 cycles|较起点降低|L2 字节命中率|搜索调用|准备秒|搜索秒|复核秒|合计秒|停止原因|',
        '|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for r in rows:
        if r['status'] == 'ok':
            hit = r['cache_hit_rate_bytes']
            lines.append(f"|{r['case']}|{r['num_cores']}|ok|{r['seed_makespan']}|{r['makespan_cycles']}|"
                         f"{r['reduction_from_seed_percent']:.2f}%|{100*hit:.2f}%|{r['official_calls']}|"
                         f"{r['preparation_seconds']:.2f}|{r['search_seconds']:.2f}|{r['final_replay_seconds']:.2f}|"
                         f"{r['total_seconds']:.2f}|{r['stop_reason']}|")
        else:
            lines.append(f"|{r['case']}|{r['num_cores']}|failed|—|—|—|—|—|—|—|—|—|{r['error']}|")
    lines += ['', '完整方案、候选履历和独立官方 replay 位于各 case/N* 目录。',
              '默认 cycles-traffic：最多退让 1% cycles，以固定归一化时间/搬运目标择优；另存 fastest/plan.json。',
              'cycles-hit / cycles-copy 是保留的历史严格目标；实际参数及归一化参考值见每行 summary。',
              'lower_bound 是纯计算下界，不代表搬运优化已结束。',
              '搜索预算按每个核数计算，输入准备、起点核验、最终重放单独计时；官方候选具有独立子进程超时。',
              '物理 DDR 与官方 added_copy_bytes 分列。后者不会因 Cache 命中而扣除。', '']
    (output/'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=Path, default=ROOT/'official/data')
    parser.add_argument('--cases', nargs='+', type=int)
    parser.add_argument('--cores', '-n', nargs='+', type=int, default=[2,3,4,5])
    parser.add_argument('--workers', type=int, help='Host processes across cases, distinct from simulated --cores.')
    parser.add_argument('--q1-source', type=Path, default=DEFAULT_Q1_SOURCE)
    parser.add_argument('--warm-start', type=Path, help='Optional plan file or case_XXX/Nn result directory.')
    parser.add_argument('--q2-warm-start', type=Path, help='Complete Q2 plans, freshly evaluated in Q3.')
    parser.add_argument('--reference-start', type=Path, help='Optional previous Q3 plans as explicit inherited safeguards.')
    parser.add_argument('--seed-policy', choices=('auto', 'portfolio'), default='auto',
                        help='auto: full warm plan needs no Q1 source; portfolio also imports a graph-only seed.')
    parser.add_argument('--objective', dest='objective_policy', choices=POLICIES, default='cycles-traffic',
                        help='Default: guarded normalized time/DDR/added COPY; legacy strict policies remain available.')
    parser.add_argument('--cycle-tolerance',type=float,default=.01,help='Maximum relative cycles increase vs fastest found.')
    parser.add_argument('--traffic-weight',type=float,default=.05,help='Weight of normalized traffic in the final score.')
    parser.add_argument('--added-share',type=float,default=.25,help='Traffic mixture: this share added COPY, remainder physical DDR.')
    parser.add_argument('--config', type=Path, default=ROOT/'official/data/config.txt')
    parser.add_argument('--output-dir', type=Path, default=None)
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--max-evaluations', type=int, default=64)
    parser.add_argument('--evaluation-timeout', type=float, default=120)
    parser.add_argument('--near-bound-fraction', type=float, default=.02)
    parser.add_argument('--no-l2', action='store_true', help='Disable L2-specific candidate families, keep Q3 evaluator.')
    parser.add_argument('--no-global', action='store_true', help='Ablate independent whole-graph construction.')
    parser.add_argument('--no-joint', action='store_true', help='Ablate joint region regrouping/core/order search.')
    parser.add_argument('--no-structural', action='store_true', help='Ablate Q1v3-derived L2-aware unit rebuilding.')
    args = parser.parse_args(argv)
    if any(n not in (1,2,3,4,5) for n in args.cores) or len(set(args.cores)) != len(args.cores):
        parser.error('cores must be distinct values in 1 through 5')
    full_roots=[p for p in (args.warm_start,args.q2_warm_start,args.reference_start) if p is not None]
    if 1 in args.cores and (not full_roots or args.seed_policy != 'auto'):
        parser.error('N1 requires a complete --warm-start with --seed-policy auto')
    if (not math.isfinite(args.seconds) or args.seconds <= 0 or args.max_evaluations < 3
            or not math.isfinite(args.evaluation_timeout) or args.evaluation_timeout <= 0
            or not 0 <= args.near_bound_fraction < 1):
        parser.error('positive finite budgets, max-evaluations >= 3, and 0 <= near-bound-fraction < 1 required')
    if args.workers is not None and args.workers < 1:
        parser.error('workers must be positive')
    from q3v3.traffic_objective import rational
    try:
        for name in ('cycle_tolerance','traffic_weight','added_share'): rational(getattr(args,name),name)
    except ValueError as exc:
        parser.error(str(exc))
    if args.objective_policy!='cycles-traffic' and (args.q2_warm_start or args.reference_start):
        parser.error('Q2/reference portfolio requires --objective cycles-traffic')
    source = args.input.resolve()
    if source.is_file():
        if args.cases:
            parser.error('single input file cannot be combined with --cases')
        graphs = [source]
    else:
        graphs = ([source/f'case_{n:03d}.json' for n in sorted(set(args.cases))]
                  if args.cases else sorted(source.glob('case_*.json')))
    if not graphs or any(not p.is_file() for p in graphs):
        parser.error('input graphs not found')
    for root in full_roots:
        if not root.exists(): parser.error(f'warm-start path does not exist: {root}')
        if root.is_file() and (len(graphs)!=1 or len(args.cores)!=1):
            parser.error('a single warm-start plan requires one graph and one core count')
        if any(find_warm_plan(root,p.stem,n) is None for p in graphs for n in args.cores):
            parser.error(f'warm-start directory must cover every requested case/core: {root}')
    output = (args.output_dir or ROOT/'outputs'/f'q3v3_memory_traffic_{datetime.now():%Y%m%d_%H%M%S}').resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    use_bridge = (not full_roots or args.seed_policy=='portfolio') if args.objective_policy=='cycles-traffic' else (args.warm_start is None or args.seed_policy=='portfolio')
    snapshot = snapshot_source(args.q1_source, output/'q1_source_snapshot') if use_bridge else None
    info = hardware()
    memory_workers = max(1, int((info['available_memory_gib']-8)//3)) if info['available_memory_gib'] else 4
    workers = min(len(graphs), args.workers or min(8, max(1,info['logical_cpus']//2), memory_workers))
    sources = sorted((ROOT/'src/q3v3').glob('*.py')) + [Path(__file__).resolve()]
    official_sources = sorted((ROOT/'official/code').glob('*.py'))
    hashes = {str(p.relative_to(ROOT)):sha256(p) for p in sources+official_sources}
    manifest = dict(algorithm=ALGORITHM_NAME, algorithm_revision=ALGORITHM_REVISION,
        status='running', hardware=info, workers=workers, simulated_cores=sorted(args.cores),
        graphs=[str(p) for p in graphs], q1_source=snapshot['snapshot_source_root'] if snapshot else None,
        q1_source_original=str(args.q1_source.resolve()) if snapshot else None, q1_snapshot=snapshot,
        objective_priority=describe(args.objective_policy),
        warm_start=str(args.warm_start.resolve()) if args.warm_start else None,
        q2_warm_start=str(args.q2_warm_start.resolve()) if args.q2_warm_start else None,
        reference_start=str(args.reference_start.resolve()) if args.reference_start else None,
        config=str(args.config.resolve()), settings=dict(seconds=args.seconds,max_evaluations=args.max_evaluations,
        enable_l2=not args.no_l2, enable_global=not args.no_global, enable_joint=not args.no_joint,
        enable_structural=not args.no_structural,
        objective_policy=args.objective_policy, seed_policy=args.seed_policy,
        cycle_tolerance=args.cycle_tolerance,traffic_weight=args.traffic_weight,added_share=args.added_share,
        near_bound_fraction=args.near_bound_fraction,evaluation_timeout=args.evaluation_timeout),
        source_hashes=hashes, command=[sys.executable]+sys.argv, singlecore_score_used=False)
    write_json(output/'run_info.json', manifest)
    rows = []
    requests = [dict(graph=str(p),output=str(output),cores=sorted(args.cores),config=str(args.config.resolve()),
        q1_source=manifest['q1_source'],warm_start=manifest['warm_start'],
        q2_warm_start=manifest['q2_warm_start'],reference_start=manifest['reference_start'],**manifest['settings']) for p in graphs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(solve_case,r):r for r in requests}
        for future in as_completed(futures):
            request = futures[future]
            try:
                finished = future.result()
            except Exception as exc:
                finished = [dict(case=Path(request['graph']).stem,num_cores=n,status='failed',error=str(exc))
                            for n in request['cores']]
            rows.extend(finished)
            save_report(output, rows)
            print(json.dumps([dict(case=r['case'],cores=r['num_cores'],status=r['status'],
                              makespan=r.get('makespan_cycles'),gain=r.get('reduction_from_seed_percent'))
                             for r in finished]), flush=True)
    unchanged = all(sha256(ROOT/name) == digest for name,digest in hashes.items())
    good = sum(r['status']=='ok' for r in rows)
    manifest.update(status='complete' if good==len(graphs)*len(args.cores) and unchanged else 'failed',
                    completed=good,expected=len(graphs)*len(args.cores),elapsed_seconds=time.perf_counter()-started,
                    sources_unchanged=unchanged)
    write_json(output/'run_info.json', manifest)
    # Wall-clock totals are not the sum of concurrent per-case times.
    timing = dict(batch_wall_seconds=manifest['elapsed_seconds'], snapshot_seconds=snapshot['capture_seconds'] if snapshot else 0,
        workers=workers, cases=[dict(case=p.stem, completed=sum(r['status']=='ok' for r in rows if r['case']==p.stem),
            total_seconds=sum(r.get('total_seconds',0) for r in rows if r['case']==p.stem),
            search_seconds=sum(r.get('search_seconds',0) for r in rows if r['case']==p.stem)) for p in graphs])
    write_json(output/'timing.json',timing)
    print(str(output), flush=True)
    return 0 if manifest['status']=='complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
