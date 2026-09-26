"""Bounded complete-plan proposals based on actual reads and current Q2 state."""
from collections import defaultdict
import copy

from q2.capacity import CapacityWindow, minimum_disruption, apply_window
from q2.memory import profile


def diagnose(result, prepared):
    accesses = defaultdict(list)
    priority = defaultdict(float)
    for event in result.get('cache_events', []):
        if event['event'] in ('hit', 'miss'):
            accesses[event['tensor_id']].append(event)
    rows = []
    for key, events in accesses.items():
        misses = [e for e in events if e['event'] == 'miss']
        rows.append(dict(logical_tid=key, reads=len(events), misses=len(misses),
                         repeated_miss_bytes=sum(e['size_bytes'] for e in misses[1:])))
        if len(misses) > 1:
            for event in misses:
                core, op = event['core_id'], event['op_id']
                sg = prepared.bundle[0][core]['op_subgraph'][op]
                priority[core, sg] += event['size_bytes'] * (len(misses) - 1) / len(misses)
    finishes = {c['core_id']: max((o['end'] for o in c['ops']), default=0)
                for c in result['per_core_timeline']}
    times = {(c['core_id'], o['op_id']): o for c in result['per_core_timeline'] for o in c['ops']}
    parents = {v: [] for v in times}
    for core, task in prepared.bundle[0].items():
        for v, ps in task['op_preds'].items():
            parents[core, v].extend(((core, u), 0) for u in ps)
        for queue in task['pipe_ops'].values():
            for u, v in zip(queue, queue[1:]):
                parents[core, v].append(((core, u), 0))
    for link in prepared.bundle[1]:
        a = link['source_core'], link['source_copy_out_id']
        b = link['target_core'], link['target_copy_in_id']
        parents[b].append((a, prepared.cross_delay))
    frontier = [v for v, o in times.items() if o['end'] == result['makespan']]
    critical, marked = [], set()
    while frontier:
        v = frontier.pop()
        if v in marked:
            continue
        marked.add(v)
        core, op = v
        critical.append((core, prepared.bundle[0][core]['op_subgraph'][op]))
        frontier.extend(u for u, lag in parents[v] if times[u]['end'] + lag == times[v]['start'])
    return dict(reads=sorted(rows, key=lambda r: (-r['repeated_miss_bytes'], r['logical_tid'])),
                targets=[list(k) for k, _ in sorted(priority.items(), key=lambda x: (-x[1], x[0]))],
                critical_targets=[list(k) for k in dict.fromkeys(critical)],
                core_finishes=finishes, spill_bytes=result['data_movement_bytes']['spill_added_copy_bytes'])


