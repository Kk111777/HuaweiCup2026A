"""规范第 5–9 节：纯增量试放、核心持续状态、四短名单联动生长。"""
from dataclasses import dataclass
import heapq
import json
from .contracts import ConstructionDraft


class BudgetExpired(TimeoutError):
    pass


class State:
    def __init__(self, graph, cores):
        if type(cores) is not int or not 1 <= cores <= 5: raise ValueError('cores must be 1..5')
        n, t = len(graph.ids), len(graph.tensors)
        self.version = 0
        self.core = [-1]*n; self.sgid = [-1]*n
        self.remaining_preds = list(map(len, graph.preds))
        self.remaining_consumers = [len(t.consumers) for t in graph.tensors]
        self.consumer_mask = [0]*t; self.holders = [0]*t
        self.ready = {v for v, d in enumerate(self.remaining_preds) if not d}
        self.tensor_ready = {}; self.compute_end = [0]*n
        self.clocks = [[0]*4 for _ in range(cores)]
        self.work = [[0, 0] for _ in range(cores)]
        self.live = [[0, 0] for _ in range(cores)]
        self.peaks = [[0, 0] for _ in range(cores)]
        self.copy_work = self.copy_bytes = self.tail = self.score = 0
        self.orders = [[] for _ in range(cores)]
        self.groups = []; self.current = set(); self.current_core = None
        self.subgraph_work = [0, 0]; self.limit = 0
        self.commit_order = []; self.close_records = []

    def plan(self, graph):
        if any(k < 0 for k in self.core): raise ValueError('partial plan cannot be published')
        return {'node_to_subgraph': {str(v): self.sgid[i] for i, v in enumerate(graph.ids)},
                'core_schedules': [q.copy() for q in self.orders]}


@dataclass(frozen=True)
class Trial:
    version: int
    op: int
    core: int
    score: int
    added_work: int
    added_bytes: int
    end: int
    tail: int
    clocks: tuple
    live: tuple
    peaks: tuple
    ready_updates: tuple
    tensor_updates: tuple
    copied_elements: int


def try_place(g, s, v, k):
    """只复制 O(N) Pipe/驻留数组及该操作触及张量的 delta；不写父状态。"""
    if v not in s.ready or s.core[v] != -1: raise ValueError('trial requires an unassigned ready op')
    clocks = [x.copy() for x in s.clocks]
    live = [x.copy() for x in s.live]; peaks = [x.copy() for x in s.peaks]
    updates = {}; added_work = added_bytes = 0
    for ti in g.inputs[v]:
        t = g.tensors[ti]
        if (ti, k) in s.tensor_ready: continue
        if t.producer is None:
            clocks[k][2] += t.q
            added_work += t.q; added_bytes += t.size
        else:
            source = s.core[t.producer]
            if source < 0 or source == k: raise AssertionError('missing producer ready time')
            clocks[source][3] = max(clocks[source][3], s.tensor_ready[ti, source])+t.q
            clocks[k][2] = max(clocks[k][2], clocks[source][3]+g.settings.delay)+t.q
            added_work += 2*t.q; added_bytes += 2*t.size
        updates[ti, k] = clocks[k][2]
    input_ready = max((updates[(ti, k)] if (ti, k) in updates else s.tensor_ready[ti, k]
                       for ti in g.inputs[v]), default=0)
    end = max(clocks[k][g.pipe[v]], input_ready)+g.duration[v]
    clocks[k][g.pipe[v]] = end
    for ti in g.outputs[v]:
        t = g.tensors[ti]; updates[ti, k] = end
        if t.final:
            clocks[k][3] = max(clocks[k][3], end)+t.q
            added_work += t.q; added_bytes += t.size
    instant = live[k].copy()
    for ti in g.touched[v]:
        t = g.tensors[ti]
        if not (s.remaining_consumers[ti] > 0 and s.holders[ti] & (1 << k)):
            instant[t.pos] += t.size
    peaks[k] = [max(a, b) for a, b in zip(peaks[k], instant)]
    tensors = []
    for ti in g.touched[v]:
        t = g.tensors[ti]; old_remaining = s.remaining_consumers[ti]
        remaining = old_remaining-int(ti in g.inputs[v])
        if remaining < 0: raise AssertionError('negative consumer count')
        mask = s.holders[ti] | (1 << k)
        consumers = s.consumer_mask[ti] | ((1 << k) if ti in g.inputs[v] else 0)
        for core in range(len(clocks)):
            before = old_remaining > 0 and bool(s.holders[ti] & (1 << core))
            after = remaining > 0 and bool(mask & (1 << core))
            live[core][t.pos] += (int(after)-int(before))*t.size
        tensors.append((ti, remaining, mask, consumers))
    tail = max(s.tail, end+g.b[v]-g.duration[v])
    capacity = (g.settings.capacity['L1'], g.settings.capacity['UB'])
    pressure = 2*sum((max(0, row[p]-capacity[p])+g.settings.bandwidth-1)//g.settings.bandwidth
                     for row in peaks for p in (0, 1))
    score = max(max(map(max, clocks)), s.copy_work+added_work, tail)+pressure
    return Trial(s.version, v, k, score, added_work, added_bytes, end, tail,
                 tuple(map(tuple, clocks)), tuple(map(tuple, live)), tuple(map(tuple, peaks)),
                 tuple(updates.items()), tuple(tensors), 8*len(clocks)+len(updates)+4*len(tensors))


