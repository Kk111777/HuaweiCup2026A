"""Compare complete case-budget outputs with a verified historical run (no solves)."""
import argparse
import csv
from functools import lru_cache
import hashlib
import io
import json
import math
from pathlib import Path
import statistics


ROOT = Path(__file__).resolve().parents[1]
CORES = (2, 3, 4, 5)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


@lru_cache(maxsize=None)
def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def number(record, key, *, required=True, positive=False):
    value = record.get(key)
    if value is None and not required:
        return None
    require(type(value) in (int, float) and math.isfinite(value), f'missing/invalid {key}: {value!r}')
    require(value > 0 if positive else value >= 0, f'negative/zero {key}: {value}')
    return value


def total(values):
    values = list(values)
    return sum(values) if values and all(v is not None for v in values) else None


def indexed(rows, fields, label):
    result = {}
    for row in rows:
        key = tuple(row.get(f) for f in fields)
        require(key not in result, f'{label}: duplicate {key}')
        result[key] = row
    return result


def load_run(path, *, case_budget):
    require(not (path/'interrupted.json').exists(), f'{path}: interrupted trial cannot be reported as a completed run')
    manifest, execution = read(path/'benchmark.json'), read(path/'results.json')
    cases, cores = manifest['cases'], manifest['cores']
    require(cases and all(type(n) is int and 1 <= n <= 100 for n in cases), 'invalid case list')
    require(len(set(cases)) == len(cases), 'duplicate manifest cases')
    require(len(cores) == len(CORES) and set(cores) == set(CORES), 'expected distinct core counts 2,3,4,5')
    require(execution.get('status') == 'complete' and execution.get('coverage_complete') is True,
            f'{path}: batch is not complete')
    require(execution.get('errors') == [], f'{path}: execution errors/missing error record')
    require(execution.get('manifest') == manifest, f'{path}: manifest differs from results.json')
    expected = {(f'case_{c:03d}', n) for c in cases for n in cores}
    rows = indexed(execution['rows'], ('case', 'cores'), str(path))
    require(set(rows) == expected and execution.get('expected_results') == len(expected),
            f'{path}: incomplete or unexpected result rows')
    folders = {(p.parent.name, p.name) for p in path.glob('case_*/N*') if p.is_dir()}
    require(folders == {(c, f'N{n}') for c, n in expected}, f'{path}: unexpected/missing core directories')
    if case_budget:
        require(manifest.get('budget_scope') == 'case_all_cores' and manifest.get('singlecore_in_solver') is False,
                'new run must use the all-core case budget without a singlecore input')
        require(manifest.get('within_case_parallel') is False, 'new run has within-case parallelism')
        case_rows = indexed(execution['case_rows'], ('case',), 'case rows')
        require(set(case_rows) == {(c,) for c, _ in expected}, 'incomplete or unexpected case rows')
    else:
        case_rows = {}
    return manifest, execution, rows, case_rows


