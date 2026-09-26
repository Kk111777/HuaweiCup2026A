"""Case-wide round-robin search and recovery of execution-valid checkpoints."""
from dataclasses import asdict
import json
from pathlib import Path
from time import perf_counter, process_time
import traceback

from common.artifacts import write_json, file_hash
from scheduling.budget import SearchBudget
from q2.construct import BudgetExpired
from q2.contracts import ScheduleState, ScoreRecord, LegalityRecord
from q2.evaluation import ScoredState, reconcile
from q2.official import read_json, digest
from .preparation import PreparationCache, PreparationService
from q2.state import plan_digest
from q2.pipeline import peak_rss
from . import VERSION
from .approximation import Calibration
from .control import parameters
from .engine import SearchSession
from .service import EvaluationService

SCHEMA_VERSION = 1


def fingerprint(request):
    return digest({k: request[k] for k in ('graph_sha256', 'config_sha256', 'source_signature',
        'problem_id', 'backend_version', 'cores', 'seconds', 'parameters')})


def folder_for(output, cores, single=False):
    return Path(output) if single else Path(output)/f'N{cores}'


def worker(request, output, started, single=False, problem=None):
    from scheduling.api import source_signature
    from scheduling.registry import resolve_problem
    problem = resolve_problem(problem or request['problem_id'])
    output = Path(output)
    budget = SearchBudget(request['seconds'], started)
    options = parameters(request['parameters'])
    cpu_start = process_time()
    sessions, per_core = {}, {}
    active_core, active_phase = None, 'input'
    shared_seconds = 0.

    def progress(phase, cores=None):
        nonlocal active_phase, active_core
        active_phase, active_core = phase, cores
        write_json(output/'progress.json', dict(phase=phase, cores=cores, budget=budget.metadata(),
                    wall_seconds=budget.elapsed(), cpu_seconds=process_time()-cpu_start,
                    per_core=per_core, shared_preparation_seconds=shared_seconds))

    def publish(session):
        folder = folder_for(output, session.cores, single)
        answer = session.result()
        values = dict(schema_version=SCHEMA_VERSION, algorithm_version=VERSION,
                      fingerprint=fingerprint(request), candidates=[asdict(r) for r in answer.incumbents.candidates],
                      stop_reason=answer.stop_reason, batches=answer.batches, search_complete=answer.search_complete,
                      beta=json.loads(answer.beta_json), statistics=json.loads(answer.statistics_json),
                      model_statistics=dict(session.service.model.stats),
                      calibration=session.service.model.calibration.record(),
                      cost_model=session.service.model.costs.record(), cache=session.service.preparation.cache.record(),
                      baseline_guard=session.protection.record() if session.protection else dict(enabled=False),
                      official_calls=session.service.calls, windows=session.windows,
                      case_elapsed_seconds=budget.elapsed(), timing=per_core.get(session.cores, {}),
                      peak_rss_bytes=peak_rss())
        write_json(folder/'checkpoint.json', values)

    try:
        if source_signature(problem) != request['source_signature']: raise ValueError('solver source changed before startup')
        if file_hash(Path(request['graph'])) != request['graph_sha256'] or file_hash(Path(request['config'])) != request['config_sha256']:
            raise ValueError('input/config changed after request creation')
        progress('shared_preparation')
        settings = problem.execution.read_settings(config=request['config'])
        g = problem.execution.create_model(read_json(request['graph']), settings)
        cache = PreparationCache(options['cache_bytes'])
        calibration = Calibration()
        preparation = PreparationService(g, cache)
        shared_seconds = budget.elapsed()
        features = problem.prediction.recognize(g, problem.execution.settings_dict(settings))
        write_json(output/'features.json', features)
        for cores in request['cores']:
            folder = folder_for(output, cores, single)
            folder.mkdir(parents=True, exist_ok=True)
            write_json(folder/'request.json', dict(request, requested_core_count=cores))
            def emit(record, destination=folder/'candidate_log.jsonl'):
                with destination.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False)+'\n')
            service = EvaluationService(g, request['graph'], folder, preparation=preparation, calibration=calibration)
            sessions[cores] = SearchSession(service, cores, options, budget, emit=emit, checkpoint=publish)
            per_core[cores] = dict(wall_seconds=0., cpu_seconds=0.)

        # Checkpoint every requested count before giving any count a search batch.
        for index, (cores, session) in enumerate(sessions.items()):
            if not budget.can_start(.01): break
            remaining = len(sessions)-index
            session.turn_deadline = perf_counter()+max(.01, (budget.target-budget.elapsed())/remaining)
            t, c = perf_counter(), process_time()
            progress('initial_checkpoint', cores)
            try:
                session.initialize()
            except (BudgetExpired, MemoryError) as error:
                session.record('initialization_unknown', reason=type(error).__name__)
            finally:
                per_core[cores]['wall_seconds'] += perf_counter()-t
                per_core[cores]['cpu_seconds'] += process_time()-c
                publish(session)

        # Complete the baseline's candidate set inside these same sessions,
        # round-robin by candidate, before spending the local-search allowance.
        guards = [s for s in sessions.values() if s.protection is not None]
        while guards and budget.can_start(.01):
            following = []
            for position, session in enumerate(guards):
                if not budget.can_start(.01): break
                limit = budget.extension if budget.extended else budget.target
                session.turn_deadline = perf_counter()+max(.01,(limit-budget.elapsed())/(len(guards)-position))
                t, c = perf_counter(), process_time()
                progress('baseline_guard', session.cores)
                try:
                    session.protection.step()
                except (BudgetExpired, MemoryError) as error:
                    session.protection.unknown = True
                    session.protection.phase = 'done'
                    session.protection.stop_reason = type(error).__name__
                    session.record('baseline_guard_unknown', reason=type(error).__name__)
                finally:
                    per_core[session.cores]['wall_seconds'] += perf_counter()-t
                    per_core[session.cores]['cpu_seconds'] += process_time()-c
                    publish(session)
                if session.protection.phase != 'done': following.append(session)
            guards = following

        active = [s for s in sessions.values() if s.pool.incumbents.search is not None and s.stop_reason is None]
        while active and budget.can_start(.01):
            following = []
            for position, session in enumerate(active):
                if not budget.can_start(.01): break
                limit = budget.extension if budget.extended else budget.target
                session.turn_deadline = min(budget.started+budget.hard,
                    perf_counter()+max(.01, (limit-budget.elapsed())/(len(active)-position)))
                t, c = perf_counter(), process_time()
                progress('search_batch', session.cores)
                try:
                    session.step_batch()
                except (BudgetExpired, MemoryError) as error:
                    session.record('turn_unknown', reason=type(error).__name__)
                finally:
                    per_core[session.cores]['wall_seconds'] += perf_counter()-t
                    per_core[session.cores]['cpu_seconds'] += process_time()-c
                    publish(session)
                if session.stop_reason is None: following.append(session)
            active = following
        for session in sessions.values():
            if session.protection is not None:
                t, c = perf_counter(), process_time()
                progress('guard_publish_verification', session.cores)
                session.turn_deadline = min(budget.started+budget.hard, perf_counter()+max(0.,
                    (budget.extension if budget.extended else budget.target)-budget.elapsed()))
                session.protection.finish()
                per_core[session.cores]['wall_seconds'] += perf_counter()-t
                per_core[session.cores]['cpu_seconds'] += process_time()-c
            if session.stop_reason is None:
                session.stop_reason = 'CASE_'+budget.stop_reason.upper()
            publish(session)
        if source_signature(problem) != request['source_signature']: raise ValueError('solver source changed during execution')
        if file_hash(Path(request['graph'])) != request['graph_sha256'] or file_hash(Path(request['config'])) != request['config_sha256']:
            raise ValueError('input/config changed during execution')
        progress('complete')
        write_json(output/'worker.json', dict(status='ok', budget=budget.metadata(), per_core=per_core,
            shared_preparation_seconds=shared_seconds, wall_seconds=budget.elapsed(), cpu_seconds=process_time()-cpu_start,
            cache=cache.record(), peak_rss_bytes=peak_rss()))
    except BaseException as error:
        write_json(output/'worker.json', dict(status='failed', phase=active_phase, cores=active_core,
            error=str(error), traceback=traceback.format_exc(), budget=budget.metadata(), per_core=per_core,
            shared_preparation_seconds=shared_seconds, wall_seconds=budget.elapsed(), cpu_seconds=process_time()-cpu_start))


