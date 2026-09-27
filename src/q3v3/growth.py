"""Q3 native ready-node growth with transactional four-Pipe trial placement.

Inspired by Q2's linked construction, with Q3-owned graph/config interfaces.
L2 hits are not assumed: the proxy pessimistically charges DDR, while actual
FIFO hits, spill, shared bandwidth and feasibility are decided officially.
"""
from collections import Counter
from dataclasses import dataclass
import heapq

from .regions import encode_order


@dataclass(frozen=True)
class Tensor:
    size: int
    pos: str
    producer: int | None
    consumers: frozenset
    final: bool
    copy_cycles: int


class Model:
    def __init__(self, graph, settings):
        self.graph, self.settings = graph, settings
        self.tensors = {}
        for tid, t in graph.tensors.items():
            ps = graph.producers[tid] & graph.ops.keys()
            cs = graph.consumers[tid] & graph.ops.keys()
            if not ps and not cs:
                continue
            if len(ps) > 1 or t['pos'] not in ('L1', 'UB'):
                raise ValueError('four-Pipe growth requires a single producer and local logical tensors')
            self.tensors[tid] = Tensor(t['size'], t['pos'], next(iter(ps), None), frozenset(cs),
                bool(ps) and (not cs or any(graph.all_ops[v]['op'] == 'COPY_OUT'
                                            for v in graph.consumers[tid])),
                max(1, (t['size'] + settings['bandwidth'] - 1) // settings['bandwidth']))
        if any(o['pipe'] not in ('PIPE_M', 'PIPE_V') for o in graph.ops.values()):
            raise ValueError('four-Pipe growth requires M/V compute operations')

    def base_traffic(self, core):
        return sum(t.size * (len({core[v] for v in t.consumers}) if t.producer is None else
                   int(t.final) + 2 * len({core[v] for v in t.consumers} - {core[t.producer]}))
                   for t in self.tensors.values())


class GrowthState:
    def __init__(self, model, cores):
        self.version = 0
        self.core, self.ends, self.tensor_ready = {}, {}, {}
        self.clocks = [{p: 0 for p in ('PIPE_M', 'PIPE_V', 'PIPE_MTE2', 'PIPE_MTE3')}
                       for _ in range(cores)]
        self.work = [Counter() for _ in range(cores)]
        self.live = [Counter() for _ in range(cores)]
        self.remaining = {tid: len(t.consumers) for tid, t in model.tensors.items()}
        self.holders = {tid: 0 for tid in model.tensors}
        self.copy_work = self.copy_bytes = 0


@dataclass(frozen=True)
class Trial:
    version: int
    op: int
    core: int
    score: float
    finish: float
    added_bytes: int
    added_work: int
    clocks: tuple
    ready: tuple
    memory: tuple
    pressure: float


def try_place(model, state, v, k):
    """No parent mutation: remote source MTE3 updates also live in the delta."""
    g = model.graph
    if v in state.core or any(u not in state.core for u in g.pred[v]):
        raise ValueError('trial requires an unassigned ready operation')
    clocks = [dict(row) for row in state.clocks]
    updates, added_work, added_bytes = {}, 0, 0
    for tid in sorted(g.inputs[v]):
        t = model.tensors[tid]
        if (tid, k) in state.tensor_ready:
            continue
        if t.producer is None:
            clocks[k]['PIPE_MTE2'] += t.copy_cycles
            copies = 1
        else:
            source = state.core[t.producer]
            out = max(clocks[source]['PIPE_MTE3'], state.tensor_ready[tid, source]) + t.copy_cycles
            clocks[source]['PIPE_MTE3'] = out
            clocks[k]['PIPE_MTE2'] = max(clocks[k]['PIPE_MTE2'],
                out + model.settings['cross_core_copy_delay_cycles']) + t.copy_cycles
            copies = 2
        updates[tid, k] = clocks[k]['PIPE_MTE2']
        added_work += copies * t.copy_cycles
        added_bytes += copies * t.size
    release = max([state.ends[u] for u in g.pred[v]] +
                  [updates.get((t, k), state.tensor_ready.get((t, k), 0)) for t in g.inputs[v]] + [0])
    pipe = g.ops[v]['pipe']
    finish = max(clocks[k][pipe], release) + g.cost[v]
    clocks[k][pipe] = finish
    for tid in sorted(g.outputs[v]):
        t = model.tensors[tid]
        updates[tid, k] = finish
        if t.final:
            clocks[k]['PIPE_MTE3'] = max(clocks[k]['PIPE_MTE3'], finish) + t.copy_cycles
            added_work += t.copy_cycles
            added_bytes += t.size
    instant = dict(state.live[k])
    memory = []
    for tid in sorted(g.inputs[v] | g.outputs[v]):
        t = model.tensors[tid]
        if not (state.remaining[tid] and state.holders[tid] & (1 << k)):
            instant[t.pos] = instant.get(t.pos, 0) + t.size
        memory.append((tid, state.remaining[tid] - int(tid in g.inputs[v]),
                       state.holders[tid] | (1 << k)))
    # Current pressure, not an irreversible historical maximum that can mask
    # all subsequent core choices. Unknown future local consumers remain a
    # conservative approximation, explicitly not a capacity certificate.
    pressure = 2 * sum(max(0, instant.get(p, 0) - model.settings['capacity'][p])
                       for p in ('L1', 'UB')) / model.settings['bandwidth']
    score = max(max(max(row.values()) for row in clocks), state.copy_work + added_work)
    score += .15 * (finish + g.backward[v] - g.cost[v]) + pressure
    return Trial(state.version, v, k, score, finish, added_bytes, added_work,
                 tuple(tuple(row.items()) for row in clocks), tuple(updates.items()),
                 tuple(memory), pressure)


def commit(model, state, trial):
    if trial.version != state.version or trial.op in state.core:
        raise ValueError('stale growth transaction')
    state.clocks = [dict(row) for row in trial.clocks]
    state.tensor_ready.update(trial.ready)
    for tid, remaining, holders in trial.memory:
        t = model.tensors[tid]
        for k in range(len(state.clocks)):
            before = state.remaining[tid] > 0 and bool(state.holders[tid] & (1 << k))
            after = remaining > 0 and bool(holders & (1 << k))
            state.live[k][t.pos] += (int(after) - int(before)) * t.size
        state.remaining[tid], state.holders[tid] = remaining, holders
    state.core[trial.op], state.ends[trial.op] = trial.core, trial.finish
    state.work[trial.core][model.graph.ops[trial.op]['pipe']] += model.graph.cost[trial.op]
    state.copy_work += trial.added_work
    state.copy_bytes += trial.added_bytes
    state.version += 1


def grow(graph, cores, settings, beta=2, check=lambda: None):
    model, state = Model(graph, settings), None
    state = GrowthState(model, cores)
    work = graph.workload(graph.ops)
    limit = max(1, max(work.values(), default=0) / (beta * cores))
    degree = {v: len(p) for v, p in graph.pred.items()}
    ready = [(-graph.backward[v], v) for v, d in degree.items() if not d]
    heapq.heapify(ready)
    order, groups, current_core, current_work, group_of = [], [], None, Counter(), {}
    while ready:
        if len(order) % 32 == 0:
            check()
        sample = [heapq.heappop(ready) for _ in range(min(4, len(ready)))]
        choices = []
        for _, v in sample:
            trials = [try_place(model, state, v, k) for k in range(cores)]
            best = min(trials, key=lambda t: (t.score, t.added_bytes,
                        max((state.work[t.core] + Counter({graph.ops[v]['pipe']: graph.cost[v]})).values()), t.core))
            fits = current_core is not None and current_work[graph.ops[v]['pipe']] + graph.cost[v] <= limit
            continuation = fits and trials[current_core].score <= best.score + .02 * max(1, limit)
            trial = trials[current_core] if continuation else best
            # Prefer direct continuation when its trial is competitive, but
            # retain critical-tail and memory signals for all four ready nodes.
            connected = continuation and any(group_of.get(u) == len(groups)-1 for u in graph.pred[v])
            choices.append(((not connected, not continuation, -graph.backward[v], trial.score, v), trial))
        _, selected = min(choices, key=lambda row: row[0])
        for row in sample:
            if row[1] != selected.op:
                heapq.heappush(ready, row)
        pipe, v = graph.ops[selected.op]['pipe'], selected.op
        if selected.core != current_core or current_work[pipe] + graph.cost[v] > limit:
            groups.append([])
            current_core, current_work = selected.core, Counter()
        groups[-1].append(v)
        group_of[v] = len(groups)-1
        current_work[pipe] += graph.cost[v]
        commit(model, state, selected)
        order.append(v)
        for w in sorted(graph.succ[v]):
            degree[w] -= 1
            if not degree[w]:
                heapq.heappush(ready, (-graph.backward[w], w))
    if state.copy_bytes != model.base_traffic(state.core):
        raise AssertionError('growth incremental/base formula traffic mismatch')
    return state.core, order, group_of, dict(beta=beta, regions=len(groups),
        predicted_base_copy_bytes=state.copy_bytes, proxy_model='four_pipe_no_shared_bandwidth_no_L2_no_spill')
