"""标准二部输入的稠密只读索引；保留原 ID 和数据字节，不按最大 ID 分配。"""
from collections import defaultdict
from dataclasses import dataclass
import heapq
from .official import Q2Error


@dataclass(frozen=True)
class Tensor:
    id: int
    size: int
    pos: int
    producer: int | None
    consumers: tuple
    final: bool
    q: int


class Graph:
    def __init__(self, raw, settings):
        from evaluation_validation import validate_graph
        from multicore_cut_evaluate_problem_1 import _copy_traffic_bytes
        try:
            validate_graph(raw)
        except ValueError as error:
            raise Q2Error('INPUT_INVALID', str(error)) from error
        self.raw, self.settings = raw, settings
        all_ops = {o['id']: o for o in raw['ops']}
        tensors = {t['id']: t for t in raw['tensors']}
        ids = sorted(i for i, o in all_ops.items() if o['op'] not in ('COPY_IN', 'COPY_OUT'))
        self.ids = tuple(ids)
        self.dense = {v: i for i, v in enumerate(ids)}
        incoming, outgoing = defaultdict(set), defaultdict(set)
        producers, consumers = defaultdict(set), defaultdict(set)
        for e in raw['edges']:
            a, b = e['source'], e['target']
            if a in all_ops and b in tensors:
                outgoing[a].add(b); producers[b].add(a)
            elif a in tensors and b in all_ops:
                incoming[b].add(a); consumers[a].add(b)
            else:
                raise Q2Error('UNSUPPORTED_INPUT_MODEL', '快速模型仅支持原始二部边')
        for i, o in all_ops.items():
            if o['op'] in ('COPY_IN', 'COPY_OUT'):
                if len(incoming[i]) != 1 or len(outgoing[i]) != 1:
                    raise Q2Error('UNSUPPORTED_INPUT_MODEL', f'非标准 COPY 形状: {i}')
                a, b = tensors[next(iter(incoming[i]))], tensors[next(iter(outgoing[i]))]
                ok = ((a['pos'] == 'DDR' and b['pos'] in ('L1', 'UB')) if o['op'] == 'COPY_IN'
                      else (a['pos'] in ('L1', 'UB') and b['pos'] == 'DDR'))
                if not ok or a['size'] != b['size']:
                    raise Q2Error('UNSUPPORTED_INPUT_MODEL', f'非标准 COPY 端点: {i}')
                if (o['op'] == 'COPY_IN' and producers[a['id']]) or (o['op'] == 'COPY_OUT' and consumers[b['id']]):
                    raise Q2Error('UNSUPPORTED_INPUT_MODEL', f'中间 DDR 通路: {i}')
            elif o['pipe'] not in ('PIPE_M', 'PIPE_V'):
                raise Q2Error('UNSUPPORTED_INPUT_MODEL', f'非 M/V 计算操作: {i}')
        local = []
        for tid, t in sorted(tensors.items()):
            ps = producers[tid] & self.dense.keys()
            cs = consumers[tid] & self.dense.keys()
            if not ps and not cs:
                continue
            if len(ps) > 1 or t['pos'] not in ('L1', 'UB'):
                raise Q2Error('UNSUPPORTED_INPUT_MODEL', f'非标准逻辑张量: {tid}')
            # 禁止 COPY 重建会删除的中间 DDR 回路及多份原始输入 COPY。
            if (ps and any(all_ops[v]['op'] == 'COPY_IN' for v in producers[tid])) or len(producers[tid]) > 1:
                raise Q2Error('UNSUPPORTED_INPUT_MODEL', f'混合或多生产者: {tid}')
            if not ps and cs and (len(producers[tid]) != 1 or
                                  all_ops[next(iter(producers[tid]))]['op'] != 'COPY_IN'):
                raise Q2Error('UNSUPPORTED_INPUT_MODEL', f'输入缺少标准 COPY_IN: {tid}')
            p = self.dense[next(iter(ps))] if ps else None
            local.append(Tensor(tid, t['size'], int(t['pos'] == 'UB'), p,
                                tuple(sorted(self.dense[v] for v in cs)),
                                bool(ps) and (not cs or any(all_ops[v]['op'] == 'COPY_OUT' for v in consumers[tid])),
                                max(1, (t['size']+settings.bandwidth-1)//settings.bandwidth)))
        self.tensors = tuple(local)
        tidx = {t.id: i for i, t in enumerate(local)}
        self.inputs = tuple(tuple(tidx[t] for t in sorted(incoming[v])) for v in ids)
        self.outputs = tuple(tuple(tidx[t] for t in sorted(outgoing[v])) for v in ids)
        self.touched = tuple(tuple(sorted(set(a) | set(b))) for a, b in zip(self.inputs, self.outputs))
        self.pipe = tuple(int(all_ops[v]['pipe'] == 'PIPE_V') for v in ids)
        self.duration = tuple(max(1, all_ops[v]['cycles']) for v in ids)
        pred = [set() for _ in ids]; succ = [set() for _ in ids]
        for t in local:
            if t.producer is not None:
                for v in t.consumers:
                    pred[v].add(t.producer); succ[t.producer].add(v)
        self.preds, self.succs = tuple(map(tuple, map(sorted, pred))), tuple(map(tuple, map(sorted, succ)))
        degree = list(map(len, pred)); ready = [v for v, d in enumerate(degree) if not d]
        heapq.heapify(ready); order = []
        while ready:
            v = heapq.heappop(ready); order.append(v)
            for w in self.succs[v]:
                degree[w] -= 1
                if not degree[w]: heapq.heappush(ready, w)
        if len(order) != len(ids): raise Q2Error('INPUT_INVALID', '计算依赖有环')
        self.order = tuple(order)
        a = [0]*len(ids); b = a.copy()
        for v in order: a[v] = self.duration[v]+max((a[p] for p in pred[v]), default=0)
        for v in reversed(order): b[v] = self.duration[v]+max((b[w] for w in succ[v]), default=0)
        self.a, self.b = tuple(a), tuple(b)
        self.work = tuple(sum(d for d, p in zip(self.duration, self.pipe) if p == k) for k in (0, 1))
        left = set(range(len(ids))); components = []
        for v in range(len(ids)):
            if v not in left: continue
            left.remove(v); queue = [v]
            for x in queue:
                for y in sorted((pred[x] | succ[x]) & left):
                    left.remove(y); queue.append(y)
            components.append(tuple(sorted(queue)))
        self.components = tuple(components)
        self.original_copy_bytes = _copy_traffic_bytes(raw)

    def scopes(self, cores, mode):
        if mode == 'global': return (frozenset(range(len(self.ids))),)
        if mode != 'component_grouped': raise ValueError(mode)
        groups = [set() for _ in range(min(cores, len(self.components)))]
        loads = [[0, 0] for _ in groups]
        weighted = [(c, [sum(self.duration[v] for v in c if self.pipe[v] == p) for p in (0, 1)])
                    for c in self.components]
        for c, w in sorted(weighted, key=lambda cw: (-max(cw[1]), self.ids[cw[0][0]])):
            k = min(range(len(groups)), key=lambda k: (max(loads[k][p]+w[p] for p in (0, 1)), sum(loads[k])+sum(w), k))
            groups[k].update(c)
            for p in (0, 1): loads[k][p] += w[p]
        return tuple(map(frozenset, groups))

    def base_traffic(self, core):
        work = size = 0
        for t in self.tensors:
            targets = {core[v] for v in t.consumers}
            copies = len(targets) if t.producer is None else int(t.final)+2*len(targets-{core[t.producer]})
            work += copies*t.q; size += copies*t.size
        return work, size
