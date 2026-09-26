"""Q2 prerequisite -> graph-wide Q3 budget -> independent official publication."""
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import copy
import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
import time
import traceback

from .artifacts import file_hash, write_json, retry_locked
from problem_1_baseline import output_lock
from q2.official import ROOT, read_json, digest, normalized
from q2.state import plan_digest
from .baseline import resolve, DEFAULT_SOURCE, SINGLECORE_SOURCE
from .config import parameters
from .official import Settings, objective, replay
from .worker import Worker


def source_hashes():
    paths = set()
    for name in ('src/q3', 'src/q2', 'src/common', 'official/code'):
        paths.update((ROOT / name).rglob('*.py'))
    paths.update(ROOT / name for name in ('src/solve_problem3.py', 'src/solve_problem2.py',
                                          'configs/q2/baseline.json', 'configs/q2/official_sha256.json'))
    return {p.relative_to(ROOT).as_posix(): file_hash(p) for p in sorted(paths) if p.is_file()}


def source_input_hashes(source):
    path = Path(source) if source else DEFAULT_SOURCE
    selected = []
    if path.is_file():
        selected = [path]
    if path.is_dir():
        selected = [p for p in (path / 'plans.zip', path / 'details.csv', path / 'provenance.json', path / 'manifest.sha256') if p.is_file()]
        if not selected:
            for plan in path.rglob('plan.json'):
                selected.extend(p for p in (plan, plan.parent / 'result.json', plan.parent / 'complete.json', plan.parent / 'provenance.json') if p.is_file())
    if SINGLECORE_SOURCE.is_dir():
        selected.extend(p for p in (SINGLECORE_SOURCE / 'run_info.json', SINGLECORE_SOURCE / 'manifest.sha256') if p.is_file())
        selected.extend(SINGLECORE_SOURCE.glob('case_*/*_res.json'))
    return {str(p.resolve()): file_hash(p) for p in sorted(set(selected))}


def verified_directory(directory, fingerprint=None):
    directory = Path(directory)
    marker = directory / 'complete.json'
    if not marker.is_file():
        return False
    try:
        record = read_json(marker)
        case = read_json(directory / 'case.json')
    except (ValueError, OSError):
        return False
    if record.get('status') != 'SUCCESS_VERIFIED' or fingerprint is not None and record.get('fingerprint') != fingerprint:
        return False
    files = record.get('files', {})
    if not case.get('rows') or 'case.json' not in files or case.get('fingerprint') != record['fingerprint']:
        return False
    required = {'case.json', 'search.json'}
    for row in case['rows']:
        base = f'N{row["num_cores"]}/'
        required.update(base + name for name in ('q2_plan.json', 'q2_source.json', 'audit.json',
                        'verified/result.json', 'verified/trace.json', 'verified/log.txt',
                        f'{row["case"]}_multicore_res.json'))
    if not required <= files.keys():
        return False
    for name, expected in files.items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file() or file_hash(path) != expected:
            return False
    return True


