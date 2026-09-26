"""Legality, externally supplied estimates and incumbent reconciliation.

No prediction formula or optimization loop is implemented here. In particular,
validating execution prepares official local tasks but does not call Scene B's
global event simulation. Generic components never reserve final replay time.
"""
from dataclasses import dataclass, replace
import math
import multiprocessing
import time
import traceback
from .contracts import LegalityRecord, ScoreRecord, ScheduleState
from .preparation import PreparationService
from .state import validate_state
from .construct import BudgetExpired


def binding(state):
    return {name: getattr(state, name) for name in
            ('state_signature', 'plan_digest', 'execution_digest', 'environment_digest')}


def check_binding(state, record):
    if any(getattr(record, name) != value for name, value in binding(state).items()):
        raise ValueError('record belongs to a different state or execution environment')


def _execution_check(raw, plan, settings):
    from evaluation_validation import validate_execution
    from multicore_cut_evaluate_problem_2 import _build_scene_b_tasks
    tasks, links, *_ = _build_scene_b_tasks(raw, plan, settings.bandwidth, settings.capacity)
    validate_execution(tasks, links)


def _legality_worker(raw, plan, root, config, capacity, bandwidth, sender):
    from .official import Settings
    settings = replace(Settings.read(root, config), capacity=capacity, bandwidth=bandwidth)
    from evaluation_validation import EvaluationValidationError
    from stub_multicore_cut_and_schedule import MulticoreCutError
    from multicore_cut_evaluate_problem_2 import SceneBEvaluationError
    from schedule_step2 import Step2SchedulingError
    from schedule_step3 import Step3SchedulingError
    try:
        _execution_check(raw, plan, settings)
        sender.send(('VALID', None))
    except MemoryError:
        sender.send(('UNKNOWN', 'MemoryError'))
    except (EvaluationValidationError, MulticoreCutError, SceneBEvaluationError,
            Step2SchedulingError, Step3SchedulingError) as error:
        sender.send(('INVALID', str(error)))
    except BaseException:
        sender.send(('INTERNAL_ERROR', traceback.format_exc()))
    finally:
        sender.close()


def _bounded_execution_check(g, state, seconds, check):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError('finite nonnegative validation timeout required')
    if seconds == 0: return 'UNKNOWN', 'TimeoutError'
    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_legality_worker,
        args=(g.raw, state.plan, g.settings.root, g.settings.config, g.settings.capacity, g.settings.bandwidth, sender))
    started = False; deadline = time.monotonic()+seconds
    try:
        child.start(); started = True; sender.close()
        while True:
            check(); remaining = deadline-time.monotonic()
            if remaining <= 0: return 'UNKNOWN', 'TimeoutError'
            if receiver.poll(min(.02, remaining)):
                try: status, reason = receiver.recv()
                except EOFError as error: raise RuntimeError('legality worker exited without a result') from error
                if status == 'INTERNAL_ERROR': raise RuntimeError(reason)
                return status, reason
            if not child.is_alive(): raise RuntimeError(f'legality worker exited {child.exitcode}')
    finally:
        receiver.close(); sender.close()
        if started:
            if child.is_alive(): child.terminate()
            child.join(); child.close()


class ExecutionValidator:
    def __init__(self, graph, preparation=None):
        self.g = graph; self.preparation = preparation or PreparationService(graph)

    def validate(self, state, *, level='execution', check=lambda: None, timeout_seconds=None):
        if level not in ('structural', 'execution'): raise ValueError('unknown validation level')
        from evaluation_validation import EvaluationValidationError
        from stub_multicore_cut_and_schedule import MulticoreCutError
        from multicore_cut_evaluate_problem_2 import SceneBEvaluationError
        from schedule_step2 import Step2SchedulingError
        from schedule_step3 import Step3SchedulingError
        # A stale state is a caller/programming error, not an illegal graph.
        try:
            check()
            validate_state(self.g, state, preparation=self.preparation)
            if level == 'execution':
                key = (state.environment_digest, state.execution_digest)
                cached = self.preparation.cache.get('legality', key)
                if cached is not None:
                    return LegalityRecord(**binding(state), level=level, **cached)
                if timeout_seconds is None:
                    _execution_check(self.g.raw, state.plan, self.g.settings)
                    status, reason = 'VALID', None
                else:
                    status, reason = _bounded_execution_check(self.g, state, timeout_seconds, check)
                check()
                self.preparation.cache.stats['execution_validations'] += 1
                if status == 'VALID': self.preparation.cache.put('legality', key, dict(status=status, reason=reason))
                return LegalityRecord(**binding(state), level=level, status=status, reason=reason)
            return LegalityRecord(**binding(state), level=level, status='VALID')
        except (BudgetExpired, TimeoutError, MemoryError) as error:
            return LegalityRecord(**binding(state), level=level, status='UNKNOWN', reason=type(error).__name__)
        except (EvaluationValidationError, MulticoreCutError, SceneBEvaluationError,
                Step2SchedulingError, Step3SchedulingError) as error:
            return LegalityRecord(**binding(state), level=level, status='INVALID', reason=str(error))