def verified_result(folder, expected_source, fingerprint):
    request, summary = read(folder/'request.json'), read(folder/'summary.json')
    marker, saved = read(folder/'verified.json'), read(folder/'evaluation.json')
    actual = read(folder/'official_replay.json')
    require(marker.get('independent_official_replay') is True, f'{folder}: no independent replay')
    require(all(r.get('source_signature') == expected_source for r in (request, summary, marker)),
            f'{folder}: source signature mismatch')
    for key in ('graph', 'config'):
        stamp = request[f'{key}_sha256']
        require(digest(Path(request[key]).resolve()) == stamp == marker.get(f'{key}_sha256'),
                f'{folder}: {key} hash mismatch')
    require(marker.get('official_fingerprint') == fingerprint, f'{folder}: official evaluator changed')
    for key, name in (('plan_sha256', 'plan.json'), ('result_sha256', 'evaluation.json'),
                      ('official_replay_sha256', 'official_replay.json')):
        require(marker.get(key) == digest((folder/name).resolve()), f'{folder}: {name} hash mismatch')
    cycles = number(actual, 'makespan', positive=True)
    solver_cycles = number(saved, 'makespan', positive=True)
    require(marker.get('official_makespan') == cycles, f'{folder}: replay cycle mismatch')
    require(marker.get('solver_makespan') == solver_cycles == summary.get('makespan'),
            f'{folder}: solver cycle mismatch')
    source = saved.get('evaluation_source', 'official')
    require(source in ('official', 'approximate') and source == summary.get('score_source')
            and source == marker.get('solver_score_source'), f'{folder}: score source mismatch')
    if source == 'official':
        require(solver_cycles == cycles and marker.get('exact_result_match') is True,
                f'{folder}: official saved score disagrees with replay')
    require(request['cores'] == int(folder.name[1:]), f'{folder}: requested core mismatch')
    require(actual.get('num_cores') == request['cores'], f'{folder}: replay target core mismatch')
    return dict(request=request, summary=summary, marker=marker, cycles=cycles,
                solver_cycles=solver_cycles, source=source,
                replay_seconds=number(marker, 'replay_seconds'))


def verify_baseline(old, new):
    """Read the hashed historical denominator, never infer it from speedup."""
    baseline = old['summary'].get('baseline')
    require(isinstance(baseline, dict), 'reference baseline missing')
    cycles = number(baseline, 'cycles', positive=True)
    reference = baseline.get('reference', {})
    require(reference.get('result_path') and reference.get('result_sha256'), 'baseline artifact reference missing')
    path = Path(reference['result_path']).resolve()
    require(digest(path) == reference['result_sha256'], f'{path}: baseline hash mismatch')
    require(read(path).get('makespan') == cycles, f'{path}: baseline cycles mismatch')
    for key in ('graph_sha256', 'config_sha256'):
        require(reference.get(key) == old['request'][key] == new['request'][key], f'baseline {key} mismatch')
    report = new['summary'].get('baseline')
    require(isinstance(report, dict) and report.get('mode') == 'reporting_only'
            and report.get('included_in_solver') is False, 'new baseline must be reporting-only')
    require(report.get('cycles') == cycles and report.get('reference') == reference.get('baseline_ref'),
            'old/new baseline denominator or artifact reference differs')
    return cycles


