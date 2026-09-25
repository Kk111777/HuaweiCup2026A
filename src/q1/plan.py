"""Shared partition, queue invariants and official objective."""
import hashlib
import json
from .model import topological

def quotient(graph, blocks):
    owner = {}
    for i, block in enumerate(blocks):
        if not block:
            raise ValueError("empty task")
        for v in block:
            if v in owner:
                raise ValueError("duplicate operation")
            owner[v] = i
    if owner.keys() != graph.ops.keys():
        raise ValueError("partition must cover every eligible operation exactly once")
    pred, succ = {i: set() for i in range(len(blocks))}, {i: set() for i in range(len(blocks))}
    for v in graph.order:
        for w in graph.succ[v]:
            a, b = owner[v], owner[w]
            if a != b:
                succ[a].add(b)
                pred[b].add(a)
    return owner, pred, succ, topological(pred, succ)


def validate_plan(graph, plan, cores=None):
    if set(plan) != {"node_to_subgraph", "core_schedules"}:
        raise ValueError("plan must contain exactly two official fields")
    mapping = plan["node_to_subgraph"]
    if any(type(k) is not str or k != str(int(k)) for k in mapping):
        raise ValueError("noncanonical operation key")
    if any(type(v) is not int or v < 0 for v in mapping.values()):
        raise ValueError("invalid task id")
    schedules = plan["core_schedules"]
    if not isinstance(schedules, list) or not schedules or (cores is not None and len(schedules) != cores):
        raise ValueError("wrong number of cores")
    if any(not isinstance(q, list) for q in schedules):
        raise ValueError("invalid core queue")
    tasks = sorted(set(mapping.values()))
    remap = {t: i for i, t in enumerate(tasks)}
    blocks = [set() for _ in tasks]
    for v, task in mapping.items():
        blocks[remap[task]].add(int(v))
    _, pred, succ, _ = quotient(graph, blocks)
    flat = [t for q in schedules for t in q]
    if any(type(t) is not int for t in flat) or len(flat) != len(set(flat)) or set(flat) != set(tasks):
        raise ValueError("core queues must cover tasks exactly once")
    for queue in schedules:
        for a, b in zip(queue, queue[1:]):
            a, b = remap[a], remap[b]
            pred[b].add(a)
            succ[a].add(b)
    topological(pred, succ)
    return [frozenset(b) for b in blocks]


def plan_digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def objective(result):
    return result["makespan"], result["data_movement_bytes"]["added_copy_bytes"]

def normalized(result):
    return json.loads(json.dumps({k:v for k,v in result.items() if k not in ("input_graph", "input_plan", "execution_mode")}))
