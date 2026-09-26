"""按官方反馈自适应 β → 最好已评价 B0 → 组装 B1 → 原版 CLI 独立复核。"""
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import copy
import csv
import io
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from common.artifacts import write_json, atomic_text
from common.baseline import calculate_speedup, load_baseline_catalog
from problem_1_baseline import output_lock
from .official import ROOT, Settings, Q2Error, read_json, file_hash, digest, signature, objective, normalized
from .graph import Graph
from .construct import construct, BudgetExpired
from .beta import BetaSearch, POLICIES
from .memory import profile
from .window import Window, choose_window, solve_window, apply_order


def load_options(path=None):
    defaults = read_json(ROOT/'configs/q2/baseline.json')
    options = read_json(path) if path else defaults
    return validate_options(options)


def validate_options(options, defaults=None):
    if options.get('algorithm_version') == 'Q2-Baseline-AdaptiveBeta-1.1':
        defaults = read_json(ROOT/'configs/q2/legacy_1_1.json')
    elif defaults is None: defaults = read_json(ROOT/'configs/q2/baseline.json')
    repair = options.get('algorithm_version') == 'Q2-Repair-2.0'
    if set(options) != set(defaults): raise Q2Error('INPUT_INVALID', 'algorithm config fields differ')
    for group in ('construction', 'enhancement', 'runtime', 'validation'):
        if set(options[group]) != set(defaults[group]): raise Q2Error('INPUT_INVALID', f'{group}: unknown/missing options')
    c, e, r = options['construction'], options['enhancement'], options['runtime']
    modes, betas = c['scope_modes'], c['betas']
    if (not isinstance(modes, list) or not modes or len(set(modes)) != len(modes)
        or any(m not in ('global', 'component_grouped') for m in modes)):
        raise Q2Error('INPUT_INVALID', 'invalid modes')
    if (not isinstance(betas, list) or not betas or len(betas) > 4
        or any(type(b) is not int for b in betas) or betas != list(range(1, len(betas)+1))):
        raise Q2Error('INPUT_INVALID', 'betas must be an ascending prefix of [1, 2, 3, 4]')
    if c['beta_search'] not in POLICIES:
        raise Q2Error('INPUT_INVALID', 'beta_search must be adaptive, fixed or grid')
    for key, val, low, high in [('shortlist_k', c['shortlist_k'], 1, 8), ('window_subgraphs', e['window_subgraphs'], 1, 8),
                                ('max_windows', e['max_windows'], 0, 32), ('workers', r['workers'], 1, 128)]:
        if type(val) is not int or not low <= val <= high: raise Q2Error('INPUT_INVALID', key)
    for key in ('wall_budget_seconds', 'final_verification_reserve_seconds'):
        value = r[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise Q2Error('INPUT_INVALID', key)
    if r['final_verification_reserve_seconds'] >= r['wall_budget_seconds']:
        raise Q2Error('INPUT_INVALID', 'reserve must be smaller than wall budget')
    for group, editable in [('construction', {'scope_modes', 'betas', 'shortlist_k', 'beta_search'} | ({'encodings', 'component_candidates', 'beta_continuation'} if repair else set())),
                            ('enhancement', {'enabled', 'window_subgraphs', 'max_windows'} | ({'objective', 'spill_gate', 'block_operations'} if repair else set())),
                            ('runtime', {'wall_budget_seconds', 'final_verification_reserve_seconds', 'workers', 'debug_trace'}),
                            ('validation', set())]:
        for k, v in options[group].items():
            if k not in editable and (type(v) is not type(defaults[group][k]) or v != defaults[group][k]):
                raise Q2Error('INPUT_INVALID', f'unsupported algorithm change: {group}.{k}')
    for v in (e['enabled'], r['debug_trace']):
        if type(v) is not bool: raise Q2Error('INPUT_INVALID', 'boolean flag required')
    if options['algorithm_version'] != defaults['algorithm_version']:
        raise Q2Error('INPUT_INVALID', 'unknown algorithm version')
    if repair:
        if (not isinstance(c['encodings'], list) or not c['encodings'] or
                len(set(c['encodings'])) != len(c['encodings']) or
                any(x not in ('legacy', 'commit', 'tail') for x in c['encodings'])):
            raise Q2Error('INPUT_INVALID', 'invalid encodings')
        if c['beta_continuation'] not in ('plateau', 'strict'):
            raise Q2Error('INPUT_INVALID', 'invalid beta continuation')
        if type(c['component_candidates']) is not bool or type(e['spill_gate']) is not bool:
            raise Q2Error('INPUT_INVALID', 'boolean flags required')
        if e['objective'] not in ('capacity_min_disruption', 'max_normalized_no_spill_peak'):
            raise Q2Error('INPUT_INVALID', 'unknown enhancement objective')
        if type(e['block_operations']) is not int or not 1 <= e['block_operations'] <= 128:
            raise Q2Error('INPUT_INVALID', 'invalid block operations')
    return options


class Budget:
    def __init__(self, runtime):
        self.start = time.monotonic(); self.limit = runtime['wall_budget_seconds']
        self.reserve = runtime['final_verification_reserve_seconds']
        self.estimate = 0.

    def remaining(self): return max(0., self.limit-(time.monotonic()-self.start))

    def check(self):
        if self.remaining() <= self.reserve: raise BudgetExpired('search budget exhausted; reserve final verification')

    def check_construction(self):
        if self.remaining() <= min(2*self.reserve, self.limit*.8):
            raise BudgetExpired('reserve initial official evaluation and final verification')

    def observe(self, seconds):
        self.estimate = max(self.estimate, seconds)
        self.reserve = max(self.reserve, 1.25*seconds)


def peak_rss(process=None):
    """本求解进程和官方子进程分别采样；不是整机内存。"""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)]+[(n, ctypes.c_size_t) for n in
                       ('peak_ws', 'ws', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]
        info = Counters(); info.cb = ctypes.sizeof(info)
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.GetCurrentProcess.restype = wintypes.HANDLE
        ps = ctypes.WinDLL('psapi', use_last_error=True)
        ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        handle = process._handle if process is not None else api.GetCurrentProcess()
        if ps.GetProcessMemoryInfo(handle, ctypes.byref(info), info.cb): return int(info.peak_ws)
        return None
    if process is not None:
        try:
            for line in Path(f'/proc/{process.pid}/status').read_text().splitlines():
                if line.startswith('VmHWM:'): return int(line.split()[1])*1024
        except (OSError, ValueError): pass
        return None
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == 'darwin' else value*1024)


