"""完整编排动作与冻结的局部域；域外 Task 的实际时间从不冻结。"""
from __future__ import annotations

from itertools import combinations

from .state import CycleError, closure, digest, topological


def insertions(state, task):
    orders = [[j for j in order if j != task] for order in state.orders]
    preds = state.with_orders(orders)
    succs = {j: set() for j in preds}
    for j, ps in preds.items():
        for p in ps:
            succs[p].add(j)
    ancestors = closure({task}, preds)-{task}
    descendants = closure({task}, succs)-{task}
    for k, order in enumerate(orders):
        low = max((p+1 for p, j in enumerate(order) if j in ancestors), default=0)
        high = min((p+1 for p, j in enumerate(order) if j in descendants), default=len(order)+1)
        for b in range(low, high):
            updated = [list(x) for x in orders]
            updated[k].insert(b, task)
            if updated != state.orders:
                yield k, b, updated


def layer1(state, diagnosis, result):
    timings = {t["task_id"]: t for c in result["per_core_timeline"] for t in c["tasks"]}
    local = result["step3_by_task"]
    entries = []
    seen = set()
    for seed in diagnosis["seeds"]:
        for j in seed["tasks"]:
            if j in seen:
                continue
            seen.add(j)
            duration = local.get(j, local.get(str(j)))["local_makespan"]
            for k, b, orders in insertions(state, j):
                prior = orders[k][b-1] if b else None
                ready = max([0 if prior is None else timings[prior]["end"]+state.context.settings.same_core_wait,
                             *[timings[p]["end"]+state.context.settings.cross_core_wait
                               for p in state.preds[j] if state.core[p] != k]])
                entries.append(((ready+duration, j, k, b), seed["id"], orders, "insert"))
    pairs = sorted({tuple(pair) for s in diagnosis["seeds"] for pair in combinations(s["tasks"], 2)})
    for a, b in pairs:
        orders = [[b if j == a else a if j == b else j for j in order] for order in state.orders]
        # Check the simultaneous swap, never a fictitious intermediate move.
        try:
            topological(state.with_orders(orders))
        except CycleError:
            continue
        entries.append(((max(timings[a]["end"], timings[b]["end"]), a, b, -1), None, orders, "swap"))
    signatures = set()
    for priority, seed, orders, action in sorted(entries, key=lambda x: (x[0], x[3])):
        key = tuple(map(tuple, orders))
        if key in signatures:
            continue
        signatures.add(key)
        yield {"node_to_subgraph": state.plan["node_to_subgraph"], "core_schedules": orders}, {
            "action": action, "seed_id": seed, "priority": priority}


def cuts_for_seed(state, seed, prepared):
    cuts = {int(j): [set(p) for p in items] for j, items in seed["cuts"].items()}
    if seed["kind"] in ("load", "overlap_gap"):
        for j in seed["tasks"]:
            nodes = state.nodes[j]
            if len(nodes) < 2:
                continue
            order = [v for v in prepared["diagnostics"][j]["step1"] if v in nodes]
            dominant = max(range(2), key=lambda p: (state.measure[j]["work"][p], -p))
            pipe = ("PIPE_M", "PIPE_V")[dominant]
            target = (state.measure[j]["work"][dominant]+1)//2
            count = 0
            prefix = set()
            for v in order[:-1]:
                prefix.add(v)
                if state.context.index.ops[v]["pipe"] == pipe:
                    count += max(1, state.context.index.cycles(v))
                if count >= target:
                    break
            prefix = closure(prefix, state.context.index.preds, nodes)
            if count >= target and prefix and prefix != nodes:
                cuts.setdefault(j, []).append(prefix)
    return {j: [p for p in ps if p and p < state.nodes[j]] for j, ps in cuts.items()}


def region_window(state, seed_tasks, diagnosis, result, options):
    selected = set(seed_tasks)
    windows = {}
    for k, order in enumerate(state.orders):
        positions = [i for i, j in enumerate(order) if j in selected]
        if positions:
            a, b = min(positions), max(positions)+1
            selected.update(order[a:b])
            windows[k] = (a, b)
    if len(selected) > options.max_region_tasks:
        return None
    available = [k for k in range(len(state.orders)) if k not in windows]
    if available:
        k = min(available, key=lambda k: (max(diagnosis["core_work"][k]), diagnosis["core_finish"][k], k))
        timing = {t["task_id"]: t for c in result["per_core_timeline"] for t in c["tasks"]}
        time = min(timing[j]["start"] for j in selected)
        gap = sum(timing[j]["end"] <= time for j in state.orders[k])
        windows[k] = (gap, gap)
    return selected, windows


