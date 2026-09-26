"""Current-state validation, model access and exact evidence on disk."""
from dataclasses import replace
from pathlib import Path
import json
from time import perf_counter

from common.artifacts import write_json, file_hash
from q2.contracts import CandidateOrigin
from q2.encoding import validate_fidelity
from q2.evaluation import ScoredState, exact_score
from q2.state import validate_state, derive_state
from q2.official import objective, read_json
from .preparation import PreparationService
from .approximation import ModelEvaluator


def scheduling_errors():
    from evaluation_validation import EvaluationValidationError
    from stub_multicore_cut_and_schedule import MulticoreCutError
    from multicore_cut_evaluate_problem_2 import SceneBEvaluationError
    from schedule_step2 import Step2SchedulingError
    from schedule_step3 import Step3SchedulingError
    return (EvaluationValidationError, MulticoreCutError, SceneBEvaluationError,
            Step2SchedulingError, Step3SchedulingError)


class EvaluationService:
    def __init__(self, graph, graph_path, directory, *, preparation=None, calibration=None, official_runner=None):
        from q2.pipeline import run_official
        self.g, self.graph_path, self.directory = graph, Path(graph_path), Path(directory)
        self.preparation = preparation or PreparationService(graph)
        self.model = ModelEvaluator(graph, self.preparation, calibration)
        self.official_runner = official_runner or run_official
        self.calls = []

    def state(self, proposal, parent=None, *, check=lambda: None):
        check()
        candidate, plan = proposal.candidate, proposal.candidate.plan
        bundle = self.preparation.prefix(plan)
        # Required even on model/official cache hits.
        validate_fidelity(self.g, bundle, candidate)
        if candidate.state is not None:
            if candidate.state.plan != plan: raise ValueError('candidate state and plan differ')
            return validate_state(self.g, candidate.state, preparation=self.preparation)
        if candidate.draft is not None:
            draft = candidate.draft
            return self.preparation.prepare(plan, origin=CandidateOrigin(draft.family, draft.mode, draft.beta, candidate.encoding),
                parent_signature=proposal.parent_signature, construction_blocks_ops=draft.construction_blocks_ops)
        origin = CandidateOrigin(proposal.family, proposal.family, None, candidate.encoding)
        if parent is None:
            return self.preparation.prepare(plan, origin=origin, parent_signature=proposal.parent_signature)
        if proposal.parent_signature != parent.state_signature: raise ValueError('proposal parent differs')
        groups = proposal.parameters.get('blocks_by_core')
        return derive_state(self.g, parent, plan, preparation=self.preparation, blocks_by_core=groups, origin=origin)

    def refined(self, state, *, check=lambda: None):
        trace = self.model.evaluate(state, check=check)
        preparation = self.preparation.prepare_execution(state, check=check)
        return ScoredState(state, trace.score, preparation.legality), trace

    def cached_exact(self, state):
        key = state.environment_digest, 'optimized-official-v1', state.execution_digest
        cached = self.preparation.cache.get('exact', key)
        path = self.directory/'exact_index'/f'{state.execution_digest}.json'
        if cached is None and path.exists():
            cached = read_json(path)
            if cached['environment_digest'] != state.environment_digest:
                raise ValueError('exact cache environment differs')
            if file_hash(self.directory/cached['evidence_path']) != cached['result_sha256']:
                raise ValueError('exact evidence on disk changed')
            self.preparation.cache.put('exact', key, cached)
        if cached is None: return None
        self.preparation.cache.stats['exact_score_hits'] += 1
        return exact_score(state, tuple(cached['objective']), evidence_path=cached['evidence_path'],
                           evidence_plan_digest=cached['evidence_plan_digest'])

    def official(self, state, seconds, *, check=lambda: None):
        check()
        validate_state(self.g, state, preparation=self.preparation)
        prepared = self.preparation.prepare_execution(state, check=check)
        score = self.cached_exact(state)
        if score is not None:
            return ScoredState(state, score, prepared.legality), dict(status='CACHED_OFFICIAL', wall_seconds=0., cached=True)
        folder = self.directory/'official'/f'eval_{len(self.calls):04d}'
        result = self.official_runner(self.graph_path, state.plan, self.g.settings, folder, seconds, self.calls, 'calibration')
        record = dict(self.calls[-1], cached=False)
        if result is None:
            self.model.costs.observe('official', record['wall_seconds'], max(1, len(self.g.ids)) ** 2,
                                     timed_out=record['status'] == 'EVAL_TIMEOUT')
            return None, record
        value = objective(result)
        relative = (folder/'result.json').relative_to(self.directory).as_posix()
        metadata = dict(objective=value, evidence_path=relative, evidence_plan_digest=state.plan_digest,
                        environment_digest=state.environment_digest, execution_digest=state.execution_digest,
                        result_sha256=file_hash(folder/'result.json'))
        write_json(self.directory/'exact_index'/f'{state.execution_digest}.json', metadata)
        self.preparation.cache.put('exact', (state.environment_digest, 'optimized-official-v1', state.execution_digest), metadata)
        self.preparation.cache.stats['official_evaluations'] += 1
        self.model.costs.observe('official', record['wall_seconds'], max(1, len(self.g.ids)) ** 2)
        score = exact_score(state, value, evidence_path=relative)
        return ScoredState(state, score, prepared.legality), record
