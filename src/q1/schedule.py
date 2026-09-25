"""Collaborator EFT policy using shared validation and allocation."""
from .allocation import allocate
from .model import Graph
from .plan import validate_plan, quotient


def assign_cores(graph, plan, durations, settings):
    model = graph if isinstance(graph, Graph) else Graph(graph)
    blocks = validate_plan(model, plan)
    ids = sorted(set(plan['node_to_subgraph'].values()))
    _, pred, succ, _ = quotient(model, blocks)
    costs = {i: durations[t] for i, t in enumerate(ids)}
    queues, _, records = allocate(pred, succ, costs, len(plan['core_schedules']),
        settings.same_core_wait, settings.cross_core_wait, tie='available')
    result = {'node_to_subgraph': dict(plan['node_to_subgraph']),
              'core_schedules': [[ids[i] for i in q] for q in queues]}
    for record in records:
        record['task'] = ids[record['task']]
    validate_plan(model, result)
    return result, records
