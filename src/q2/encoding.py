"""Submission encoders and actual Step1 computation-projection checks."""
import heapq
import json
from .contracts import EncodedCandidate

def validate_order(g, core_by_op, order, cores: int) -> None:
    if len(core_by_op) != len(g.ids) or len(order) != len(g.ids):
        raise ValueError('incomplete computation schedule')
    if set(order) != set(range(len(g.ids))):
        raise ValueError('duplicate/missing computation operation')
    if any(type(k) is not int or not 0 <= k < cores for k in core_by_op):
        raise ValueError('invalid core')
    rank = [0] * len(order)
    for i, v in enumerate(order): rank[v] = i
    if any(rank[p] >= rank[v] for v in order for p in g.preds[v]):
        raise ValueError('computation order violates a data dependency')


def encode_atomic(g, cores, core_by_op, order) -> dict:
    """Singleton sgids enforce the computation projection, NOT the full proxy timeline."""
    validate_order(g, core_by_op, order, cores)
    mapping, queues = {}, [[] for _ in range(cores)]
    for j, v in enumerate(order):
        mapping[str(g.ids[v])] = j
        queues[core_by_op[v]].append(j)
    return {'node_to_subgraph': mapping, 'core_schedules': queues}


def tail_order(g) -> tuple[int, ...]:
    degree = list(map(len, g.preds))
    ready = [(-g.b[v], g.ids[v], v) for v, n in enumerate(degree) if not n]
    heapq.heapify(ready)
    order = []
    while ready:
        _, _, v = heapq.heappop(ready); order.append(v)
        for w in g.succs[v]:
            degree[w] -= 1
            if not degree[w]: heapq.heappush(ready, (-g.b[w], g.ids[w], w))
    if len(order) != len(g.ids): raise ValueError('cyclic computation input')
    return tuple(order)


def plan_core_map(g, plan) -> tuple[int, ...]:
    by_group = {j: k for k, queue in enumerate(plan['core_schedules']) for j in queue}
    return tuple(by_group[plan['node_to_subgraph'][str(v)]] for v in g.ids)


def assert_compute_fidelity(g, bundle, core, intended_order) -> None:
    expected = [[] for _ in bundle['cores']]
    for v in intended_order: expected[core[v]].append(g.ids[v])
    for k, c in bundle['cores'].items():
        actual = [o for o in c['seq'] if o in g.dense]
        if actual != expected[k]:
            raise AssertionError(f'computation projection mismatch on core {k}')



def encode_candidate(g, cores, draft, encoding):
    if encoding not in ('legacy', 'commit', 'tail'):
        raise ValueError('unknown encoding')
    order = None if encoding == 'legacy' else (draft.commit_order if encoding == 'commit' else tail_order(g))
    plan = draft.legacy_plan if order is None else encode_atomic(g, cores, draft.operation_core, order)
    return EncodedCandidate(json.dumps(plan), draft, encoding, order)


def validate_fidelity(g, bundle, candidate):
    intended = candidate.intended_order
    reference = candidate.draft.commit_order if intended is None else intended
    expected = [[] for _ in candidate.plan['core_schedules']]
    for v in reference:
        expected[candidate.draft.operation_core[v]].append(g.ids[v])
    differences = []
    for k, c in bundle['cores'].items():
        actual = [o for o in c['seq'] if o in g.dense]
        if actual != expected[k]:
            at = next((i for i, pair in enumerate(zip(actual, expected[k])) if pair[0] != pair[1]),
                      min(len(actual), len(expected[k])))
            differences.append(dict(core=k, first_difference=at,
                                    actual=actual[at:at+1], expected=expected[k][at:at+1]))
    if intended is not None and differences:
        raise AssertionError(f'computation projection mismatch: {differences}')
    return dict(policy=candidate.encoding, required=intended is not None,
                compute_fidelity_verified=not differences, differences=differences)


def realized_blocks(g, plan, bundle, draft, max_operations=32):
    """Normalize original logical groups onto actual contiguous core queues.

    Never move an sgid across another block or split a submitted sgid. Large
    indivisible sgids are allowed. Legacy winners use actual execution, not commit.
    """
    logical = {v: i for i, block in enumerate(draft.search_blocks) for v in block}
    result = {}
    for k, core in bundle['cores'].items():
        members = {}
        for op in core['seq']:
            if op in g.dense:
                members.setdefault(core['op_subgraph'][op], []).append(g.dense[op])
        blocks, active, count, previous = [], [], 0, None
        for sg in core['subgraph_order']:
            nodes = members.get(sg, [])
            identity = tuple(sorted({logical[v] for v in nodes}))
            if active and (identity != previous or count + len(nodes) > max_operations):
                blocks.append(tuple(active)); active, count = [], 0
            active.append(sg); count += len(nodes); previous = identity
        if active: blocks.append(tuple(active))
        result[k] = tuple(blocks)
    return result
