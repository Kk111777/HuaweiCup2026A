"""Export a frozen, explicitly partial snapshot while the original batch continues."""
from datetime import datetime, timezone
import csv
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
import zipfile
from package_results import OUTPUT, ROOT, read, digest, table


def main():
    captured = datetime.now(timezone.utc)
    snapshot = OUTPUT / ('snapshot_' + captured.astimezone().strftime('%Y%m%d_%H%M%S'))
    snapshot.mkdir(exist_ok=False)
    markers = [(i, n, OUTPUT / f'case_{i:03d}' / f'N{n}')
               for i in range(1, 101) for n in (2, 3, 4, 5)
               if (OUTPUT / f'case_{i:03d}' / f'N{n}' / 'complete.json').is_file()]
    baseline_cache, rows, schedules = {}, [], []
    for i, n, directory in markers:
        case = f'case_{i:03d}'
        complete = read(directory / 'complete.json')
        for name, expected in complete['artifacts_sha256'].items():
            assert digest(directory / name) == expected, (case, n, name)
        plan, result, metrics = [read(directory / name) for name in ('plan.json', 'result.json', 'metrics.json')]
        if case not in baseline_cache:
            source = ROOT / 'results/problem_1_singlecore' / case / f'{case}_singlecore_res.json'
            baseline_cache[case] = read(source)['makespan'], digest(source)
        baseline, baseline_hash = baseline_cache[case]
        assert baseline_hash == metrics['baseline_ref']['result_sha256']
        assert baseline == metrics['singlecore_makespan_cycles']
        assert set(plan) == {'node_to_subgraph', 'core_schedules'}
        assert len(plan['core_schedules']) == result['num_cores'] == n
        assert result['makespan'] == metrics['makespan_cycles']
        ratio = baseline / result['makespan']
        assert math.isclose(ratio, metrics['speedup'], rel_tol=1e-14)
        destination = snapshot / f'N{n}' / f'{case}_plan.json'
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(directory / 'plan.json', destination)
        assert digest(destination) == complete['artifacts_sha256']['plan.json']
        tasks = [t for core in plan['core_schedules'] for t in core]
        assert len(tasks) == len(set(tasks))
        assert set(tasks) == set(plan['node_to_subgraph'].values())
        for core in result['per_core_timeline']:
            assert [t['task_id'] for t in core['tasks']] == plan['core_schedules'][core['core_id']]
            for order, task in enumerate(core['tasks']):
                schedules.append(dict(case=case, num_cores=n, core_id=core['core_id'], order=order,
                                      subgraph_id=task['subgraph_id'], start_cycles=task['start'],
                                      end_cycles=task['end'], duration_cycles=task['duration']))
        rows.append(dict(case=case, num_cores=n, baseline_cycles=baseline,
                         makespan_cycles=result['makespan'], speedup=ratio,
                         mode=metrics['mode'], beta=metrics['beta'], task_count=metrics['task_count'],
                         elapsed_seconds=metrics['elapsed_seconds'],
                         plan=destination.relative_to(snapshot).as_posix()))
    sets = {n: {r['case'] for r in rows if r['num_cores'] == n} for n in (2, 3, 4, 5)}
    common = set.intersection(*sets.values())
    means = []
    for n, cases in sets.items():
        selected = [r for r in rows if r['num_cores'] == n]
        ratios = [r['speedup'] for r in selected]
        common_ratios = [r['speedup'] for r in selected if r['case'] in common]
        means.append(dict(num_cores=n, completed_cases=len(cases), expected_cases=100,
                          mean_speedup_completed=statistics.mean(ratios),
                          common_cases=len(common), mean_speedup_common_cases=statistics.mean(common_ratios),
                          faster_cases=sum(x > 1 for x in ratios), equal_cases=sum(x == 1 for x in ratios),
                          slower_cases=sum(x < 1 for x in ratios)))
    timings = [read(OUTPUT / f'case_{i:03d}' / 'timing.json') for i in range(1, 101)]
    timing_rows = []
    for t in timings:
        timing_rows.append(dict(case=t['case'], status=t['status'], started_at_utc=t['started_at_utc'],
                               finished_at_utc=t.get('finished_at_utc'), wall_seconds=t.get('wall_seconds'),
                               cpu_seconds=t.get('cpu_seconds'),
                               **{f'N{n}_seconds': t.get('core_elapsed_seconds', {}).get(str(n))
                                  for n in (2, 3, 4, 5)}))
    table(snapshot / 'speedups.csv', rows)
    table(snapshot / 'average_speedups.csv', means)
    table(snapshot / 'subgraph_schedules.csv', schedules)
    table(snapshot / 'case_timings.csv', timing_rows)
    missing = [dict(case=f'case_{i:03d}', num_cores=n) for i in range(1, 101)
               for n in (2, 3, 4, 5) if f'case_{i:03d}' not in sets[n]]
    metadata = dict(status='partial_snapshot', captured_at_utc=captured.isoformat(),
                    verified_plans=len(rows), complete_cases=len(common), expected_cases=100,
                    missing=missing, means=means, batch_elapsed_seconds=read(OUTPUT / 'run_info.json')['elapsed_seconds'])
    (snapshot / 'snapshot_info.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    lines = ['# 题目一阶段结果（非最终全量版）', '',
             f'截取时间：{captured.astimezone().isoformat()}。完整 case {len(common)}/100；已验证方案 {len(rows)}/400。',
             '原始批处理继续运行；本目录为只读结果快照，不用于续跑。',
             'N2–N5 中 JSON 含 node_to_subgraph（计算节点划分）与 core_schedules（各核心子图执行顺序）。',
             'subgraph_schedules.csv 展开核心、顺序和官方模拟的开始/结束 cycles。编号从 0 开始。',
             'speedups.csv：逐例加速比 = 已校验历史单核 makespan / 当前多核 makespan。小于1的结果按原值保留。',
             'average_speedups.csv 同时给出各核数已完成样本的算术平均、四种核数共同完成样本的算术平均；不把缺失样本当成0。',
             'case_timings.csv：100行；已结束case有完整wall_seconds、cpu_seconds与四个阶段耗时，仍在运行的case总耗时留空。',
             'Python运行秒数不参与加速比计算。最终全量结果完成后单独交付。', '',
             '| 核数 | 已完成/100 | 已完成样本平均加速比 | 共同完成99例平均加速比 |',
             '|---|---:|---:|---:|']
    for m in means:
        lines.append(f"| {m['num_cores']} | {m['completed_cases']} | {m['mean_speedup_completed']:.6f} | {m['mean_speedup_common_cases']:.6f} |")
    lines += ['', '未完成：' + ', '.join(f"{m['case']}/N{m['num_cores']}" for m in missing),
              '', '已检查所有完成标记中的产物SHA-256、基线引用和cycles、方案复制一致性以及核心队列与执行时间线一致性。',
              '仅生成本地文件，没有提交、推送或上传。']
    (snapshot / 'README.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    manifest = {p.relative_to(snapshot).as_posix(): digest(p) for p in sorted(snapshot.rglob('*')) if p.is_file()}
    (snapshot / 'manifest.sha256').write_text(''.join(f'{h}  {p}\n' for p, h in manifest.items()), encoding='utf-8')
    archive = snapshot.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as z:
        for path in sorted(snapshot.rglob('*')):
            if path.is_file():
                z.write(path, path.relative_to(snapshot).as_posix())
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for name, expected in manifest.items():
            assert hashlib.sha256(z.read(name)).hexdigest() == expected
    audit = dict(**metadata, snapshot_directory=str(snapshot), zip_path=str(archive),
                 zip_sha256=digest(archive), zip_bytes=archive.stat().st_size, verified=True)
    snapshot.with_suffix('.audit.json').write_text(json.dumps(audit, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
