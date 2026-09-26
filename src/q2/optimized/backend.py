"""Complete Scene B bindings for the common scheduling API."""
from scheduling.contracts import (CaseRuntime, ExecutionModel, PlanSemantics,
                                  PredictionModel, ProblemDefinition, ProposalSet)
from q2.official import Settings, objective
from q2.graph import Graph
from .preparation import PreparationService
from q2.state import plan_digest, build_state
from q2.encoding import plan_core_map
from q2.affinity import component_candidates
from q2.evaluation import reconcile
from . import VERSION
from .control import default_parameters, parameters
from .construction import Construction
from .approximation import ModelEvaluator, CostModel
from .proposals import order_proposals, feedback_proposals, rebuild_proposals, block_proposals
from .runtime import worker, recover


def create_model(raw, settings=None):
    return Graph(raw, settings or Settings.read())


def settings_dict(settings):
    return dict(capacity=dict(settings.capacity), bandwidth=settings.bandwidth, cross_core_copy_delay_cycles=settings.delay)


def validate_plan(graph, plan, cores=None):
    from stub_multicore_cut_and_schedule import derive_multicore_plan
    if cores is not None and len(plan['core_schedules']) != cores: raise ValueError('core count differs')
    return derive_multicore_plan(graph.raw, plan)


def signature(graph, plan, cores=None):
    validate_plan(graph, plan, cores)
    return build_state(graph, plan).state_signature


def create_index(graph, settings=None):
    if settings is not None and graph.settings != settings: raise ValueError('graph/settings differ')
    return PreparationService(graph)


def evaluate(raw, plan, settings):
    return settings.evaluate(raw, plan)


def preanalyze(graph, plan, settings=None):
    service = create_index(graph, settings)
    state = service.prepare(plan)
    return service.prepare_execution(state)


def create_estimator(graph, settings=None, options=None):
    return ModelEvaluator(graph)


def recognize(graph, settings=None):
    return dict(computations=len(graph.ids), tensors=len(graph.tensors), components=len(graph.components),
                matrix_work=graph.work[0], vector_work=graph.work[1], scene='B')


def complexity(graph, plan, settings=None):
    core = plan_core_map(graph, plan)
    sizes = [core.count(k) for k in range(len(plan['core_schedules']))]
    return dict(computations=len(core), compute_squared_sum=sum(n*n for n in sizes), cores=len(sizes))


def proxy(graph, plan, settings=None):
    """Necessary compute/DDR work only, never an official or model makespan."""
    core = plan_core_map(graph, plan)
    loads = [[0, 0] for _ in plan['core_schedules']]
    for v, k in enumerate(core): loads[k][graph.pipe[v]] += graph.duration[v]
    return max(max(map(max, loads), default=0), graph.base_traffic(core)[0], max(graph.b, default=0))


def seed(graph, cores, settings=None):
    candidate = next(component_candidates(graph, cores))
    return candidate.plan, dict(family='component', encoding=candidate.encoding)


def empty_result(cores):
    return dict(scene='B', evaluation_source='approximate', makespan=0, num_cores=cores,
                data_movement_bytes=dict(added_copy_bytes=0, spill_added_copy_bytes=0),
                per_core_timeline=[dict(core_id=k, ops=[]) for k in range(cores)])


def get_problem_definition():
    return ProblemDefinition(id='q2', version=VERSION,
        semantics=PlanSemantics(validate_plan, plan_digest, objective, signature),
        execution=ExecutionModel(Settings.read, settings_dict, create_model, create_index, evaluate,
                                 preanalyze, plan_digest, (ValueError,), empty_result),
        prediction=PredictionModel(create_estimator, parameters, CostModel, recognize, complexity, proxy,
                                   'necessary_base_work_bound', 'Necessary work bound; not a full execution estimate.'),
        proposals=ProposalSet(seed, Construction, order_proposals, feedback_proposals,
                              rebuild_proposals, block_proposals, reconcile, parameters),
        default_parameters=default_parameters, supported_cores=(1, 2, 3, 4, 5),
        artifact_files=('configs/q2/optimized.json',), case_runtime=CaseRuntime(worker, recover))