def compare(run, reference):
    manifest, execution, new_rows, case_rows = load_run(run, case_budget=True)
    old_manifest, _, old_rows, _ = load_run(reference, case_budget=False)
    official_files = sorted((ROOT/'official/code').glob('*.py'))
    require(bool(official_files), 'official evaluator files not found')
    fingerprint = hashlib.sha256('\n'.join(f'{p.name}:{digest(p.resolve())}' for p in official_files).encode()).hexdigest()
    rows, cases = [], []
    for case in sorted({c for c, _ in new_rows}):
        request, case_summary = read(run/case/'request.json'), read(run/case/'summary.json')
        root_rows = indexed(case_summary['rows'], ('cores',), f'{case} summary rows')
        require(set(root_rows) == {(n,) for n in CORES}, f'{case}: incomplete root summary')
        require(case_summary.get('coverage_complete') is True and case_summary.get('budget_scope') == 'case_all_cores'
                and request.get('budget_scope') == 'case_all_cores',
                f'{case}: incorrect budget scope/coverage')
        require(case_summary.get('source_signature') == request.get('source_signature') == manifest['source_signature'],
                f'{case}: root source mismatch')
        require(request.get('cores') == manifest['cores'] and case_summary.get('cores') == manifest['cores']
                and request.get('seconds') == manifest['target_seconds'] == case_summary.get('target_seconds'),
                f'{case}: root budget or core list mismatch')
        recorded = {k:v for k,v in case_rows[(case,)].items() if k not in ('case','reused')}
        require(recorded == {k:v for k,v in case_summary.items() if k != 'rows'}, f'{case}: stale case row')
        group, historical = [], []
        for n in CORES:
            key = (case, n)
            require(key in old_rows, f'reference missing {key}')
            old = verified_result(reference/case/f'N{n}', old_manifest['source_signature'], fingerprint)
            new = verified_result(run/case/f'N{n}', manifest['source_signature'], fingerprint)
            for obj, result_row in ((new, new_rows[key]), (old, old_rows[key])):
                require({k:v for k,v in result_row.items() if k not in ('case','cores','reused')} ==
                        {k:v for k,v in obj['summary'].items() if k != 'cores'}, f'{key}: stale per-core result row')
            require(root_rows[(n,)] == new['summary'], f'{key}: stale root summary row')
            require(new['request'].get('budget_scope') == 'case_all_cores'
                    and new['request'].get('case_cores') == manifest['cores']
                    and new['request'].get('seconds') == manifest['target_seconds']
                    and not new['request'].get('test_baseline'), f'{key}: invalid new solve request')
            for field in ('graph_sha256','config_sha256','parameters'):
                require(new['request'][field] == request[field], f'{key}: root/core {field} mismatch')
            base = verify_baseline(old, new)
            historical.append(number(old['summary'], 'cached_baseline_charge_seconds', required=False))
            row = dict(case=case, cores=n, baseline_cycles=base,
                old_official_cycles=old['cycles'], new_official_cycles=new['cycles'],
                old_speedup=base/old['cycles'], new_speedup=base/new['cycles'],
                cycles_change_fraction=new['cycles']/old['cycles']-1,
                speedup_change_fraction=old['cycles']/new['cycles']-1,
                solver_score_source=new['source'], solver_cycles=new['solver_cycles'],
                prediction_error_fraction=new['solver_cycles']/new['cycles']-1,
                old_wall_seconds=number(old['summary'],'wall_seconds'),
                new_wall_seconds=number(new['summary'],'wall_seconds'),
                old_cpu_seconds=number(old['summary'],'cpu_seconds',required=False),
                new_cpu_seconds=number(new['summary'],'cpu_seconds',required=False),
                old_replay_seconds=old['replay_seconds'], new_replay_seconds=new['replay_seconds'],
                new_status=new['summary']['status'], new_hard_timeout=new['summary'].get('hard_timeout'),
                new_stop_reason=new['summary'].get('history_stop') or new['summary'].get('timed_out_phase'))
            require(number(new['summary'],'cached_baseline_charge_seconds') == 0,
                    f'{key}: historical baseline time was charged')
            group.append(row); rows.append(row)
        require(len({r['baseline_cycles'] for r in group}) == 1, f'{case}: baseline differs across cores')
        require(len(set(historical)) == 1, f'{case}: historical baseline seconds differ across cores')
        old_wall = sum(r['old_wall_seconds'] for r in group)
        new_wall = number(case_summary, 'wall_seconds', positive=True)
        require(old_wall > 0, f'{case}: invalid old wall time')
        target = number(case_summary, 'target_seconds', positive=True)
        extension = number(case_summary, 'extension_seconds', positive=True)
        hard = number(case_summary, 'hard_limit_seconds', positive=True)
        require(case_summary.get('exceeded_target') == (new_wall > target)
                and case_summary.get('exceeded_extension') == (new_wall > extension), f'{case}: stale overtime flags')
        cases.append(dict(case=case, old_four_core_wall_seconds=old_wall, new_case_wall_seconds=new_wall,
            wall_time_saved_seconds=old_wall-new_wall, wall_time_saved_fraction=1-new_wall/old_wall,
            old_four_core_cpu_seconds=total(r['old_cpu_seconds'] for r in group),
            new_case_cpu_seconds=number(case_summary,'cpu_seconds',required=False),
            new_shared_preparation_seconds=number(case_summary,'shared_preparation_seconds',required=False),
            old_singlecore_historical_seconds=historical[0],
            new_baseline_seconds=number(case_summary,'baseline_seconds',required=False),
            old_replay_sum_seconds=sum(r['old_replay_seconds'] for r in group),
            new_replay_sum_seconds=sum(r['new_replay_seconds'] for r in group),
            new_target_seconds=target, new_extension_seconds=extension, new_hard_limit_seconds=hard,
            new_extended=case_summary.get('extended'), new_exceeded_target=case_summary['exceeded_target'],
            new_exceeded_extension=case_summary['exceeded_extension'], new_hard_timeout=case_summary.get('hard_timeout'),
            new_status=case_summary['status'], new_coverage_complete=case_summary['coverage_complete']))
    return rows, cases, manifest, old_manifest, execution


