"""B0、固定执行关系 B1、DDR 区间证据及不可撤销的部分分支下界。"""
from __future__ import annotations

from .state import topological
from schedule_step3 import _op_duration, _uses_ddr_bandwidth


def cannot_improve(bound, movement, threshold):
    return bound > threshold[0] or (bound == threshold[0] and movement >= threshold[1])


def basic(state):
    waits = state.waits()
    finish = {}
    for j in state.topo:
        finish[j] = state.measure[j]["d0"] + max(
            (finish[p] + waits[p, j] for p in state.combined[j]), default=0)
    return dict(time=max(max(finish.values(), default=0), sum(m["q"] for m in state.measure.values())),
                movement=sum(m["bytes"] for m in state.measure.values()) - state.context.original_bytes,
                task_finish=finish)


def _static_expansion(state, prepared):
    """Build partition-only edges with the original node/edge insertion order."""
    duration, preds, gaps, copies = {}, {}, {}, {}

    def node(key, p=0):
        duration[key] = p
        preds.setdefault(key, set())

    def edge(a, b, gap=0):
        preds[b].add(a)
        gaps[a, b] = max(gaps.get((a, b), 0), gap)

    end = (2, -1, -1)
    node(end)
    for j, task in prepared["tasks"].items():
        start, stop = (0, j, -1), (2, j, -1)
        node(start)
        node(stop)
        q = 0
        for v, op in task["op_by_id"].items():
            key = (1, j, v)
            p = _op_duration(op, task["in_tids"], task["out_tids"],
                             task["tensor_by_id"], state.context.settings.bandwidth)
            node(key, p)
            edge(start, key)
            edge(key, stop)
            if _uses_ddr_bandwidth(op, task["in_tids"], task["out_tids"], task["tensor_by_id"]):
                copies[key] = p
                q += p
        for v, ps in task["op_preds"].items():
            for p in ps:
                edge((1, j, p), (1, j, v))
        for order in task["pipe_ops"].values():
            for a, b in zip(order, order[1:]):
                edge((1, j, a), (1, j, b))
        edge(start, stop, max(q, state.measure[j]["d0"]))
        edge(stop, end)
    return dict(prepared=prepared, duration=duration, preds=preds, gaps=gaps,
                copies=copies, copy_work=sum(copies.values()), end=end)


def expanded(state, prepared, b0=None):
    b0 = b0 or basic(state)
    template = state._partition.get("b1")
    if template is None or template["prepared"] is not prepared:
        template = _static_expansion(state, prepared)
        state._partition["b1"] = template
    duration, copies, end = template["duration"], template["copies"], template["end"]
    preds = template["preds"].copy()
    gaps = template["gaps"].copy()
    for (a, b), delay in state.waits().items():
        source, target = (2, a, -1), (0, b, -1)
        if preds[target] is template["preds"][target]:
            preds[target] = preds[target].copy()
        preds[target].add(source)
        gaps[source, target] = max(gaps.get((source, target), 0), delay)
    order = topological(preds)
    release, tail = {}, dict.fromkeys(preds, 0)
    for v in order:
        release[v] = max((release[p] + duration[p] + gaps[p, v] for p in preds[v]), default=0)
    for v in reversed(order):
        for p in preds[v]:
            tail[p] = max(tail[p], gaps[p, v] + duration[v] + tail[v])
    return dict(time=max(b0["time"], release[end], template["copy_work"]),
                movement=prepared["traffic"]["added_copy_bytes"], release=release,
                tail=tail, copies=copies.copy(), duration=duration.copy())


def ddr_violation(bound, upper, endpoint_limit=12):
    windows = []
    points = {0, upper}
    for op, q in bound["copies"].items():
        r, d = bound["release"][op], upper - bound["tail"][op]
        if r + q > d:
            return dict(kind="copy_window", operation=list(op[1:]), release=r,
                        deadline=d, work=q, upper=upper)
        windows.append((r, d, q))
        points.update((r, d))
    points = sorted(t for t in points if 0 <= t <= upper)
    if len(points) > endpoint_limit:
        points = [points[i * (len(points) - 1) // (endpoint_limit - 1)] for i in range(endpoint_limit)]
    for a_index, a in enumerate(points):
        for b in points[a_index + 1:]:
            work = sum(max(0, q - max(0, a-r) - max(0, d-b)) for r, d, q in windows)
            if work > b-a:
                return dict(kind="ddr_interval", start=a, end=b, required_work=work,
                            capacity=b-a, upper=upper)
    return None


def partial(context, mapping, core_by_label, num_cores):
    """Unknown producers/labels/cores contribute no speculative boundary or wait."""
    work = {}
    group_work = {j: [0, 0] for j in mapping.values()}
    group_q = dict.fromkeys(group_work, 0)
    preds = {j: set() for j in group_work}
    for v, j in mapping.items():
        pipe_index = int(context.index.ops[v]["pipe"] == "PIPE_V")
        group_work[j][pipe_index] += max(1, context.index.cycles(v))
        preds[j].update(mapping[p] for p in context.index.preds[v] if p in mapping and mapping[p] != j)
        if j in core_by_label:
            k = core_by_label[j]
            p = context.index.ops[v]["pipe"]
            work[k, p] = work.get((k, p), 0) + max(1, context.index.cycles(v))
    q = size = 0
    for t in context.index.tensors.values():
        labels = {mapping[v] for v in t.consumers if v in mapping}
        if t.producer is None:
            copies = len(labels)
            for label in labels:
                group_q[label] += t.copy_cycles
        elif t.producer in mapping:
            external = labels - {mapping[t.producer]}
            copies = len(external) + int(bool(external) or t.final_output)
            for label in external:
                group_q[label] += t.copy_cycles
            if external or t.final_output:
                group_q[mapping[t.producer]] += t.copy_cycles
        else:
            copies = 0
        q += copies * t.copy_cycles
        size += copies * t.size
    finish = {}
    for j in topological(preds):
        ready = max((finish[p] + (context.settings.cross_core_wait
                     if p in core_by_label and j in core_by_label and core_by_label[p] != core_by_label[j] else 0)
                     for p in preds[j]), default=0)
        finish[j] = ready + max(*group_work[j], group_q[j])
    return dict(time=max((context.total_work[0]+num_cores-1)//num_cores,
                         (context.total_work[1]+num_cores-1)//num_cores,
                         context.compute_path, max(work.values(), default=0), q, max(finish.values(), default=0)),
                movement=size-context.original_bytes)
