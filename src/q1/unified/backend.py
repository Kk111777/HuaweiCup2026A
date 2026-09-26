"""Q1 bindings: the existing algorithms, defaults and result semantics unchanged."""
from scheduling.contracts import (ExecutionModel, PlanSemantics, PredictionModel,
                                  ProblemDefinition, ProposalSet)

from .. import official
from ..graph import GraphIndex
from ..model import Graph
from ..plan import objective, plan_digest, validate_plan
from ..improvement.incremental import IncrementalEvaluator, EvaluationPolicy
from ..improvement.operators import initial_candidates, local_candidates, _retain_states
from ..improvement.regions import rebuild_regions
from ..improvement.schedule import schedule, cheap_plan_proxy, CHEAP_PROXY_MODE, CHEAP_PROXY_NOTE
from .construction import Construction
from .features import recognize, plan_complexity, OfficialCostModel, control_profile
from .seed import structural_plan


def default_parameters():
    from pathlib import Path
    import json
    path = Path(__file__).resolve().parents[3] / 'configs' / 'unified.json'
    return json.loads(path.read_text(encoding='utf-8'))


def settings_dict(settings):
    return dict(capacity=settings.capacity, bandwidth=settings.bandwidth,
                task_same_core_wait_cycles=settings.same_core_wait,
                task_cross_core_wait_cycles=settings.cross_core_wait)


def preanalysis_key(plan):
    return tuple(sorted((int(v), task) for v, task in plan['node_to_subgraph'].items()))


def empty_result(cores):
    return dict(scene='A', evaluation_source='approximate', makespan=0, num_cores=cores,
        per_core_timeline=[dict(core_id=n,tasks=[],subgraphs=[],ops=[]) for n in range(cores)],
        step3_by_task={}, data_movement_bytes=dict(added_copy_bytes=0,scheduled_copy_bytes=0))


def partition_signature(graph, plan, cores=None):
    """Preserve Q1's partition-only state identity, including its current limits."""
    return frozenset(validate_plan(graph, plan, cores))


def get_problem_definition(bindings=None):
    """Build explicit bindings; the old engine wrapper supplies patched symbols.

    Production uses these defaults. Compatibility injection does not mutate
    modules, replace sys.modules entries or affect another concurrent search.
    """
    defaults = dict(validate_plan=validate_plan, plan_digest=plan_digest, objective=objective,
        IncrementalEvaluator=IncrementalEvaluator, EvaluationPolicy=EvaluationPolicy,
        OfficialCostModel=OfficialCostModel, recognize=recognize,
        plan_complexity=plan_complexity, cheap_plan_proxy=cheap_plan_proxy,
        CHEAP_PROXY_MODE=CHEAP_PROXY_MODE, CHEAP_PROXY_NOTE=CHEAP_PROXY_NOTE,
        initial_candidates=initial_candidates, local_candidates=local_candidates,
        rebuild_regions=rebuild_regions, schedule=schedule, _retain_states=_retain_states,
        control_profile=control_profile)
    if bindings is not None:
        unknown = set(bindings) - defaults.keys()
        if unknown:
            raise ValueError(f'unknown Q1 compatibility bindings: {sorted(unknown)}')
        defaults.update(bindings)
    b = defaults
    signature = partition_signature
    if b['validate_plan'] is not validate_plan:
        validator = b['validate_plan']
        signature = lambda graph, plan, cores=None: frozenset(validator(graph, plan, cores))
    return ProblemDefinition(id='q1', version='1',
        semantics=PlanSemantics(validate=b['validate_plan'], key=b['plan_digest'],
                                objective=b['objective'], state_signature=signature),
        execution=ExecutionModel(read_settings=official.Settings.read, settings_dict=settings_dict,
            create_model=Graph, create_index=GraphIndex, evaluate=official.evaluate,
            preanalyze=official.preanalyze, preanalysis_key=preanalysis_key,
            errors=official.SCHEDULING_ERRORS, empty_result=empty_result),
        prediction=PredictionModel(create_estimator=b['IncrementalEvaluator'],
            create_policy=b['EvaluationPolicy'], create_cost_model=b['OfficialCostModel'],
            recognize=b['recognize'], complexity=b['plan_complexity'], proxy=b['cheap_plan_proxy'],
            proxy_mode=b['CHEAP_PROXY_MODE'], proxy_note=b['CHEAP_PROXY_NOTE']),
        proposals=ProposalSet(seed=structural_plan, create_constructor=Construction,
            initial=b['initial_candidates'], local=b['local_candidates'], rebuild=b['rebuild_regions'],
            schedule=b['schedule'], retain=b['_retain_states'], control_profile=b['control_profile'],
            controlled_parameters=('stage_widths', 'stage_batches', 'max_region_candidates',
                'max_structural_candidates', 'max_proposals_per_round',
                'max_cuts_per_task', 'memory_merges')),
        default_parameters=default_parameters, artifact_files=('configs/unified.json',))