def capacity_candidates(evaluator, plan, prepared, options):
    if not prepared.bundle[3]['spill_added_copy_bytes']:
        return
    count = 0
    state, prefix = prepared.scene_state, prepared.prefix
    for core, local in prefix['cores'].items():
        pressure = profile(local['graph'], local['seq'], evaluator.settings.scene.capacity)
        if pressure.no_spill:
            continue
        blocks = tuple(b.sgids for b in state.realized_blocks_by_core[core])
        if len(blocks) < 2:
            continue
        owner = {sg: i for i, block in enumerate(blocks) for sg in block}
        centers = dict.fromkeys(owner[local['op_subgraph'][op]] for i, op in enumerate(local['seq'])
                                if pressure.curve[0][i] > evaluator.settings.scene.capacity['L1']
                                or pressure.curve[1][i] > evaluator.settings.scene.capacity['UB'])
        seen = set()
        for center in centers:
            width = min(options['window_width'], len(blocks))
            left = min(max(0, center - width // 2), len(blocks) - width)
            if left in seen:
                continue
            if count >= options['max_windows']:
                return
            seen.add(left)
            count += 1
            try:
                window = CapacityWindow(prefix, core, blocks[left:left + width], evaluator.settings.scene.capacity)
            except ValueError:
                continue  # outside this fixed-window domain, not globally infeasible
            answer = minimum_disruption(window)
            if answer['status'] == 'OPTIMAL_IN_FIXED_WINDOW' and answer['inversions']:
                candidate, _ = apply_window(evaluator.graph, plan, prefix, window, answer,
                                            lambda graph, p: evaluator.service.prefix(p))
                yield 'capacity', candidate


def _blocks(prepared):
    return [(core, tuple(b.sgids)) for core, row in enumerate(prepared.scene_state.realized_blocks_by_core)
            for b in row]


def _move(plan, core, block, target, slot):
    new = copy.deepcopy(plan)
    members = set(block)
    new['core_schedules'][core] = [s for s in new['core_schedules'][core] if s not in members]
    new['core_schedules'][target][slot:slot] = block
    return new


def candidates(evaluator, plan, result, prepared, stage, options):
    queues, view = plan['core_schedules'], prepared.bundle[4]
    diagnostic = diagnose(result, prepared)
    targets = [tuple(x) for x in diagnostic['targets']]
    critical = [tuple(x) for x in diagnostic['critical_targets']]
    finishes = diagnostic['core_finishes']
    blocks = _blocks(prepared)
    heavy = sorted(range(len(queues)), key=lambda k: (-finishes[k], k))
    late = [(k, sg) for k in heavy for sg in queues[k]]
    seeds = options['diagnostic_seeds']
    if stage == 'cheap':
        yield from capacity_candidates(evaluator, plan, prepared, options)
        selected = [x for x in dict.fromkeys(targets + critical + late) if x[1] in queues[x[0]]][:seeds]
        for core, sg in selected:
            i = queues[core].index(sg)
            for j in sorted({max(0, min(len(queues[core]) - 1, i + d)) for d in (-4, -2, -1, 1, 2, 4)}):
                if i == j:
                    continue
                new = copy.deepcopy(plan)
                new['core_schedules'][core].pop(i)
                new['core_schedules'][core].insert(j, sg)
                yield ('l2_order' if (core, sg) in targets else 'late_order'), new
        return
    loads = {k: sum(max(1, op['cycles']) for op in evaluator.raw['ops']
                    if op['id'] in view['mapping'] and view['core_by_subgraph'][view['mapping'][op['id']]] == k)
             for k in range(len(queues))}
    local_keys = {k: set() for k in range(len(queues))}
    for event in result.get('cache_events', []):
        if event['event'] in ('hit', 'miss'):
            local_keys[event['core_id']].add(event['tensor_id'])
    important = set(targets + critical)
    block_priority = sorted(blocks, key=lambda x: (not any((x[0], sg) in important for sg in x[1]),
                            -finishes[x[0]], x[0], queues[x[0]].index(x[1][0])))[:seeds]

    def destinations(core):
        choices = [k for k in range(len(queues)) if k != core]
        by_load = sorted(choices, key=lambda k: (loads[k], k))
        by_shared = sorted(choices, key=lambda k: (-len(local_keys[k] & local_keys[core]), loads[k], k))
        order = []
        for a, b in zip(by_load, by_shared):
            order.extend((a, b))
        return list(dict.fromkeys(order))[:options['migration_targets']]

    if stage == 'joint':
        emitted = 0
        for core, block in block_priority:
            source = [s for s in queues[core] if s not in block]
            index = queues[core].index(block[0])
            left = max(0, min(index, len(source)) - 2)
            segment = source[left:left + options['joint_blocks']]
            if len(segment) < 2:
                continue
            variants = [segment[-1:] + segment[:-1], segment[1:] + segment[:1], list(reversed(segment))]
            for target in destinations(core):
                for variant in variants:
                    for slot in sorted({0, len(queues[target])}):
                        new = _move(plan, core, block, target, slot)
                        new['core_schedules'][core] = source[:left] + variant + source[left + len(segment):]
                        yield 'joint_migrate_order', new
                        emitted += 1
                        if emitted >= options['joint_candidates']:
                            return
        return
    if stage != 'placement':
        raise ValueError('unknown candidate stage')
    from q2.affinity import component_candidates
    for encoded in component_candidates(evaluator.graph, len(queues)):
        yield 'whole_component', encoded.plan
    for core, block in block_priority:
        for target in destinations(core):
            for slot in sorted({0, len(queues[target]) // 2, len(queues[target])}):
                yield 'migrate', _move(plan, core, block, target, slot)
    for i, (a, ablock) in enumerate(block_priority):
        for b, bblock in block_priority[i + 1:]:
            if a == b:
                continue
            new = copy.deepcopy(plan)
            ai, bi = queues[a].index(ablock[0]), queues[b].index(bblock[0])
            new['core_schedules'][a][ai:ai + len(ablock)] = bblock
            new['core_schedules'][b][bi:bi + len(bblock)] = ablock
            yield 'swap', new
    mapping = view['mapping']
    fresh = max(view['subgraph_ids'], default=-1) + 1
    for core in heavy:
        order = [v for v in prepared.bundle[0][core]['seq'] if v in mapping]
        groups = defaultdict(list)
        for v in order:
            groups[mapping[v]].append(v)
        for sg, members in sorted(groups.items(), key=lambda x: (-len(x[1]), x[0]))[:seeds]:
            if len(members) < 2:
                continue
            for cut in sorted({max(1, len(members) // 4), len(members) // 2, 3 * len(members) // 4}):
                for target in destinations(core) or [core]:
                    new = copy.deepcopy(plan)
                    for v in members[cut:]:
                        new['node_to_subgraph'][str(v)] = fresh
                    slot = queues[core].index(sg) + 1 if target == core else len(queues[target])
                    new['core_schedules'][target].insert(slot, fresh)
                    yield 'split_migrate', new