def identity(graph, cores, settings, options, baseline):
    paths = sorted((ROOT/'src/q2').glob('*.py'))+sorted((ROOT/'src/common').glob('*.py'))
    paths += [ROOT/'src/problem_1_baseline.py', ROOT/'src/solve_problem2.py', ROOT/'configs/q2/official_sha256.json']
    return dict(problem=2, scene='B', schema_version=1, case=graph.stem, num_cores=cores,
                graph_sha256=file_hash(graph), config_sha256=file_hash(settings.config),
                official_sha256=settings.hashes, source_sha256={p.relative_to(ROOT).as_posix(): file_hash(p) for p in paths},
                algorithm=options, python=dict(executable=sys.executable, version=platform.python_version()), baseline=baseline)


def reusable(directory, expected):
    required = {'plan.json', 'result.json', 'trace.json', 'result.log', 'run.json', 'provenance.json', 'windows.jsonl'}
    try:
        done = read_json(directory/'complete.json')
        if done.get('fingerprint') != expected or done.get('status') != 'SUCCESS_VERIFIED' or not done.get('official_replay_verified'):
            return False
        hashes = done['artifacts_sha256']
        if not required <= hashes.keys(): return False
        if any(Path(n).name != n or '/' in n or '\\' in n or file_hash(directory/n) != h for n, h in hashes.items()): return False
        run = read_json(directory/'run.json'); provenance = read_json(directory/'provenance.json')
        return run['status'] == 'SUCCESS_VERIFIED' and digest(provenance['identity']) == expected
    except (OSError, ValueError, KeyError, TypeError): return False