def evaluate_approximate(state, legality, evaluator, *, check=lambda: None):
    """The supplied model must return an honestly tagged, versioned ScoreRecord."""
    check_binding(state, legality)
    if (legality.level, legality.status) != ('execution', 'VALID'):
        raise ValueError('approximate scoring requires exact execution legality')
    check(); score = evaluator(state); check()
    if not isinstance(score, ScoreRecord) or score.source != 'approximate':
        raise ValueError('external estimator must return an approximate ScoreRecord')
    check_binding(state, score)
    return score


def exact_score(state, objective, *, evidence_path=None, evidence_plan_digest=None):
    source = 'official' if evidence_plan_digest in (None, state.plan_digest) else 'equivalent_official'
    return ScoreRecord(**binding(state), makespan=objective[0], added_copy_bytes=objective[1],
                       source=source, exact=True, evaluator_version='Q2-Official-2.1',
                       evidence_path=evidence_path, evidence_plan_digest=evidence_plan_digest or state.plan_digest)


def better(candidate, incumbent):
    if candidate.makespan != incumbent.makespan: return candidate.makespan < incumbent.makespan
    a, b = candidate.added_copy_bytes, incumbent.added_copy_bytes
    return a is not None and b is not None and a < b


@dataclass(frozen=True)
class ScoredState:
    state: ScheduleState
    score: ScoreRecord
    legality: LegalityRecord

    def __post_init__(self):
        check_binding(self.state, self.score); check_binding(self.state, self.legality)
        if (self.legality.level, self.legality.status) != ('execution', 'VALID'):
            raise ValueError('incumbents require confirmed execution legality')


@dataclass(frozen=True)
class Incumbents:
    search: ScoredState | None
    exact: ScoredState | None
    candidates: tuple[ScoredState, ...]


def reconcile(candidates):
    """Rerank the caller's retained pool after calibration (no hidden state pool).

    Latest estimates replace older estimates; an exact result never downgrades.
    Plan identity is distinct from search identity: two blockings can coexist.
    """
    rows = list(candidates); latest = {}; measured = {}; executions = {}
    for row in rows:
        if not isinstance(row, ScoredState): raise TypeError('ScoredState required')
        key = (row.state.environment_digest, row.state.plan_digest)
        execution_key = (row.state.environment_digest, row.state.execution_digest)
        if row.score.exact:
            if execution_key in executions and (executions[execution_key].makespan, executions[execution_key].added_copy_bytes) != (row.score.makespan, row.score.added_copy_bytes):
                raise ValueError('conflicting exact scores')
            measured[key] = row.score
            executions.setdefault(execution_key, row.score)
        state_key = (row.state.environment_digest, row.state.state_signature)
        if state_key not in latest or latest[state_key].state.plan_digest == row.state.plan_digest:
            latest[state_key] = row
    result = []
    for row in latest.values():
        key = (row.state.environment_digest, row.state.plan_digest)
        exact = measured.get(key, executions.get((row.state.environment_digest, row.state.execution_digest)))
        if exact is not None:
            donor = exact.evidence_plan_digest or exact.plan_digest
            row = replace(row, score=replace(exact, **binding(row.state), evidence_plan_digest=donor,
                source='official' if donor == row.state.plan_digest else 'equivalent_official'))
        result.append(row)
    best = exact = None
    for row in result:
        if best is None or better(row.score, best.score): best = row
        if row.score.exact and (exact is None or better(row.score, exact.score)): exact = row
    return Incumbents(best, exact, tuple(result))
