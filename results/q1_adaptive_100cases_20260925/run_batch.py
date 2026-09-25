"""Instrument the unchanged collaborator solver; run from its isolated worktree."""
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import csv
import ctypes
import io
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time
import traceback
import winreg

OUTPUT = Path(__file__).resolve().parent
ROOT = OUTPUT.parents[1]
PROJECT = ROOT.parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from q1 import run as runner
from q1 import official
from common.baseline import load_baseline_catalog
from problem_1_baseline import output_lock

CORES = [2, 3, 4, 5]
WORKERS = 20
DATA = PROJECT / 'official' / 'data'
CONFIG = DATA / 'config.txt'


def now():
    return datetime.now(timezone.utc).isoformat()


def hardware():
    class Memory(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in (
                'total', 'available', 'page', 'available_page', 'virtual',
                'available_virtual', 'extended')]
    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ctypes.WinError()
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                       r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
        cpu = winreg.QueryValueEx(key, 'ProcessorNameString')[0]
    return dict(cpu=cpu, logical_cpus=os.cpu_count(), total_memory_gib=memory.total / 2**30,
                available_memory_gib=memory.available / 2**30)


def timed_case(graph, baseline):
    case = Path(graph).stem
    directory = OUTPUT / case
    directory.mkdir(parents=True, exist_ok=True)
    start, cpu_start = time.perf_counter(), time.process_time()
    timing = dict(case=case, status='running', pid=os.getpid(), started_at_utc=now(),
                  candidate_jobs=1, beta_search='adaptive', num_cores=CORES)
    runner.write_json(directory / 'timing.json', timing)
    rows = []
    try:
        with (directory / 'console.log').open('w', encoding='utf-8', buffering=1) as log:
            with redirect_stdout(log), redirect_stderr(log):
                rows = runner.run_case(graph, str(CONFIG), str(OUTPUT), CORES,
                                       False, 1, baseline, beta_search='adaptive')
        successful = {r['num_cores'] for r in rows if r['status'] == 'success'}
        timing['status'] = 'success' if successful == {1, *CORES} else 'failed'
    except Exception:
        timing.update(status='failed', error=traceback.format_exc())
    finally:
        timing.update(finished_at_utc=now(), wall_seconds=time.perf_counter() - start,
                      cpu_seconds=time.process_time() - cpu_start,
                      core_elapsed_seconds={str(r['num_cores']): r.get('elapsed_seconds')
                                            for r in rows if r['num_cores'] != 1})
        runner.write_json(directory / 'timing.json', timing)
        runner.write_json(directory / 'rows.json', rows)
    return rows, timing


def save_timings(timings):
    fields = ['case', 'status', 'started_at_utc', 'finished_at_utc', 'wall_seconds',
              'cpu_seconds', 'N2_seconds', 'N3_seconds', 'N4_seconds', 'N5_seconds', 'pid', 'error']
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fields, lineterminator='\n', extrasaction='ignore')
    writer.writeheader()
    for timing in sorted(timings, key=lambda t: t['case']):
        writer.writerow({**timing, **{f'N{n}_seconds': timing['core_elapsed_seconds'].get(str(n))
                                     for n in CORES}})
    runner.atomic_text(OUTPUT / 'case_timings.csv', '\ufeff' + buffer.getvalue())