def run_official(graph, plan, settings, directory, timeout, calls, phase):
    directory.mkdir(parents=True, exist_ok=False)
    plan_path = directory/'plan.json'; write_json(plan_path, plan)
    # 已审原版 CLI 无后代进程；只终止并回收本次创建的进程。
    command = [sys.executable, '-X', 'utf8', str(settings.root/'code/multicore_cut_evaluate_problem_2.py'),
               str(graph), str(plan_path), '--config', str(settings.config), '-o', str(directory/'result.json'),
               '--trace-output', str(directory/'trace.json'), '--log-output', str(directory/'result.log')]
    record = dict(phase=phase, plan_hash=digest(plan), timeout_seconds=max(0., timeout), status='EVAL_TIMEOUT')
    calls.append(record); started = time.monotonic()
    if timeout <= 0:
        record['wall_seconds'] = 0.; record['launched'] = False
        return None
    record['launched'] = True
    # 来源若发生改变，不接受新结果；不自动改写 manifest。
    Settings.read(settings.root, settings.config)
    try:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) as child:
            stopped = threading.Event(); rss = []
            def sample():
                while not stopped.is_set():
                    value = peak_rss(child)
                    if value is not None: rss.append(value)
                    stopped.wait(.05)
            monitor = threading.Thread(target=sample, daemon=True); monitor.start()
            try:
                stdout, stderr = child.communicate(timeout=timeout)
            except BaseException:
                child.kill(); child.communicate()
                raise
            finally:
                stopped.set(); monitor.join()
                record['peak_rss_bytes'] = max(rss, default=None)
        atomic_text(directory/'stdout.txt', stdout); atomic_text(directory/'stderr.txt', stderr)
        record['returncode'] = child.returncode
        if child.returncode:
            record['status'] = 'OFFICIAL_ERROR'; record['stderr'] = stderr[-6000:]
            if any(line == 'MemoryError' or line.startswith('MemoryError:') for line in stderr.splitlines()):
                record['status'] = 'RESOURCE_LIMIT'
                return None
            # 编程异常保留 traceback 并终止，不能当作普通候选失败吞掉。
            if 'Traceback (most recent call last)' in stderr:
                raise RuntimeError(stderr)
            return None
        result = read_json(directory/'result.json')
        if result.get('scene') != 'B' or result.get('num_cores') != len(plan['core_schedules']):
            raise AssertionError('official scene/core count differs')
        Settings.read(settings.root, settings.config)
        record['status'] = 'OK'; record['objective'] = objective(result)
        return result
    except subprocess.TimeoutExpired:
        record['status'] = 'EVAL_TIMEOUT'
        return None
    finally:
        record['wall_seconds'] = time.monotonic()-started


def execution_digest(bundle, settings):
    return digest(dict(cores={k: dict(graph=c['graph'], seq=c['seq']) for k, c in bundle['cores'].items()},
                       cross_links=bundle['cross_links'], settings=settings.hashes))


def select_final(baseline, enhanced):
    return 'B1' if enhanced is not None and objective(enhanced) < objective(baseline) else 'B0'


def load_baseline_result(directory):
    """Load a published, hash-verified Q2 2.0 snapshot for a future optimizer."""
    from .contracts import BaselineResult
    directory = Path(directory)
    complete = read_json(directory/'complete.json')
    if not reusable(directory, complete.get('fingerprint')):
        raise Q2Error('RESULT_MISMATCH', 'baseline artifacts failed integrity check')
    return BaselineResult.from_record(read_json(directory/'baseline_state.json'))