def read_checkpoint(path, request):
    data = read_json(path)
    if (data.get('schema_version'), data.get('algorithm_version'), data.get('fingerprint')) != (
            SCHEMA_VERSION, VERSION, fingerprint(request)):
        raise ValueError('optimized checkpoint identity/version differs')
    rows = []
    for row in data['candidates']:
        state = ScheduleState.from_record(row['state'])
        if plan_digest(state.plan) != state.plan_digest: raise ValueError('checkpoint plan identity differs')
        rows.append(ScoredState(state, ScoreRecord(**row['score']), LegalityRecord(**row['legality'])))
    return data, reconcile(rows)


def recover(request, output, started, single, expired, telemetry):
    output = Path(output)
    outcome = read_json(output/'worker.json') if (output/'worker.json').exists() else {}
    progress = read_json(output/'progress.json') if (output/'progress.json').exists() else {}
    policy = parameters(request['parameters'])['output_policy']
    elapsed = perf_counter()-started
    rows = []
    for cores in request['cores']:
        folder = folder_for(output, cores, single)
        checkpoint = folder/'checkpoint.json'
        row = dict(cores=cores, status='NO_VALID_STATE', score_source=None, makespan=None,
                   officially_evaluated_final=False, independent_replay=False)
        if checkpoint.exists():
            data, incumbents = read_checkpoint(checkpoint, request)
            if expired and progress.get('cores') == cores:
                # Last checkpoint can precede a killed in-flight operation.
                # Account its wall time, rather than reporting only finished work.
                timing = dict(progress.get('per_core', {}).get(str(cores), data['timing']))
                timing['wall_seconds'] = timing.get('wall_seconds', 0)+max(0., elapsed-progress.get('wall_seconds', elapsed))
                timing['cpu_seconds_lower_bound'] = True
                timing['interrupted_phase'] = progress.get('phase')
                data['timing'] = timing
            guard = data.get('baseline_guard', dict(enabled=False))
            chosen = incumbents.search if policy == 'search' and not guard['enabled'] else incumbents.exact
            if chosen is None:
                row['status'] = 'NO_EXACT_STATE' if policy == 'best-exact' or guard['enabled'] else 'NO_VALID_STATE'
            else:
                state = chosen.state
                status = 'SUCCESS_EXACT_UNREPLAYED' if chosen.score.exact else 'SUCCESS_ESTIMATED'
                write_json(folder/'plan.json', state.plan)
                write_json(folder/'state.json', asdict(state))
                write_json(folder/'score.json', asdict(chosen.score))
                write_json(folder/'legality.json', asdict(chosen.legality))
                write_json(folder/'pool.json', [asdict(r) for r in incumbents.candidates])
                write_json(folder/'beta.json', data['beta'])
                write_json(folder/'timings.json', dict(data['timing'], model=data['model_statistics'],
                            official_calls=data['official_calls'], cache=data['cache'], peak_rss_bytes=data['peak_rss_bytes']))
                row.update(status=status, makespan=chosen.score.makespan, score_source=chosen.score.source,
                    added_copy_bytes=chosen.score.added_copy_bytes, plan_digest=state.plan_digest,
                    state_signature=state.state_signature, execution_digest=state.execution_digest,
                    batches=data['batches'], stop_reason=data['stop_reason'], search_complete=data['search_complete'],
                    officially_evaluated_final=chosen.score.exact, timing=data['timing'])
                row['baseline_guard'] = guard
                if guard['enabled'] and guard['baseline_makespan'] is not None:
                    protected = (guard['baseline_makespan'], guard['baseline_added_copy_bytes'])
                    if (chosen.score.makespan, chosen.score.added_copy_bytes) > protected:
                        raise ValueError('published plan regresses against measured baseline')
                files = ('plan.json', 'state.json', 'score.json', 'legality.json', 'pool.json', 'beta.json', 'timings.json')
                write_json(folder/'frozen_manifest.json', dict(schema_version=SCHEMA_VERSION, algorithm_version=VERSION,
                    kind='q2_optimized_frozen_output', fingerprint=fingerprint(request), status=status,
                    artifacts_sha256={name: file_hash(folder/name) for name in files}))
        write_json(folder/'summary.json', row)
        rows.append(row)
    complete = all(row['makespan'] is not None for row in rows)
    ok = complete and not expired and outcome.get('status') == 'ok'
    result = dict(status='ok' if ok else 'hard_timeout_with_checkpoint' if expired and complete else 'failed',
        algorithm_version=VERSION, schema_version=SCHEMA_VERSION, problem_id=request['problem_id'],
        fingerprint=fingerprint(request), rows=rows, cores=request['cores'], coverage_complete=complete,
        budget_scope='case_all_cores', target_seconds=request['seconds'], extension_seconds=request['seconds']*1.5,
        hard_limit_seconds=request['seconds']*2, wall_seconds=elapsed,
        cpu_seconds=telemetry.get('cpu_seconds', outcome.get('cpu_seconds')),
        exceeded_target=elapsed>request['seconds'], exceeded_extension=elapsed>request['seconds']*1.5,
        extended=outcome.get('budget', progress.get('budget', {})).get('extended', False), hard_timeout=expired,
        shared_preparation_seconds=outcome.get('shared_preparation_seconds', progress.get('shared_preparation_seconds')),
        peak_rss_bytes=outcome.get('peak_rss_bytes'), baseline_policy=(
            'protected_current_run' if request['parameters'].get('baseline_guard', True) else 'not_required_for_search'),
        official_replay_in_search=False, source_signature=request['source_signature'],
        error=outcome.get('error'), traceback=outcome.get('traceback'))
    write_json(output/('case_summary.json' if single else 'summary.json'), result)
    if ok:
        write_json(output/'complete.json', dict(schema_version=SCHEMA_VERSION, algorithm_version=VERSION,
                    fingerprint=fingerprint(request), status='OPTIMIZATION_COMPLETE',
                    summary_sha256=file_hash(output/('case_summary.json' if single else 'summary.json'))))
    return result