def solve_case(graph_path, cores, output, *, config=None, reference=ROOT / 'official',
               q2_source=None, options=None, resume=False, hashes=None, q2_hashes=None):
    started = time.monotonic()
    graph_path, output = Path(graph_path).resolve(), Path(output).resolve()
    options = parameters(options)
    settings = Settings.read(reference, config)
    hashes = source_hashes() if hashes is None else hashes
    q2_hashes = source_input_hashes(q2_source) if q2_hashes is None else q2_hashes
    identity = dict(input_sha256=file_hash(graph_path), cores=cores, options=options,
                    source_hashes=hashes, q2_input_hashes=q2_hashes, official=settings.scene.hashes,
                    q3_environment=settings.namespace)
    fingerprint = digest(identity)
    destination = output / graph_path.stem
    if destination.exists():
        if resume and verified_directory(destination, fingerprint):
            return read_json(destination / 'case.json')['rows']
        raise ValueError(f'existing Q3 output is incomplete, changed or not resumable: {destination}')
    output.mkdir(parents=True, exist_ok=True)
    attempt = Path(tempfile.mkdtemp(prefix=f'.{graph_path.stem}-attempt-', dir=output))
    scratch = attempt / 'scratch'
    scratch.mkdir()
    worker = Worker(graph_path, settings, options, scratch)
    states, records, rows = {}, [], []
    prerequisite_seconds = 0.0
    reserve = options['verification_reserve']
    deadline = started + options['seconds']

    def remaining():
        return deadline - time.monotonic()

    def timeout(search=False):
        value = remaining() - (reserve if search else 0)
        if value <= 0:
            raise TimeoutError('Q3 graph-wide budget exhausted')
        return min(options['evaluation_timeout'], value)

    def checkpoint(phase):
        write_json(attempt / 'progress.json', dict(phase=phase, case=graph_path.stem,
            q3_seconds=time.monotonic() - started - prerequisite_seconds,
            prerequisite_seconds=prerequisite_seconds, remaining_seconds=max(0, remaining()),
            objectives={n: s['objective'] for n, s in states.items()}, candidate_records=len(records)))

    def evaluate_candidate(n, path, family, search=True):
        state = states[n]
        reply = worker.call(dict(action='evaluate', plan=str(path), incumbent=state['objective'],
                                 output=str(scratch / f'candidate_result_{len(records):06d}.json')), timeout(search))
        records.append(dict(num_cores=n, family=family, plan_digest=plan_digest(read_json(path)),
                            **{k: v for k, v in reply.items() if k not in ('diagnostics', 'result')}))
        if reply.get('objective') is not None and tuple(reply['objective']) < tuple(state['objective']):
            shutil.copyfile(path, state['plan'])
            shutil.copyfile(reply['result'], state['result'])
            state['objective'] = reply['objective']
            state['winner'] = family
            state['stage'] = 0
            state['done'] = False
            write_json(state['directory'] / 'diagnostics.json', reply['diagnostics'])
            return True
        return False

    try:
        # Q2 generation is an explicitly separate stage. Existing Q2 answers are
        # sufficient input; no previous search histories or estimates are loaded.
        inputs = {}
        for n in cores:
            checkpoint(f'Q2_input_N{n}')
            path, origin = resolve(graph_path, n, q2_source, settings, options, attempt / f'N{n}')
            prerequisite_seconds += origin['generated_seconds']
            deadline = started + prerequisite_seconds + options['seconds']
            inputs[n] = path, origin
        seed_costs = []
        for n in cores:
            directory = attempt / f'N{n}'
            plan, origin = inputs[n]
            checkpoint(f'Q2_replay_N{n}')
            if origin.get('frozen_objective'):
                q2_result = dict(makespan=origin['expected_objective'][0],
                                data_movement_bytes=dict(added_copy_bytes=origin['expected_objective'][1]))
                write_json(directory / 'no_l2/reference.json', dict(status='REUSED_VERIFIED_Q2',
                           objective=origin['expected_objective'], source=origin))
            else:
                q2_result = replay(graph_path, plan, settings, directory / 'no_l2', timeout(), problem=2)
            if origin['expected_objective'] is not None and tuple(origin['expected_objective']) != objective(q2_result):
                raise AssertionError('Q2 source result disagrees with independent official replay')
            checkpoint(f'Q3_seed_N{n}')
            stamp = time.monotonic()
            seed = replay(graph_path, plan, settings, directory / 'seed_l2', timeout())
            seed_costs.append(time.monotonic() - stamp)
            best_plan, best_result = directory / 'best_plan.json', directory / 'best_result.json'
            shutil.copyfile(plan, best_plan)
            shutil.copyfile(directory / 'seed_l2/result.json', best_result)
            states[n] = dict(directory=directory, plan=best_plan, result=best_result,
                             objective=list(objective(seed)), seed_objective=list(objective(seed)),
                             q2_objective=list(objective(q2_result)), origin=origin, winner='Q2_initial',
                             stage=0, seen={plan_digest(read_json(plan))}, tried=0, done=False, stop_reason=None)
        # Pilot large graphs require ~2.4x their seed CLI cost for final replay
        # plus the independent physical audit. Keep headroom under CPU contention.
        reserve = max(reserve, 4 * sum(seed_costs) + 10)
        for smaller, larger in zip(cores, cores[1:]):
            if tuple(states[larger]['objective']) > tuple(states[smaller]['objective']):
                padded = read_json(states[smaller]['plan'])
                padded['core_schedules'] += [[] for _ in range(larger - smaller)]
                path = scratch / 'padded.json'
                write_json(path, padded)
                if remaining() > reserve + 1:
                    evaluate_candidate(larger, path, 'empty_core_seed')
        stages = ('cheap', 'placement', 'joint')
        while any(not state['done'] for state in states.values()) and remaining() > reserve + 1:
            for n in cores:
                state = states[n]
                if state['done']:
                    continue
                if state['tried'] >= options['max_candidates']:
                    state.update(done=True, stop_reason='candidate_limit')
                    continue
                if remaining() <= reserve + 1:
                    break
                checkpoint(f'search_N{n}_{stages[state["stage"]]}')
                try:
                    proposed = worker.call(dict(action='next', plan=str(state['plan']), result=str(state['result']),
                                                stage=stages[state['stage']], count=options['batch_size']), timeout(True))
                    if proposed['status'] != 'PROPOSALS':
                        raise RuntimeError(f'current verified state could not generate proposals: {proposed}')
                    improved = False
                    for proposal in proposed['proposals']:
                        path = Path(proposal['path'])
                        try:
                            if proposal['digest'] in state['seen']:
                                continue
                            if state['tried'] >= options['max_candidates'] or remaining() <= reserve + 1:
                                break
                            state['seen'].add(proposal['digest'])
                            state['tried'] += 1
                            improved |= evaluate_candidate(n, path, proposal['family'])
                        finally:
                            path.unlink(missing_ok=True)
                    if not improved and proposed['exhausted']:
                        state['stage'] += 1
                        if state['stage'] == len(stages):
                            state.update(done=True, stop_reason='stagnation')
                except TimeoutError as error:
                    records.append(dict(num_cores=n, status='UNKNOWN', error=str(error)))
                    state.update(done=True, stop_reason='evaluation_timeout')
        # Final ascending pass propagates any late small-core improvement. The
        # exact written padded plan is still independently evaluated below.
        for index, n in enumerate(cores):
            state = states[n]
            state['stop_reason'] = state['stop_reason'] or 'case_time_budget'
            if index:
                previous = states[cores[index - 1]]
                if tuple(state['objective']) > tuple(previous['objective']):
                    plan = read_json(previous['plan'])
                    plan['core_schedules'] += [[] for _ in range(n - cores[index - 1])]
                    write_json(state['plan'], plan)
                    state['objective'] = list(previous['objective'])
                    state['winner'] = 'final_empty_core_inheritance'
                    state['padded'] = True
            checkpoint(f'final_replay_N{n}')
            submitted = state['directory'] / f'{graph_path.stem}_multicore_res.json'
            shutil.copyfile(state['plan'], submitted)
            stamp = time.monotonic()
            final = replay(graph_path, submitted, settings, state['directory'] / 'verified', timeout())
            if objective(final) != tuple(state['objective']) or objective(final) > tuple(state['seed_objective']):
                raise AssertionError('final replay objective mismatch/regression')
            if not state.get('padded') and normalized(final) != normalized(read_json(state['result'])):
                raise AssertionError('full final replay differs from the search result')
            audit = worker.call(dict(action='audit', plan=str(submitted),
                                      result=str(state['directory'] / 'verified/result.json')), timeout())
            if audit['status'] != 'AUDITED':
                raise AssertionError(f'final physical audit failed: {audit}')
            write_json(state['directory'] / 'audit.json', audit['audit'])
            shutil.copyfile(state['directory'] / 'verified/result.json', state['result'])
            t2, t23, t3 = state['q2_objective'][0], state['seed_objective'][0], final['makespan']
            ratio = lambda a, b: a / b if b else (1.0 if a == 0 else None)
            row = dict(case=graph_path.stem, num_cores=n, status='SUCCESS_VERIFIED',
                       t2_cycles=t2, t2_to_3_cycles=t23, t3_cycles=t3,
                       hardware_speedup=ratio(t2, t23), algorithm_speedup=ratio(t23, t3), cache_speedup=ratio(t2, t3),
                       no_l2_added_copy_bytes=state['q2_objective'][1], added_copy_bytes=objective(final)[1],
                       **{k: final['data_movement_bytes'][k] for k in ('scheduled_copy_bytes', 'spill_added_copy_bytes')},
                       cache_hit_rate_bytes=final['cache_stats']['hit_rate'], cache_hit_bytes=final['cache_stats']['hit_bytes'],
                       cache_miss_bytes=final['cache_stats']['miss_bytes'], actual_ddr_bytes=audit['audit']['actual_ddr_bytes'],
                       used_cores=sum(bool(c['ops']) for c in final['per_core_timeline']),
                       winner=state['winner'], candidates=state['tried'], stop_reason=state['stop_reason'],
                       search_complete=state['stop_reason'] == 'stagnation',
                       q2_source_kind=state['origin']['kind'], q2_generated_seconds=state['origin']['generated_seconds'],
                       q2_result_reused=bool(state['origin'].get('frozen_objective')),
                       final_verification_seconds=time.monotonic() - stamp,
                       plan_sha256=file_hash(submitted), result_sha256=file_hash(state['directory'] / 'verified/result.json'))
            write_json(state['directory'] / 'metrics.json', row)
            rows.append(row)
        worker.close()
        if file_hash(graph_path) != identity['input_sha256'] or source_hashes() != hashes:
            raise AssertionError('input or implementation changed during this run')
        elapsed = time.monotonic() - started
        q3_seconds = elapsed - prerequisite_seconds
        if q3_seconds > options['seconds']:
            raise TimeoutError('verified outputs exceeded the configured graph-wide budget')
        for row in rows:
            row.update(case_q3_seconds=q3_seconds, case_prerequisite_seconds=prerequisite_seconds,
                       case_end_to_end_seconds=elapsed)
        write_json(attempt / 'search.json', records)
        write_json(attempt / 'case.json', dict(status='SUCCESS_VERIFIED', fingerprint=fingerprint, identity=identity,
            q3_seconds=q3_seconds, prerequisite_seconds=prerequisite_seconds, end_to_end_seconds=elapsed,
            python_version=platform.python_version(), rows=rows))
        files = {p.relative_to(attempt).as_posix(): file_hash(p) for p in attempt.rglob('*')
                 if p.is_file() and not p.is_relative_to(scratch)}
        write_json(attempt / 'complete.json', dict(status='SUCCESS_VERIFIED', fingerprint=fingerprint, files=files))
        # Scratch is outside the published manifest; remove only files owned by
        # this attempt. No recursive deletion or computed parent cleanup.
        for p in scratch.iterdir():
            if p.is_file():
                p.unlink()
        scratch.rmdir()
        retry_locked(attempt.rename, destination)
        return rows
    except BaseException as error:
        worker.close()
        write_json(attempt / 'failure.json', dict(status='FAILED', error=str(error), traceback=traceback.format_exc()))
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return [dict(case=graph_path.stem, num_cores=n, status='FAILED', error=str(error),
                     evidence=str(attempt), case_end_to_end_seconds=time.monotonic() - started) for n in cores]


