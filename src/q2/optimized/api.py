"""Optimized case/batch entrypoints and independent frozen-output replay."""
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
import json

from common.artifacts import write_json, file_hash
from problem_1_baseline import output_lock
from scheduling.api import solve_case as common_solve_case, source_signature
from q2.official import Settings, read_json, digest
from q2.pipeline import run_official
from q2.contracts import ScheduleState, ScoreRecord, LegalityRecord
from q2.evaluation import ScoredState
from q2.state import plan_digest
from . import VERSION
from .control import parameters
from .runtime import SCHEMA_VERSION, fingerprint

FROZEN_FILES = {'plan.json', 'state.json', 'score.json', 'legality.json', 'pool.json', 'beta.json', 'timings.json'}


def solve_case(graph, config, cores, output, *, seconds=600, options=None):
    return common_solve_case(graph, config, cores, output, seconds=seconds,
                             parameters=parameters(options), problem='q2')


def frozen_output(directory):
    directory = Path(directory)
    manifest = read_json(directory/'frozen_manifest.json')
    if (manifest.get('schema_version'), manifest.get('algorithm_version'), manifest.get('kind')) != (
            SCHEMA_VERSION, VERSION, 'q2_optimized_frozen_output'):
        raise ValueError('unknown frozen output version')
    hashes = manifest['artifacts_sha256']
    if set(hashes) != FROZEN_FILES: raise ValueError('frozen file set differs')
    if any(file_hash(directory/name) != value for name, value in hashes.items()):
        raise ValueError('frozen output was modified')
    state = ScheduleState.from_record(read_json(directory/'state.json'))
    row = ScoredState(state, ScoreRecord(**read_json(directory/'score.json')),
                      LegalityRecord(**read_json(directory/'legality.json')))
    if state.plan != read_json(directory/'plan.json') or state.plan_digest != plan_digest(state.plan):
        raise ValueError('frozen plan/state differ')
    expected = 'SUCCESS_EXACT_UNREPLAYED' if row.score.exact else 'SUCCESS_ESTIMATED'
    if manifest['status'] != expected: raise ValueError('frozen score provenance differs')
    return manifest, row


def reusable(directory, expected):
    directory = Path(directory)
    complete = read_json(directory/'complete.json')
    if (complete.get('algorithm_version'), complete.get('schema_version'), complete.get('status')) != (
            VERSION, SCHEMA_VERSION, 'OPTIMIZATION_COMPLETE'):
        raise ValueError('unknown or incomplete optimization version; not resumable')
    request = read_json(directory/'request.json')
    if complete['fingerprint'] != fingerprint(request) or complete['fingerprint'] != fingerprint(expected):
        raise ValueError('resume input/config/source/parameters differ')
    if file_hash(directory/'summary.json') != complete['summary_sha256']:
        raise ValueError('completed summary was modified')
    summary = read_json(directory/'summary.json')
    if summary['status'] != 'ok' or not summary['coverage_complete']: raise ValueError('incomplete case cannot be resumed')
    for cores in request['cores']:
        manifest, _ = frozen_output(directory/f'N{cores}')
        if manifest['fingerprint'] != complete['fingerprint']: raise ValueError('core output belongs to another case')
    return summary


def _batch_job(graph, config, cores, directory, seconds, options):
    return solve_case(graph, config, cores, directory, seconds=seconds, options=options)