def solve(graph_path, cores, output, options=None, reference=ROOT/'official', config=None, resume=False, baseline=None):
    graph_path = Path(graph_path).resolve(); directory = Path(output).resolve()/graph_path.stem/f'N{cores}'
    directory.parent.mkdir(parents=True, exist_ok=True)
    options = load_options() if options is None else validate_options(options)
    settings = Settings.read(reference, config)
    source = identity(graph_path, cores, settings, options, baseline); fp = digest(source)
    if resume and reusable(directory, fp):
        return {**read_json(directory/'run.json'), 'resumed': True}
    if directory.exists(): raise Q2Error('OUTPUT_EXISTS', f'选择新输出目录或匹配的 --resume: {directory}')
    calls = []; windows = []; candidates = []; budget = Budget(options['runtime'])
    stages = {'c1_profile_calls': 0, 'c1_profile_seconds': 0.}; phase = 'graph'; stop = 'completed'
    def profiled(graph, seq, capacity):
        start = time.monotonic()
        try:
            return profile(graph, seq, capacity)
        finally:
            stages['c1_profile_calls'] += 1
            stages['c1_profile_seconds'] += time.monotonic()-start
    # 失败 attempt 保留证据，正式目录只在全部验证完成后原子出现。
    attempt = Path(tempfile.mkdtemp(prefix=f'.N{cores}-attempt-', dir=directory.parent))
    report = dict(case=graph_path.stem, num_cores=cores, status='RUNNING', fingerprint=fp,
                  beta_search=options['construction']['beta_search'],
                  official_calls=calls, candidates=candidates, windows=windows, stages=stages)
    def checkpoint():
        write_json(attempt/'progress.json', dict(phase=phase, elapsed_seconds=time.monotonic()-budget.start,
                   stop_reason=stop, official_calls=calls, completed_candidates=len(candidates), completed_windows=len(windows),
                   search=report.get('search')))
        print(f'[{graph_path.stem} N{cores}] {phase}; candidates={len(candidates)} windows={len(windows)} calls={len(calls)}', flush=True)
    try:
        started = time.monotonic(); raw = read_json(graph_path); g = Graph(raw, settings)
        stages['graph_seconds'] = time.monotonic()-started
        repaired = options['algorithm_version'] == 'Q2-Repair-2.0'
        if repaired:
            from .candidates import search_baseline
            outcome = search_baseline(g, graph_path, cores, options, attempt, budget, calls,
                                      candidates, windows, run_official, fp, report)
            baseline_plan, baseline_result = outcome['baseline_plan'], outcome['baseline_result']
            enhanced, selected = outcome['enhanced'], outcome['selected']
            final_plan, final_result = outcome['final_plan'], outcome['final_result']
            stop, accepted = outcome['stop'], outcome['accepted']
        else:
            baseline_plan = baseline_result = None
            c = options['construction']; prefix_seconds = 0.; construction_seconds = 0.
            modes = [mode for mode in c['scope_modes'] if mode != 'component_grouped' or len(g.components) > 1]
            search = BetaSearch(modes, c['betas'], c['beta_search'])
            report['search'] = search.report
            # 仅复用完全相同方案的成功评价。等价执行图仍可能有不同子图边界及结果元数据。
            # 缓存只保留指标、证书和落盘路径，避免持有多个完整官方结果。
            evaluated = {}; best_key = None
            phase = 'construction'; checkpoint()
            debug_stream = (attempt/'construction.jsonl').open('w', encoding='utf-8') if options['runtime']['debug_trace'] else None
            try:
                while (spec := search.next_candidate()) is not None:
                    mode, beta = spec
                    interrupted = info = None
                    try:
                        phase = 'construction'; checkpoint(); budget.check_construction(); started = time.monotonic()
                        interrupted = dict(mode=mode, beta=beta, status='CONSTRUCTION_INCOMPLETE')
                        stats = {}
                        trace = (lambda ev: debug_stream.write(json.dumps(dict(mode=mode, beta=beta, **ev))+'\n')) if debug_stream else None
                        try:
                            plan, info = construct(g, cores, mode, beta, c['shortlist_k'], budget.check_construction, trace, stats=stats)
                        finally:
                            elapsed = time.monotonic()-started
                            construction_seconds += elapsed
                            interrupted.update(wall_seconds=elapsed, **stats)
                        interrupted = None
                        info = dict(info, candidate_index=len(candidates), plan_hash=digest(plan), status='CONSTRUCTED')
                        candidates.append(info)
                        cached = evaluated.get(info['plan_hash'])
                        result = None
                        if cached is not None:
                            if (info['base_copy_work'], info['base_copy_bytes']) != cached['traffic']:
                                raise AssertionError('same plan has different base traffic')
                            info.update(cached['profile'], status='DUPLICATE', evaluation_status='CACHED',
                                        objective=cached['objective'], reused_candidate_index=cached['candidate_index'],
                                        official_call_index=cached['official_call_index'])
                        else:
                            phase = 'step1_profile'; checkpoint(); budget.check(); started = time.monotonic()
                            from stub_multicore_cut_and_schedule import MulticoreCutError
                            from multicore_cut_evaluate_problem_2 import SceneBEvaluationError
                            try:
                                bundle = settings.prefix(raw, plan)
                            except (MulticoreCutError, SceneBEvaluationError) as error:
                                prefix_seconds += time.monotonic()-started
                                info.update(status='PLAN_INVALID', error=str(error), evaluation_status='NOT_EVALUATED')
                                info['search_decision'] = search.observe(mode, beta, None)
                                checkpoint(); continue
                            prefix_work = sum(max(1, o['cycles']) for x in bundle['cores'].values()
                                              for o in x['graph']['ops'] if o['op'] in ('COPY_IN', 'COPY_OUT'))
                            if (prefix_work, bundle['base_copy_bytes']) != (info['base_copy_work'], info['base_copy_bytes']):
                                raise AssertionError('incremental/formula/official prefix traffic differs')
                            profiles = {k: profiled(x['graph'], x['seq'], settings.capacity) for k, x in bundle['cores'].items()}
                            info.update(profile_peak_scaled=max((p.scaled for p in profiles.values()), default=0),
                                        execution_signature=execution_digest(bundle, settings))
                            prefix_seconds += time.monotonic()-started
                            del bundle, profiles
                            phase = 'baseline_official'; checkpoint(); budget.check()
                            if baseline_result is not None and budget.remaining()-budget.reserve < budget.estimate:
                                raise BudgetExpired('reserve final verification before another candidate evaluation')
                            call_index = len(calls); evaluation_dir = attempt/f'eval_{call_index:03d}'
                            result = run_official(graph_path, plan, settings, evaluation_dir,
                                                  budget.remaining()-budget.reserve, calls, 'B0')
                            budget.observe(calls[-1]['wall_seconds'])
                            search.report['official_evaluations'] += int(calls[-1]['launched'])
                            info.update(evaluation_status=calls[-1]['status'], official_call_index=call_index,
                                        status='SUCCESS' if result is not None else calls[-1]['status'])
                            if result is not None:
                                info['objective'] = objective(result)
                                evaluated[info['plan_hash']] = dict(objective=info['objective'],
                                    traffic=(info['base_copy_work'], info['base_copy_bytes']),
                                    profile={k: info[k] for k in ('profile_peak_scaled', 'execution_signature')},
                                    candidate_index=info['candidate_index'], official_call_index=call_index,
                                    result_path=evaluation_dir/'result.json')
                        value = info.get('objective')
                        improved_best = False
                        if value is not None:
                            key = (*value, ('global', 'component_grouped').index(mode), beta)
                            if best_key is None or key < best_key:
                                # 同方案命中只有成为新 incumbent 时才读回完整结果。
                                baseline_result = result if result is not None else read_json(cached['result_path'])
                                baseline_plan, best_key = plan, key
                                improved_best = True
                        info['search_decision'] = search.observe(mode, beta, value, cached=cached is not None)
                        if improved_best:
                            report['selected_candidate'] = dict(info)
                        del result
                        checkpoint()
                    except BudgetExpired:
                        if interrupted is not None: report['interrupted_construction'] = interrupted
                        stop = 'construction_budget' if phase == 'construction' else 'initialization_budget'
                        if info is not None and 'search_decision' not in info:
                            info.update(evaluation_status='NOT_EVALUATED_BUDGET',
                                        search_decision=dict(previous_makespan=None, strictly_improved=None,
                                                             next_beta=None, stop_reason=stop))
                        search.stop(stop); break
            finally:
                if debug_stream: debug_stream.close()
            stages.update(construction_seconds=construction_seconds, step1_profile_seconds=prefix_seconds)
            stages['initialization_official_seconds'] = sum(call['wall_seconds'] for call in calls)
            report['evaluated_candidates'] = search.report['evaluated_candidates']
            if baseline_result is None:
                search.report['fallback_used'] = True
                phase = 'fallback_official'; checkpoint()
                fallback = dict(node_to_subgraph={str(g.ids[v]): i for i, v in enumerate(g.order)},
                                core_schedules=[list(range(len(g.ids)))]+[[] for _ in range(cores-1)])
                result = run_official(graph_path, fallback, settings, attempt/f'eval_{len(calls):03d}',
                                      budget.remaining()-budget.reserve, calls, 'fallback')
                if result is None: raise Q2Error(calls[-1]['status'], '没有完成有效 B0；未发布正式方案')
                baseline_plan, baseline_result = fallback, result
                budget.observe(calls[-1]['wall_seconds']); report['selected_candidate'] = dict(mode='fallback')
            started = time.monotonic()
            write_json(attempt/'baseline_plan.json', baseline_plan); write_json(attempt/'baseline_result.json', baseline_result)
            stages['plan_serialization_seconds'] = time.monotonic()-started
            phase = 'windows'; checkpoint(); started = time.monotonic()
            trial_plan = copy.deepcopy(baseline_plan); bundle = None; processed = set(); accepted = 0
            e = options['enhancement']
            if e['enabled']:
                for _ in range(e['max_windows']):
                    try:
                        budget.check()
                        if bundle is None:
                            bundle = settings.prefix(raw, trial_plan); bundle['_raw'] = raw
                        profiles = {k: profiled(x['graph'], x['seq'], settings.capacity) for k, x in bundle['cores'].items()}
                        selected = choose_window(bundle, profiles, e['window_subgraphs'], processed)
                        if selected is None: break
                        k, left, right = selected; processed.add(selected)
                        w = Window(bundle, k, left, right, settings.capacity)
                        window_started = time.monotonic(); r = solve_window(w, budget.check)
                        predicted = r.pop('predicted_seq', None)
                        record = dict(core=k, left=left, right=right, version=accepted, **r,
                                      wall_seconds=time.monotonic()-window_started, accepted=False)
                        windows.append(record)
                        if not r['completed']: stop = 'window_budget'; break
                        if r['peak_after'] < r['peak_before']:
                            r['predicted_seq'] = predicted
                            trial_plan, bundle = apply_order(trial_plan, bundle, w, r, settings, profiler=profiled)
                            accepted += 1; processed.clear(); record['accepted'] = True; record['sequence_verified'] = True
                        checkpoint()
                    except BudgetExpired:
                        stop = 'enhancement_budget'; break
                    except Q2Error as error:
                        if error.status != 'WINDOW_UNSUPPORTED': raise
                        windows.append(dict(status=error.status, error=str(error), completed=False, accepted=False))
            stages['enhancement_seconds'] = time.monotonic()-started
            enhanced = None
            if trial_plan != baseline_plan:
                phase = 'enhanced_official'; checkpoint()
                # 没有足够时间容纳一次估计评价和最终复核时保留 B0。
                if budget.remaining()-budget.reserve >= budget.estimate:
                    enhanced = run_official(graph_path, trial_plan, settings, attempt/f'eval_{len(calls):03d}',
                                            budget.remaining()-budget.reserve, calls, 'B1')
                    budget.observe(calls[-1]['wall_seconds'])
                else: stop = 'enhanced_evaluation_budget'
            selected = select_final(baseline_result, enhanced)
            final_plan, final_result = (trial_plan, enhanced) if selected == 'B1' else (baseline_plan, baseline_result)
        started = time.monotonic(); write_json(attempt/'plan.json', final_plan)
        stages['plan_serialization_seconds'] += time.monotonic()-started
        phase = 'final_verification'; checkpoint(); started = time.monotonic()
        reloaded = read_json(attempt/'plan.json')
        stages['plan_serialization_seconds'] += time.monotonic()-started
        if reloaded != final_plan or signature(reloaded) != signature(final_plan): raise AssertionError('serialized plan differs')
        verification_dir = attempt/f'eval_{len(calls):03d}'
        verified = run_official(graph_path, reloaded, settings, verification_dir, budget.remaining(), calls, 'final_verification')
        if verified is None: raise Q2Error(calls[-1]['status'], '最终独立复核未成功；未发布正式方案')
        if normalized(verified) != normalized(final_result): raise AssertionError('full official replay differs')
        if objective(verified) > objective(baseline_result): raise AssertionError('final is worse than B0')
        if identity(graph_path, cores, settings, options, baseline) != source:
            raise Q2Error('REFERENCE_MISMATCH', '运行期间输入或工程源码发生变化')
        stages['verification_seconds'] = time.monotonic()-started
        if not repaired: report['search_complete'] = stop == 'completed'
        stages['official_seconds'] = sum(c['wall_seconds'] for c in calls)
        report.update(status='SUCCESS_VERIFIED', selected=selected, baseline_objective=objective(baseline_result),
                      enhanced_objective=objective(enhanced) if enhanced else None, final_objective=objective(verified),
                      baseline_plan_hash=digest(baseline_plan), plan_hash=digest(final_plan),
                      stop_reason=stop, accepted_windows=accepted, official_replay_verified=True,
                      official_evaluations=sum(c['launched'] for c in calls),
                      distinct_evaluated_plans=len({c['plan_hash'] for c in calls if c['launched']}),
                      failed_official_calls=sum(c['status'] != 'OK' for c in calls if c['launched']),
                      wall_seconds=time.monotonic()-budget.start, peak_rss_bytes=peak_rss(),
                      official_peak_rss_bytes=max((c.get('peak_rss_bytes') or 0 for c in calls), default=0),
                      rss_scope='solver peak and separately sampled official child high-water RSS; not combined simultaneous RSS',
                      data_movement_bytes=verified['data_movement_bytes'], baseline_ref=baseline,
                      speedup=calculate_speedup(baseline['makespan_cycles'], verified['makespan']) if baseline and verified['makespan'] > 0 else None)
        for item in candidates: item.pop('plan', None)
        write_json(attempt/'result.json', verified)
        for name in ('trace.json', 'result.log'):
            atomic_text(attempt/name, (verification_dir/name).read_text(encoding='utf-8'))
        write_json(attempt/'run.json', report)
        git = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
        dirty = subprocess.run(['git', 'status', '--porcelain=v1'], cwd=ROOT, capture_output=True, text=True)
        write_json(attempt/'provenance.json', dict(identity=source, fingerprint=fp, git_head=git.stdout.strip(),
                   git_status=dirty.stdout.splitlines(), platform=platform.platform(), created_at=datetime.now(timezone.utc).isoformat()))
        atomic_text(attempt/'windows.jsonl', ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in windows))
        names = ['plan.json', 'result.json', 'trace.json', 'result.log', 'run.json', 'provenance.json',
                 'windows.jsonl', 'baseline_plan.json', 'baseline_result.json']
        if repaired:
            from .candidates import export_baseline_result
            from dataclasses import asdict
            snapshot = export_baseline_result(g, outcome['candidate'], verified, candidates, report['search'])
            write_json(attempt/'baseline_state.json', asdict(snapshot))
            names += ['baseline_state.json', 'beta_state.json']
        write_json(attempt/'complete.json', dict(status='SUCCESS_VERIFIED', fingerprint=fp, official_replay_verified=True,
                   artifacts_sha256={n: file_hash(attempt/n) for n in names}))
        attempt.rename(directory)
        print(f'[{graph_path.stem} N{cores}] SUCCESS_VERIFIED {selected}: {verified["makespan"]} cycles', flush=True)
        return report
    except BaseException as error:
        status = error.status if isinstance(error, Q2Error) else ('RESOURCE_LIMIT' if isinstance(error, MemoryError) else 'INTERNAL_ERROR')
        report.update(status=status, error=str(error),
                      phase=phase, traceback=traceback.format_exc(), wall_seconds=time.monotonic()-budget.start,
                      peak_rss_bytes=peak_rss(), attempt=str(attempt))
        for item in candidates: item.pop('plan', None)
        write_json(attempt/'failure.json', report)
        if isinstance(error, (Q2Error, MemoryError)): return report
        raise


