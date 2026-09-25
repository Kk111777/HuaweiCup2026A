"""Audit and package the completed run without changing any solver decisions."""
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import statistics
import zipfile

OUTPUT = Path(__file__).resolve().parent
ROOT = OUTPUT.parents[1]


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def table(path, rows, fields=None):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    info = read(OUTPUT / 'run_info.json')
    assert info['status'] == 'complete', 'Refusing to package an incomplete experiment'
    assert read(OUTPUT / 'audit.json')['complete']
    package = OUTPUT / 'submission'
    package.mkdir(exist_ok=False)
    result_rows, schedules, files = [], [], {}
    for i in range(1, 101):
        case = f'case_{i:03d}'
        baseline_file = ROOT / 'results/problem_1_singlecore' / case / f'{case}_singlecore_res.json'
        baseline_cycles = read(baseline_file)['makespan']
        for n in (2, 3, 4, 5):
            directory = OUTPUT / case / f'N{n}'
            complete = read(directory / 'complete.json')
            # Audit source artifacts again immediately before creating submission copies.
            for name, expected in complete['artifacts_sha256'].items():
                assert digest(directory / name) == expected, (case, n, name)
            plan, result, metrics = [read(directory / name) for name in ('plan.json', 'result.json', 'metrics.json')]
            assert set(plan) == {'node_to_subgraph', 'core_schedules'}
            assert len(plan['core_schedules']) == result['num_cores'] == n
            assert digest(baseline_file) == metrics['baseline_ref']['result_sha256']
            assert metrics['singlecore_makespan_cycles'] == baseline_cycles
            assert result['makespan'] == metrics['makespan_cycles']
            speedup = baseline_cycles / result['makespan']
            assert math.isclose(speedup, metrics['speedup'], rel_tol=1e-14)
            destination = package / f'N{n}' / f'{case}_plan.json'
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(directory / 'plan.json', destination)
            assert digest(destination) == complete['artifacts_sha256']['plan.json']
            files[destination.relative_to(package).as_posix()] = digest(destination)
            task_ids = [task for core in plan['core_schedules'] for task in core]
            assert len(task_ids) == len(set(task_ids))
            assert set(task_ids) == set(plan['node_to_subgraph'].values())
            for core in result['per_core_timeline']:
                assert [t['task_id'] for t in core['tasks']] == plan['core_schedules'][core['core_id']]
                for order, task in enumerate(core['tasks']):
                    schedules.append(dict(case=case, num_cores=n, core_id=core['core_id'],
                                          order=order, subgraph_id=task['subgraph_id'],
                                          start_cycles=task['start'], end_cycles=task['end'],
                                          duration_cycles=task['duration']))
            result_rows.append(dict(case=case, num_cores=n, baseline_cycles=baseline_cycles,
                                    makespan_cycles=result['makespan'], speedup=speedup,
                                    mode=metrics['mode'], beta=metrics['beta'],
                                    task_count=metrics['task_count'],
                                    elapsed_seconds=metrics['elapsed_seconds'],
                                    plan=destination.relative_to(package).as_posix()))
    assert len(result_rows) == len(files) == 400
    averages = []
    for n in (2, 3, 4, 5):
        rows = [r for r in result_rows if r['num_cores'] == n]
        assert len(rows) == 100
        ratios = [r['speedup'] for r in rows]
        averages.append(dict(num_cores=n, cases=100, arithmetic_mean_speedup=statistics.mean(ratios),
                             median_speedup=statistics.median(ratios), min_speedup=min(ratios),
                             max_speedup=max(ratios), faster_cases=sum(s > 1 for s in ratios),
                             equal_cases=sum(s == 1 for s in ratios), slower_cases=sum(s < 1 for s in ratios)))
    table(package / 'speedups.csv', result_rows)
    table(package / 'average_speedups.csv', averages)
    table(package / 'subgraph_schedules.csv', schedules)
    shutil.copyfile(OUTPUT / 'case_timings.csv', package / 'case_timings.csv')
    instructions = '''# 第一问划分方案、子图调度与加速比

N2、N3、N4、N5 各含 case_001_plan.json 至 case_100_plan.json，共 400 份方案。
方案保持原算法输出的官方格式：node_to_subgraph 是计算节点到子图的映射，core_schedules[k] 是核心 k 的子图执行顺序。核心和子图编号从 0 开始；空列表表示空闲核心。
同一方案已由原求解器写入后重读、完整调用官方评估器复评，并核对完整结果一致性。
subgraph_schedules.csv 提供每个子图的核心、队列顺序和官方模拟开始/结束 cycles，便于直接检查排布。
speedups.csv 保存每例、每个核数的结果；average_speedups.csv 对全部 100 例的逐例加速比取算术平均。
加速比 = 已校验单核基线 makespan / 本次方案官方 makespan。小于 1 表示该方案慢于单核，按原值保留。
case_timings.csv 的 wall_seconds 为本次每个 case 完成四个核数的总墙钟时间，cpu_seconds 为该进程 CPU 时间；不含队列等待。N2–N5_seconds 为原求解器的分核阶段计时。
Python 运行秒数不参与模拟加速比计算。单核基线复用已有归档，不把历史耗时算入本次求解耗时。
本次为协作者提交 04634a2400ce9fa0c45084bed06fb4ce72de917d 的 adaptive 算法；20 个 case 进程，每例顺序执行候选。

在协作者工作区中复评一个方案（路径按实际位置替换）：
python official/code/multicore_cut_evaluate_problem_1.py <case_001.json> <N5/case_001_plan.json> --config <config.txt>

此包包含待提交的方案与辅助结果，未向任何平台上传。
'''
    (package / 'README.md').write_text(instructions, encoding='utf-8')
    manifest = {p.relative_to(package).as_posix(): digest(p)
                for p in sorted(package.rglob('*')) if p.is_file()}
    (package / 'manifest.sha256').write_text(''.join(f'{h}  {p}\n' for p, h in manifest.items()), encoding='utf-8')
    archive = OUTPUT / 'problem1_submission_100cases.zip'
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as z:
        for path in sorted(package.rglob('*')):
            if path.is_file():
                z.write(path, path.relative_to(package).as_posix())
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for path, expected in manifest.items():
            assert hashlib.sha256(z.read(path)).hexdigest() == expected
    (OUTPUT / 'submission_audit.json').write_text(json.dumps(dict(
        complete=True, plans=400, cases=100, averages=averages, zip_sha256=digest(archive),
        zip_bytes=archive.stat().st_size, runner_sha256=digest(Path(__file__))), indent=2) + '\n', encoding='utf-8')
    with (OUTPUT / 'report.md').open('a', encoding='utf-8') as stream:
        stream.write('\n## 提交方案与最终加速比\n\n')
        stream.write('已整理 400 份官方格式方案、子图排布表和逐例加速比，见 problem1_submission_100cases.zip。\n\n')
        stream.write('| 模拟核数 | 100例平均加速比 | 快于单核 | 等于单核 | 慢于单核 |\n|---|---:|---:|---:|---:|\n')
        for a in averages:
            stream.write(f"| {a['num_cores']} | {a['arithmetic_mean_speedup']:.6f} | {a['faster_cases']} | {a['equal_cases']} | {a['slower_cases']} |\n")
        stream.write(f'\n压缩包 SHA-256：{digest(archive)}\n')
    print(json.dumps(averages, indent=2))
    print(archive)


if __name__ == '__main__':
    main()