def main():
    with output_lock(OUTPUT):
        if (OUTPUT / 'run_info.json').exists():
            raise RuntimeError('This is a fresh experiment; refusing to overwrite an existing run.')
        started = time.perf_counter()
        graphs = sorted(DATA.glob('case_*.json'))
        assert [g.stem for g in graphs] == [f'case_{n:03d}' for n in range(1, 101)]
        info = dict(status='preparing', started_at_utc=now(), pid=os.getpid(),
                    hardware=hardware(), workers=WORKERS, candidate_jobs=1,
                    cores=CORES, beta_search='adaptive', expected_cases=100,
                    collaborator_commit='04634a2400ce9fa0c45084bed06fb4ce72de917d',
                    python=sys.version, executable=sys.executable, platform=platform.platform(),
                    random_seed=None, random_note='Algorithm is deterministic and uses no RNG.',
                    concurrency_reason='20 CPU processes on 24 logical CPUs; one candidate at a time per case.',
                    timing_scope='wall_seconds: worker start through solver outputs, excludes queue wait and final timing bookkeeping; cpu_seconds: same process CPU time; Nn_seconds: unchanged solver metrics, excludes shared graph setup and completion hashing; baseline is reused and not timed as fresh work.',
                    runner_sha256=runner.file_hash(Path(__file__)),
                    code_sha256={str(p.relative_to(ROOT)): runner.file_hash(p)
                                 for folder in ('src', 'official/code')
                                 for p in sorted((ROOT / folder).rglob('*.py'))},
                    input_sha256={g.name: runner.file_hash(g) for g in graphs},
                    config_sha256=runner.file_hash(CONFIG))
        runner.write_json(OUTPUT / 'run_info.json', info)
        print('[PREPARE] verifying 100-case baseline archive', flush=True)
        catalog = load_baseline_catalog(ROOT / 'results/problem_1_singlecore', graphs,
                                        CONFIG, official.CODE, repo_root=ROOT)
        info.update(status='running', baseline_validation_seconds=time.perf_counter() - started,
                    baseline_archive='results/problem_1_singlecore',
                    baseline_manifest_sha256=runner.file_hash(ROOT / 'results/problem_1_singlecore/manifest.sha256'))
        runner.write_json(OUTPUT / 'run_info.json', info)
        rows, timings = [], []
        cases = [g.stem for g in graphs]
        runner.summarize(rows, cases, CORES, OUTPUT)
        # Larger graphs start first to reduce the batch's long tail.
        with ProcessPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(timed_case, str(g), catalog[g.stem]): g
                       for g in sorted(graphs, key=lambda g: g.stat().st_size, reverse=True)}
            while futures:
                done, _ = wait(futures, timeout=30, return_when=FIRST_COMPLETED)
                for future in done:
                    graph = futures.pop(future)
                    try:
                        case_rows, timing = future.result()
                    except Exception:
                        timing_path = OUTPUT / graph.stem / 'timing.json'
                        timing = json.loads(timing_path.read_text()) if timing_path.exists() else {'case': graph.stem}
                        timing.update(status='worker_crashed', error=traceback.format_exc(),
                                      core_elapsed_seconds={})
                        runner.write_json(timing_path, timing)
                        case_rows = []
                    rows.extend(case_rows)
                    timings.append(timing)
                    save_timings(timings)
                    runner.summarize(rows, cases, CORES, OUTPUT)
                    print(f"[DONE {len(timings)}/100] {graph.stem} {timing['status']} wall={timing.get('wall_seconds')}s", flush=True)
                sample = dict(at_utc=now(), completed_cases=len(timings), **hardware())
                with (OUTPUT / 'resources.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(sample) + '\n')
                info.update(completed_cases=len(timings), elapsed_seconds=time.perf_counter() - started,
                            last_progress_at_utc=now(), latest_hardware=sample)
                runner.write_json(OUTPUT / 'run_info.json', info)
                if not done:
                    print(f"[PROGRESS] completed={len(timings)}/100 elapsed={info['elapsed_seconds']:.0f}s available_memory={sample['available_memory_gib']:.1f}GiB", flush=True)
        report = runner.summarize(rows, cases, CORES, OUTPUT)
        errors = []
        for graph in graphs:
            source = runner.provenance(graph, CONFIG, catalog[graph.stem], beta_search='adaptive')
            for n in CORES:
                valid, reason = runner.reusable(OUTPUT / graph.stem / f'N{n}', runner.fingerprint(source, n))
                if not valid:
                    errors.append(dict(case=graph.stem, num_cores=n, error=reason))
        if len(timings) != 100 or any(t['status'] != 'success' for t in timings):
            errors.append({'error': 'Incomplete case timing coverage or case failure'})
        info.update(status='complete' if report['complete'] and not errors else 'failed',
                    finished_at_utc=now(), elapsed_seconds=time.perf_counter() - started,
                    audit_errors=errors, verified_multicore_results=400 - sum('num_cores' in e for e in errors))
        runner.write_json(OUTPUT / 'audit.json', dict(complete=info['status'] == 'complete', errors=errors))
        runner.write_json(OUTPUT / 'run_info.json', info)
        good = [t['wall_seconds'] for t in timings if t['status'] == 'success']
        lines = ['# 协作者题目一 100 case 实验', '', f"状态：{info['status']}；完整成功 {len(good)}/100。",
                 f"CPU：{info['hardware']['cpu']}；20 个 case 进程，每例 1 个候选执行流。",
                 '策略：adaptive；模拟核数：2、3、4、5；单核参考值通过归档校验后复用。',
                 f"整批耗时（含前置校验与末尾验收）：{info['elapsed_seconds']:.3f} 秒。", '',
                 '逐例总耗时见 case_timings.csv，各核数指标见 summary.csv。',
                 'wall_seconds 含图读取、求解、方案重放及求解产物写入，不含队列等待与计时记录自身写入；CPU 秒为本进程累计用户态加内核态时间。',
                 'N2–N5_seconds 沿用原算法的阶段计时，不含共享图初始化及完成标记哈希。',
                 '并行墙钟时间包含资源竞争，不代表单进程独占设备的用时。', '']
        if good:
            lines += [f'逐例墙钟：最小 {min(good):.3f} 秒；中位数 {statistics.median(good):.3f} 秒；最大 {max(good):.3f} 秒。', '']
        lines += [f"N{a['num_cores']}：成功 {a['successful_cases']}/100，平均加速比 {a['mean_speedup']}。"
                  for a in report['average_speedup']]
        runner.atomic_text(OUTPUT / 'report.md', '\n'.join(lines) + '\n')
        print(f"[FINISHED] status={info['status']} elapsed={info['elapsed_seconds']:.3f}s", flush=True)
        return 0 if info['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