def domain_record(state, seed, region, groups, mode):
    selected, windows = region
    groups = [sorted(p) for p in groups]
    if any(not p for p in groups) or sum(map(len, groups)) != len(set().union(*map(set, groups))):
        raise RuntimeError("local domain has empty or overlapping fragments")
    if set().union(*map(set, groups)) != set().union(*(state.nodes[j] for j in selected)):
        raise RuntimeError("local domain does not cover its region")
    groups.sort(key=lambda p: p[0])
    record = dict(base_version=state.signature, old_task_ids=sorted(selected), fragments=groups,
                  partition_mode=mode, allowed_cores=sorted(windows),
                  windows={str(k): list(pair) for k, pair in sorted(windows.items())},
                  window_anchors={str(k): [state.orders[k][a-1] if a else None,
                                          state.orders[k][b] if b < len(state.orders[k]) else None]
                                  for k, (a, b) in sorted(windows.items())})
    record["id"] = digest(record)
    record.update(seed_ids=[seed["id"]], principle=seed["principle"])
    return record


def domains(state, diagnosis, result, prepared, options, layer, deferred):
    seen = set()
    for seed in diagnosis["seeds"]:
        selected = list(seed["tasks"])
        if layer == 3 and len(selected) == 2:
            # A blocked two-endpoint merge may become legal if its intermediate
            # Task is also allowed to change. Include one deterministic shortest path.
            for a, b in (selected, selected[::-1]):
                queue = [[a]]
                for path in queue:
                    if len(path) > options.max_region_tasks:
                        break
                    if path[-1] == b:
                        if len(path) > 2:
                            selected = sorted(set(selected) | set(path))
                            break
                        continue
                    for nxt in sorted(state.succs[path[-1]]):
                        if nxt not in path:
                            queue.append([*path, nxt])
        region = region_window(state, selected, diagnosis, result, options)
        if region is None:
            deferred.append(dict(seed_id=seed["id"], layer=layer, reason="region_task_limit"))
            continue
        selected, windows = region
        base = {j: set(state.nodes[j]) for j in selected}
        cuts = cuts_for_seed(state, seed, prepared)
        choices = []
        if layer == 2:
            for a, b in combinations(seed["tasks"], 2):
                choices.append(([base[j] for j in sorted(base) if j not in (a, b)] + [base[a] | base[b]], "merge"))
            for j, pieces in cuts.items():
                for piece in pieces:
                    choices.append(([base[k] for k in sorted(base) if k != j]+[piece, base[j]-piece], "split"))
                    for other in seed["tasks"]:
                        if other == j:
                            continue
                        for moved in (piece, base[j]-piece):
                            choices.append(([base[k] for k in sorted(base) if k not in (j, other)]
                                            + [base[j]-moved, base[other] | moved], "transfer"))
        else:
            fragments = [base[j] for j in sorted(base)]
            for j in sorted(cuts):
                for cut in cuts[j]:
                    refined = []
                    for piece in fragments:
                        refined.extend(part for part in (piece & cut, piece-cut) if part)
                    if len(refined) <= options.max_fragments:
                        fragments = refined
                    else:
                        deferred.append(dict(seed_id=seed["id"], layer=layer, reason="fragment_cut_limit"))
            choices.append((fragments, "repartition"))
        for groups, action in choices:
            if len(groups) > options.max_fragments:
                deferred.append(dict(seed_id=seed["id"], layer=layer, reason="fragment_limit"))
                continue
            domain = domain_record(state, seed, region, groups, "repartition" if layer == 3 else "fixed")
            if domain["id"] in seen:
                continue
            seen.add(domain["id"])
            domain["action"] = action
            yield domain


def canonical_groups(state, domain, groups):
    """Keep unchanged IDs, restore identical old sets, assign other groups by min op ID."""
    old = set(domain["old_task_ids"])
    outside = {j: set(ns) for j, ns in state.nodes.items() if j not in old}
    reused = {frozenset(state.nodes[j]): j for j in old}
    new = {}
    waiting = []
    used = set(outside)
    for group in sorted(groups, key=min):
        if frozenset(group) in reused:
            j = reused[frozenset(group)]
            new[j] = set(group)
            used.add(j)
        else:
            waiting.append(group)
    candidate = 0
    for group in waiting:
        while candidate in used:
            candidate += 1
        new[candidate] = set(group)
        used.add(candidate)
    return outside, new


def build_plan(state, domain, new_groups, assignments, permutations):
    outside, new = canonical_groups(state, domain, new_groups)
    mapping = {str(v): j for j, nodes in {**outside, **new}.items() for v in nodes}
    orders = [list(order) for order in state.orders]
    for raw, (a, b) in domain["windows"].items():
        k = int(raw)
        orders[k][a:b] = list(permutations[k])
    return dict(node_to_subgraph=mapping, core_schedules=orders)