def commit(g, s, trial, *, seed=False, limit=None):
    v, k = trial.op, trial.core
    if trial.version != s.version or v not in s.ready or s.core[v] != -1:
        raise ValueError('stale or duplicate trial')
    if seed:
        if s.current: raise ValueError('close current subgraph first')
        j = len(s.groups); s.groups.append([]); s.orders[k].append(j)
        s.current_core = k; s.limit = max(limit, g.duration[v])
    elif not s.current or k != s.current_core:
        raise ValueError('continuation core differs')
    j = len(s.groups)-1
    s.clocks = list(map(list, trial.clocks)); s.live = list(map(list, trial.live)); s.peaks = list(map(list, trial.peaks))
    s.tensor_ready.update(trial.ready_updates)
    for ti, remaining, holders, consumers in trial.tensor_updates:
        s.remaining_consumers[ti] = remaining; s.holders[ti] = holders; s.consumer_mask[ti] = consumers
    s.copy_work += trial.added_work; s.copy_bytes += trial.added_bytes
    s.tail, s.score = trial.tail, trial.score
    s.compute_end[v] = trial.end; s.core[v] = k; s.sgid[v] = j
    s.work[k][g.pipe[v]] += g.duration[v]; s.subgraph_work[g.pipe[v]] += g.duration[v]
    s.groups[j].append(v); s.current.add(v); s.ready.remove(v)
    s.commit_order.append(v)
    for w in g.succs[v]:
        s.remaining_preds[w] -= 1
        if s.remaining_preds[w] == 0: s.ready.add(w)
    s.version += 1


def close(s, reason='explicit_close', advantage=None):
    if s.current:
        s.close_records.append(dict(sgid=len(s.groups)-1, core=s.current_core,
                                   reason=reason, work=tuple(s.subgraph_work), limit=s.limit,
                                   work_over_limit=max(s.subgraph_work)/max(1, s.limit),
                                   core_advantage=advantage))
    s.current = set(); s.current_core = None; s.subgraph_work = [0, 0]; s.limit = 0