def batch(graphs, config, cores, output, *, seconds=600, options=None, workers=1, resume=False):
    graphs = [Path(p).resolve() for p in graphs]
    config, output = Path(config).resolve(), Path(output).resolve()
    options = parameters(options)
    if not graphs or len({p.stem for p in graphs}) != len(graphs): raise ValueError('distinct nonempty case names required')
    if type(workers) is not int or not 1 <= workers <= 128: raise ValueError('invalid workers')
    output.mkdir(parents=True, exist_ok=True)
    signature = source_signature('q2')
    results, pending = {}, []
    with output_lock(output):
        if not resume and any(p.name != '.run.lock' for p in output.iterdir()): raise ValueError('batch output must be empty')
        request = dict(algorithm_version=VERSION, schema_version=SCHEMA_VERSION, cores=list(cores), seconds=seconds,
            parameters=options, source_signature=signature, config_sha256=file_hash(config),
            graphs={g.stem: file_hash(g) for g in graphs}, workers=workers)
        if resume:
            old = read_json(output/'batch_request.json')
            # Worker count changes scheduling only; all algorithm/source identities remain fixed.
            if {k:v for k,v in old.items() if k != 'workers'} != {k:v for k,v in request.items() if k != 'workers'}:
                raise ValueError('batch resume version/input/parameters differ')
        else: write_json(output/'batch_request.json', request)
        for graph in graphs:
            directory = output/graph.stem
            expected = dict(graph_sha256=file_hash(graph), config_sha256=file_hash(config), source_signature=signature,
                            problem_id='q2', backend_version=VERSION, cores=list(cores), seconds=seconds, parameters=options)
            if directory.exists():
                if not resume: raise ValueError('case output already exists')
                results[graph.stem] = reusable(directory, expected)
            else: pending.append(graph)
        def save():
            answer = dict(algorithm_version=VERSION, requested=len(graphs), finished=len(results),
                          successful=sum(r['status']=='ok' for r in results.values()),
                          results={name: results[name] for name in sorted(results)})
            write_json(output/'batch_summary.json', answer)
            return answer
        save()
        if workers == 1:
            for graph in pending:
                results[graph.stem] = _batch_job(graph, config, cores, output/graph.stem, seconds, options)
                save()
        elif pending:
            with ProcessPoolExecutor(max_workers=workers) as executor:
                jobs = {executor.submit(_batch_job, graph, config, cores, output/graph.stem, seconds, options): graph
                        for graph in sorted(pending, key=lambda p: (-p.stat().st_size, p.name))}
                for future in as_completed(jobs):
                    results[jobs[future].stem] = future.result(); save()
        return save()


def replay(graph, config, snapshot, output, *, timeout=600):
    """Freeze check first, then measure exactly that output; never change selection."""
    graph, snapshot, output = Path(graph).resolve(), Path(snapshot).resolve(), Path(output).resolve()
    manifest, row = frozen_output(snapshot)
    request = read_json(snapshot/'request.json')
    settings = Settings.read(config=config)
    if file_hash(graph) != request['graph_sha256'] or file_hash(settings.config) != request['config_sha256']:
        raise ValueError('replay input/config differs from frozen search')
    if output.exists(): raise FileExistsError(output)
    before = file_hash(snapshot/'plan.json')
    calls = []
    result = run_official(graph, read_json(snapshot/'plan.json'), settings, output, timeout, calls, 'offline_replay')
    frozen_output(snapshot)
    if file_hash(snapshot/'plan.json') != before: raise ValueError('plan changed during replay')
    record = dict(algorithm_version=VERSION, snapshot_fingerprint=manifest['fingerprint'],
                  frozen_plan_sha256=before, score_source=row.score.source, predicted_makespan=row.score.makespan,
                  calls=calls, status='FAILED' if result is None else 'OFFICIAL_REPLAY_OK',
                  independent_replay=True, included_in_search_budget=False)
    if result is not None:
        record.update(official_makespan=result['makespan'], data_movement_bytes=result['data_movement_bytes'],
                      prediction_error_cycles=row.score.makespan-result['makespan'])
        if row.score.exact and (row.score.makespan, row.score.added_copy_bytes) != (
                result['makespan'], result['data_movement_bytes']['added_copy_bytes']):
            record['status'] = 'EXACT_EVIDENCE_MISMATCH'
    write_json(output/'replay.json', record)
    if record['status'] == 'OFFICIAL_REPLAY_OK':
        write_json(output/'verified.json', dict(record, result_sha256=file_hash(output/'result.json')))
    return record