def batch(graphs, cores, output, *, config=None, reference=ROOT / 'official', q2_source=None,
          options=None, workers=4, resume=False, plots=True):
    from .report import summarize
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    options = parameters(options)
    hashes, q2_hashes = source_hashes(), source_input_hashes(q2_source)
    expected = [(p.stem, n) for p in graphs for n in cores]
    started = time.monotonic()
    rows = []
    with output_lock(output):
        info = dict(status='RUNNING', started_at=datetime.now(timezone.utc).isoformat(),
                    python_version=platform.python_version(), workers=workers, cores=cores,
                    expected=len(expected), options=options, source_hashes=hashes, q2_input_hashes=q2_hashes,
                    input_sha256={p.name: file_hash(p) for p in graphs})
        write_json(output / 'run_info.json', info)
        kwargs = dict(config=config, reference=reference, q2_source=q2_source, options=options,
                      resume=resume, hashes=hashes, q2_hashes=q2_hashes)
        if workers == 1:
            for graph in graphs:
                finished = solve_case(graph, cores, output, **kwargs)
                rows.extend(finished)
                summarize(rows, expected, output, plots=False)
                print(json.dumps(dict(case=graph.stem, successful=sum(r['status'] == 'SUCCESS_VERIFIED' for r in finished)), ensure_ascii=False), flush=True)
        else:
            with ProcessPoolExecutor(max_workers=min(workers, len(graphs))) as pool:
                futures = {pool.submit(solve_case, graph, cores, output, **kwargs): graph for graph in graphs}
                for future in as_completed(futures):
                    graph = futures[future]
                    finished = future.result()
                    rows.extend(finished)
                    summarize(rows, expected, output, plots=False)
                    print(json.dumps(dict(case=graph.stem, successful=sum(r['status'] == 'SUCCESS_VERIFIED' for r in finished)), ensure_ascii=False), flush=True)
        result = summarize(rows, expected, output, plots=plots, export=True)
        unchanged = hashes == source_hashes()
        info.update(status='COMPLETE' if result['complete'] and unchanged else 'FAILED', successful=result['successful'],
                    batch_wall_seconds=time.monotonic() - started, sources_unchanged=unchanged)
        write_json(output / 'run_info.json', info)
    return result