def shortlist(g, s, scope, count, stats):
    k = s.current_core
    stats['shortlist_rounds'] += 1
    stats['ready_scanned'] += len(s.ready)
    eligible = [v for v in s.ready if v in scope and s.subgraph_work[g.pipe[v]]+g.duration[v] <= s.limit]
    key = lambda v: (-g.b[v], g.ids[v])
    def reuse(v):
        return sum(g.tensors[t].q*(1 if g.tensors[t].producer is None else 2)
                   for t in g.inputs[v] if (t, k) in s.tensor_ready)
    lists = [heapq.nsmallest(count, eligible, key=key),
             heapq.nsmallest(count, eligible, key=lambda v: (-reuse(v), *key(v))),
             heapq.nsmallest(count, (v for v in eligible if any(p in s.current for p in g.preds[v])), key=key)]
    if s.work[k][0] != s.work[k][1]:
        lighter = int(s.work[k][1] < s.work[k][0])
        lists.append(heapq.nsmallest(count, (v for v in eligible if g.pipe[v] == lighter), key=key))
    selected = sorted(set().union(*map(set, lists)))
    stats['shortlist_total'] += len(selected)
    stats['shortlist_max'] = max(stats['shortlist_max'], len(selected))
    return selected


def construct_candidate(g, cores, mode, beta, count=8, check=lambda: None, trace=None, *, stats=None):
    s = State(g, cores)
    # 调用方持有计数器，预算中断后仍能记录已付出的构造成本。
    if stats is None: stats = {}
    stats.update(trials=0, ready_scanned=0, shortlist_rounds=0, shortlist_total=0, shortlist_max=0, copied_state_elements=0)
    limit = max(1, (max(g.work)+beta*cores-1)//(beta*cores))
    def trials(v):
        result = [try_place(g, s, v, k) for k in range(cores)]
        stats['trials'] += cores
        stats['copied_state_elements'] += sum(t.copied_elements for t in result)
        return result
    for scope in g.scopes(cores, mode):
        remaining = len(scope)
        while remaining:
            check()
            if not s.current:
                stats['ready_scanned'] += len(s.ready)
                v = min((v for v in s.ready if v in scope), key=lambda v: (-g.b[v], g.a[v], g.ids[v]))
                best = min(trials(v), key=lambda t: (t.score, t.added_bytes,
                           max(s.work[t.core][p]+(g.duration[v] if p == g.pipe[v] else 0) for p in (0, 1)), t.core))
                commit(g, s, best, seed=True, limit=limit)
            else:
                candidates = shortlist(g, s, scope, count, stats)
                if not candidates: close(s, 'no_eligible_candidate'); continue
                ranked = []
                for v in candidates:
                    check(); ts = trials(v); current = ts[s.current_core]
                    advantage = min((t.score for t in ts if t.core != s.current_core), default=current.score)-current.score
                    ranked.append(((-advantage, -g.b[v], current.added_bytes, g.ids[v]), current))
                rank, best = min(ranked, key=lambda x: x[0])
                if rank[0] > 0: close(s, 'negative_core_advantage', -rank[0]); continue
                commit(g, s, best)
            remaining -= 1
            if trace is not None:
                trace({'version': s.version, 'op_id': g.ids[best.op], 'core': best.core,
                       'sgid': s.sgid[best.op], 'proxy_score': best.score})
        close(s, 'scope_exhausted')
    expected = g.base_traffic(s.core)
    if expected != (s.copy_work, s.copy_bytes): raise AssertionError('incremental/formula traffic mismatch')
    info = dict(mode=mode, beta=beta, proxy_J=s.score, base_copy_work=s.copy_work,
                base_copy_bytes=s.copy_bytes, close_records=s.close_records,
                proxy_clocks=s.clocks, proxy_peaks=s.peaks,
                proxy_semantics='historical_max; append_source_MTE3; global_remaining_consumers', **stats)
    return ConstructionDraft(tuple(s.core), tuple(s.commit_order), tuple(map(tuple, s.groups)),
                             json.dumps(s.plan(g)), 'linked', mode, beta, json.dumps(info))


def construct(g, cores, mode, beta, count=8, check=lambda: None, trace=None, *, stats=None):
    """Compatibility wrapper; exactly the same construction kernel as Q2 2.0."""
    draft = construct_candidate(g, cores, mode, beta, count, check, trace, stats=stats)
    return draft.legacy_plan, draft.diagnostics
