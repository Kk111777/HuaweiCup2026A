"""Current-plan preparation and three deliberately different identities."""
from dataclasses import replace
import json
from .contracts import BlockState, CandidateOrigin, ScheduleState, EncodedCandidate
from .official import digest
from .encoding import plan_core_map


def plan_digest(plan):
    # The official builder iterates mapping.items(); preserve that insertion order.
    return digest(dict(mapping_items=list(plan['node_to_subgraph'].items()),
                       core_schedules=plan['core_schedules']))


def environment_digest(g):
    return digest(dict(graph=g.raw, capacity=g.settings.capacity, bandwidth=g.settings.bandwidth,
                       delay=g.settings.delay, official=g.settings.hashes, adapter='Q2-State-2.1'))


def execution_digest(bundle, namespace):
    return digest(dict(environment=namespace,
                       cores={k: dict(graph=c['graph'], seq=c['seq']) for k,c in bundle['cores'].items()},
                       cross_links=bundle['cross_links']))


def _prepare(g, plan, bundle, namespace, *, origin=CandidateOrigin(), parent_signature=None,
             construction_blocks_ops=(), blocks_by_core=None, max_operations=32):
    """Internal builder: bundle must come from this plan's PreparationService."""
    if type(max_operations) is not int or max_operations < 1: raise ValueError('invalid block size')
    core = plan_core_map(g, plan)
    orders = tuple(tuple(g.dense[o] for o in bundle['cores'][k]['seq'] if o in g.dense)
                   for k in range(len(plan['core_schedules'])))
    if sorted(v for row in orders for v in row) != list(range(len(g.ids))):
        raise ValueError('computation coverage differs')
    logical = {}
    for i, block in enumerate(construction_blocks_ops):
        for v in block:
            if type(v) is not int or not 0 <= v < len(g.ids) or v in logical:
                raise ValueError('invalid construction operation block')
            logical[v] = str(i)
    if logical and len(logical) != len(g.ids): raise ValueError('incomplete construction blocks')
    if blocks_by_core is not None and len(blocks_by_core) != len(orders):
        raise ValueError('blocks must include every core, including empty cores')
    all_blocks, groups = [], []
    for k, order in enumerate(orders):
        c = bundle['cores'][k]
        members = {sg: [] for sg in c['subgraph_order']}
        for v in order: members[c['op_subgraph'][g.ids[v]]].append(v)
        members = {sg: tuple(nodes) for sg, nodes in members.items()}
        groups.append(tuple(members[sg] for sg in c['subgraph_order']))
        if blocks_by_core is None:
            rows, active, count, previous = [], [], 0, None
            for sg in c['subgraph_order']:
                nodes = members[sg]
                identity = tuple(sorted({logical[v] for v in nodes})) if logical else (sg,)
                if active and (identity != previous or count + len(nodes) > max_operations):
                    rows.append(tuple(active)); active, count = [], 0
                active.append(sg); count += len(nodes); previous = identity
            if active: rows.append(tuple(active))
        else:
            rows = list(blocks_by_core[k])
        flattened = tuple(sg for row in rows for sg in (row.sgids if isinstance(row, BlockState) else row))
        if flattened != tuple(c['subgraph_order']):
            raise ValueError('blocks must partition contiguous submitted sgids in actual order')
        blocks = []
        for position, row in enumerate(rows):
            sgids = tuple(row.sgids if isinstance(row, BlockState) else row)
            if not sgids: raise ValueError('empty search block')
            ops = tuple(v for sg in sgids for v in members[sg])
            if isinstance(row, BlockState):
                if (row.core, row.position, row.ops) != (k, position, ops):
                    raise ValueError('stale block operation/core/position')
                block = row
            else:
                block = BlockState(f'{k}:{position}', ops, k, sgids, position,
                                   tuple(sorted({logical[v] for v in ops})) if logical else ())
            blocks.append(block)
        if tuple(v for b in blocks for v in b.ops) != order:
            raise ValueError('block operations differ from actual computation order')
        all_blocks.append(tuple(blocks))
    if len({b.block_id for row in all_blocks for b in row}) != sum(map(len, all_blocks)):
        raise ValueError('duplicate block identifiers')
    execution = execution_digest(bundle, namespace)
    signature = digest(dict(environment=namespace, core=core, orders=orders, groups=groups,
                            blocks=[[b.ops for b in row] for row in all_blocks], execution=execution))
    return ScheduleState(json.dumps(plan), core, orders, tuple(all_blocks), namespace,
                         plan_digest(plan), signature, execution, origin, parent_signature)


def build_state(g, plan, *, preparation=None, **kwargs):
    from .preparation import PreparationService
    return (preparation or PreparationService(g)).prepare(plan, **kwargs)


def validate_state(g, state, *, preparation=None):
    """Reconstruct all authoritative fields; reject a dataclass with stale contents."""
    fresh = build_state(g, state.plan, preparation=preparation, origin=state.origin,
                        parent_signature=state.parent_signature,
                        blocks_by_core=state.realized_blocks_by_core)
    if fresh != state: raise ValueError('state differs from current plan or environment')
    return fresh


def derive_state(g, parent, plan, *, preparation=None, blocks_by_core=None, origin=None):
    validate_state(g, parent, preparation=preparation)
    fresh = build_state(g, plan, preparation=preparation, blocks_by_core=blocks_by_core,
                        parent_signature=parent.state_signature, origin=origin or parent.origin,
                        construction_blocks_ops=tuple(b.ops for row in parent.realized_blocks_by_core for b in row))
    # Track ancestry by operation overlap, not by coincidentally reused sgid numbers.
    old = tuple(b for row in parent.realized_blocks_by_core for b in row)
    owner = {v: i for i,b in enumerate(old) for v in b.ops}
    blocks = tuple(tuple(replace(b, origin_block_ids=tuple(old[i].block_id for i in sorted({owner[v] for v in b.ops})))
                         for b in row) for row in fresh.realized_blocks_by_core)
    return replace(fresh, realized_blocks_by_core=blocks)


def candidate_from_state(state, *, draft=None):
    return EncodedCandidate(state.plan_json, draft, state.origin.encoding, None,
                            state.parent_signature, state=state)