def percent(value):
    return '缺失' if value is None else f'{value*100:+.2f}%'


def decimal(value, digits=2):
    return '缺失' if value is None else f'{value:,.{digits}f}'


def report(rows, cases, manifest, old_manifest, execution):
    regressions = [r for r in rows if r['cycles_change_fraction'] > 0]
    worst = max(rows,key=lambda r:r['cycles_change_fraction'])
    old_mean = statistics.mean(r['old_four_core_wall_seconds'] for r in cases)
    new_mean = statistics.mean(r['new_case_wall_seconds'] for r in cases)
    lines = ['# 整 case 共享预算复测报告', '',
        f"覆盖 {len(cases)} 个 case × 4 种核数，共 {len(rows)} 组；全部最终方案的独立官方回放标记、方案/评分文件哈希、输入/配置和官方代码指纹已核验。",
        f"目标为每个 case 四核数合计 {manifest['target_seconds']:g} 秒，包含共享准备和求解内官方评估。平均实际总用时由 {old_mean/60:.2f} 分钟降至 {new_mean/60:.2f} 分钟，累计用时减少 {(1-new_mean/old_mean)*100:.2f}%。",
        f"周期改善 {sum(r['cycles_change_fraction']<0 for r in rows)} 组，持平 {sum(r['cycles_change_fraction']==0 for r in rows)} 组，回退 {len(regressions)} 组；回退超过 1% 的有 {sum(r['cycles_change_fraction']>.01 for r in rows)} 组。最大周期变化：{worst['case']} / {worst['cores']} 核 {percent(worst['cycles_change_fraction'])}。",
        '', '## 加速比', '',
        '采用相同单核分母，每个 case 等权；总体均值是全部 case×核数组合的算术平均。周期变化为新/旧−1，负值更好。', '',
        '| 核数 | 组数 | 旧平均加速比 | 新平均加速比 | 均值变化 |',
        '|---|---:|---:|---:|---:|']
    for n in (*CORES, None):
        group = [r for r in rows if n is None or r['cores']==n]
        old = statistics.mean(r['old_speedup'] for r in group)
        new = statistics.mean(r['new_speedup'] for r in group)
        lines.append(f"| {n if n else '总体'} | {len(group)} | {old:.6f}× | {new:.6f}× | {percent(new/old-1)} |")
    lines += ['', '## 逐 case 用时', '',
        '旧用时为四种核数实际 wall 之和，不加历史单核耗时；新用时来自整 case 入口真实计时。历史基线和离线回放仅另列。', '',
        '| case | 旧四核合计（分钟） | 新整图（分钟） | 节省 | 目标（秒） | 触发延长 | 超目标 | 超延长窗口 | 保护超时 | 历史基线（秒） | 新离线回放累计（秒） |',
        '|---|---:|---:|---:|---:|---|---|---|---|---:|---:|']
    yes = lambda v: '是' if v is True else '否' if v is False else '缺失'
    for r in cases:
        lines.append(f"| {r['case']} | {r['old_four_core_wall_seconds']/60:.2f} | {r['new_case_wall_seconds']/60:.2f} | {percent(r['wall_time_saved_fraction'])} | {r['new_target_seconds']:g} | {yes(r['new_extended'])} | {yes(r['new_exceeded_target'])} | {yes(r['new_exceeded_extension'])} | {yes(r['new_hard_timeout'])} | {decimal(r['old_singlecore_historical_seconds'])} | {r['new_replay_sum_seconds']:.2f} |")
    lines += ['', '## 逐 case、逐核数官方成绩', '',
        '| case | 核数 | 旧官方 cycles | 新官方 cycles | 周期变化 | 旧加速比 | 新加速比 | 求解保存来源 | 求解保存 cycles | 该核求解（秒） | 独立回放（秒） |',
        '|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|']
    for r in rows:
        lines.append(f"| {r['case']} | {r['cores']} | {r['old_official_cycles']:,} | {r['new_official_cycles']:,} | {percent(r['cycles_change_fraction'])} | {r['old_speedup']:.6f}× | {r['new_speedup']:.6f}× | {r['solver_score_source']} | {r['solver_cycles']:,} | {r['new_wall_seconds']:.2f} | {r['new_replay_seconds']:.2f} |")
    lines += ['', '## 口径与边界', '',
        f"- 旧批次为 {old_manifest['local_test_workers']} 个本地 case worker，新批次为 {manifest['local_test_workers']} 个；这不是严格相同硬件负载的速度对照。并发资源竞争影响 wall 和限时内完成的工作量，不能将全部时间节省归因为代码效率提升。",
        '- 两批都是每个 case 内多核数串行。旧版本每种核数单独分配预算，新版本共享整图预算；探索量改变，成绩优劣必须用本表的官方回放结果判断。',
        '- 单核归档仅在新求解结束后作报告分母，不输入候选生成、预算或近似校准；历史单核耗时不加进主用时比较。',
        '- 近似保存值单独保留。官方成绩、加速比和变化幅度只采用最终已保存方案的独立回放值，没有重新择优或替换最终方案。',
        '- 独立回放是求解之后的离线评分，用时另列；其累计调用秒数不是并行回放批次的墙钟时间。',
        '- 新整图时间包含共享准备与监督开销，不能用每核时间之和替代；CPU 与 wall 含义不同，缺失数据不会填成零。',
        f"- 新求解批次墙钟：{number(execution,'elapsed_seconds'):.2f} 秒。该值与各 case 用时累计不同，并包含批测组织开销。",
        f"- 新求解器指纹：`{manifest['source_signature']}`；旧求解器指纹：`{old_manifest['source_signature']}`。",
        '', '数据文件：[逐核结果](summary.csv) · [整 case 用时](case_comparison.csv)', '']
    return '\n'.join(lines)


def write_csv(path, rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream,fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    atomic_text(path,stream.getvalue(),encoding='utf-8-sig')


def atomic_text(path, text, encoding='utf-8'):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(text,encoding=encoding,newline='')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--reference',type=Path,required=True)
    args = parser.parse_args()
    run, reference = args.run.resolve(), args.reference.resolve()
    require(run != reference, 'new run and reference must differ')
    rows,cases,manifest,old_manifest,execution = compare(run,reference)
    text = report(rows,cases,manifest,old_manifest,execution)
    write_csv(run/'summary.csv',rows)
    write_csv(run/'case_comparison.csv',cases)
    atomic_text(run/'REPORT.md',text)
    print(json.dumps(dict(cases=len(cases),official_pairs=len(rows),
        old_mean_case_seconds=statistics.mean(r['old_four_core_wall_seconds'] for r in cases),
        new_mean_case_seconds=statistics.mean(r['new_case_wall_seconds'] for r in cases),
        old_mean_speedup=statistics.mean(r['old_speedup'] for r in rows),
        new_mean_speedup=statistics.mean(r['new_speedup'] for r in rows),
        cycle_regressions=sum(r['cycles_change_fraction']>0 for r in rows),
        report=str(run/'REPORT.md')),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