def summarize(rows, requested, output):
    expected = {(str(case), n) for case, n in requested}
    success = {(r['case'], r['num_cores']): r for r in rows if r['status'] == 'SUCCESS_VERIFIED'}
    strategy_complete = sum(r.get('search_complete', r.get('stop_reason') == 'completed') for r in success.values())
    report = dict(requested=len(expected), successful=len(success), complete=len(success) == len(expected),
                  strategy_complete=strategy_complete, search_complete=strategy_complete == len(expected), rows=rows)
    report['averages'] = []
    for n in sorted({n for _, n in expected}):
        group = [r for (_, k), r in success.items() if k == n]
        ratios = [r['speedup'] for r in group if r.get('speedup') is not None]
        report['averages'].append(dict(num_cores=n, successful_cases=len(group), requested_cases=sum(k == n for _, k in expected),
                                      speedup_cases=len(ratios), mean_speedup=sum(ratios)/len(ratios) if ratios else None))
    Path(output).mkdir(parents=True, exist_ok=True); write_json(Path(output)/'summary.json', report)
    stream = io.StringIO(); fields = ['case', 'num_cores', 'status', 'makespan', 'added_copy_bytes', 'selected',
                                    'beta_search', 'selected_mode', 'selected_beta', 'evaluated_candidates',
                                    'initialization_official_evaluations', 'initialization_cache_hits', 'wall_seconds', 'speedup']
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n'); writer.writeheader()
    for r in rows:
        writer.writerow({**{f: r.get(f) for f in fields}, 'makespan': r.get('final_objective', [None])[0],
                         'selected_mode': r.get('selected_candidate', {}).get('mode'),
                         'selected_beta': r.get('selected_candidate', {}).get('beta'),
                         'initialization_official_evaluations': r.get('search', {}).get('official_evaluations'),
                         'initialization_cache_hits': r.get('search', {}).get('cache_hits'),
                         'added_copy_bytes': r.get('data_movement_bytes', {}).get('added_copy_bytes')})
    atomic_text(Path(output)/'summary.csv', stream.getvalue())
    return report


def batch(graphs, cores, output, options, reference, config, workers=1, resume=False, baseline_archive=None):
    graphs = [Path(g).resolve() for g in graphs]; output = Path(output).resolve()
    options = copy.deepcopy(options); options['runtime']['workers'] = workers
    validate_options(options)
    if not graphs or len({p.stem for p in graphs}) != len(graphs): raise Q2Error('INPUT_INVALID', 'nonempty distinct cases required')
    settings = Settings.read(reference, config)
    catalog = load_baseline_catalog(baseline_archive, graphs, settings.config, settings.root/'code', repo_root=ROOT) if baseline_archive else {}
    output.mkdir(parents=True, exist_ok=True)
    rows = []; requested = [(p.stem, n) for p in graphs for n in cores]
    with output_lock(output):
        arguments = [(p, n, output, options, reference, config, resume, catalog.get(p.stem)) for p in graphs for n in cores]
        if workers == 1:
            for args in arguments:
                rows.append(solve(*args)); summarize(rows, requested, output)
        else:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(solve, *a): a for a in arguments}
                for future in as_completed(futures):
                    # 编程异常传播；不将其伪装为正常没改善。
                    rows.append(future.result()); summarize(rows, requested, output)
    return summarize(rows, requested, output)
