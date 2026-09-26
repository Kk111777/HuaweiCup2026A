"""Shared-DDR event model, ported from the reviewed design prototype.

Matching finite official tests is not a proof of global equivalence. This module
produces model estimates; it never labels its values as official measurements.
"""
from collections import defaultdict
from dataclasses import dataclass
import heapq
import math


class ModelUnavailable(ValueError):
    """The approximation could not schedule its network; not an illegality verdict."""


@dataclass(frozen=True)
class Network:
    ids: tuple
    duration: tuple
    ddr: tuple
    successors: tuple
    degree: tuple
    pipes: tuple


def network(bundle, settings, prepared=None, *, include_pipes=True):
    from schedule_step3 import _op_duration, _uses_ddr_bandwidth, op_pipe
    ids, durations, ddr, pipes, parents = [], [], [], [], {}
    for core, prefix in bundle['cores'].items():
        if prepared is None:
            graph, seq = prefix['graph'], prefix['seq']
            ops = {o['id']: o for o in graph['ops']}
            tensors = {t['id']: t for t in graph['tensors']}
            ins, outs = {o: set() for o in ops}, {o: set() for o in ops}
            predecessors, producers = {o: set() for o in ops}, defaultdict(set)
            for edge in graph['edges']:
                a, b = edge['source'], edge['target']
                if a in ops and b in ops:
                    predecessors[b].add(a)
                elif a in ops:
                    outs[a].add(b); producers[b].add(a)
                elif b in ops:
                    ins[b].add(a)
            for op in ops:
                for tensor in ins[op]:
                    predecessors[op].update(producers[tensor])
            ins = {o: sorted(x) for o, x in ins.items()}
            outs = {o: sorted(x) for o, x in outs.items()}
            pipe_orders = defaultdict(list)
            for op in seq:
                pipe_orders[op_pipe(ops[op])].append(op)
        else:
            task = prepared[core]
            ops, tensors = task['op_by_id'], task['tensor_by_id']
            ins, outs, seq = task['in_tids'], task['out_tids'], task['seq']
            predecessors, pipe_orders = task['op_preds'], task['pipe_ops']
        for op in seq:
            key = core, op
            ids.append(key); pipes.append((core, op_pipe(ops[op])))
            durations.append(_op_duration(ops[op], ins, outs, tensors, settings.bandwidth))
            ddr.append(_uses_ddr_bandwidth(ops[op], ins, outs, tensors))
            parents[key] = {(core, p): 0 for p in predecessors[op]}
        if include_pipes:
            for order in pipe_orders.values():
                for a, b in zip(order, order[1:]):
                    parents[core, b][core, a] = 0
    for link in bundle['cross_links']:
        a = link['source_core'], link['source_copy_out_id']
        b = link['target_core'], link['target_copy_in_id']
        parents[b][a] = settings.delay
    index = {key: i for i, key in enumerate(ids)}
    successors = [[] for _ in ids]
    for key in ids:
        for parent, lag in sorted(parents[key].items()):
            successors[index[parent]].append((index[key], lag))
    return Network(tuple(ids), tuple(durations), tuple(ddr), tuple(map(tuple, successors)),
                   tuple(len(parents[key]) for key in ids), tuple(pipes))


def lower_bound(net):
    """Necessary path, per-compute-pipe load and total DDR service (maximum)."""
    degree = list(net.degree)
    ready = [i for i, n in enumerate(degree) if not n]
    ends = list(net.duration)
    visited = 0
    for i in ready:
        visited += 1
        for child, lag in net.successors[i]:
            ends[child] = max(ends[child], ends[i] + lag + net.duration[child])
            degree[child] -= 1
            if not degree[child]:
                ready.append(child)
    if visited != len(degree):
        raise ModelUnavailable('cyclic necessary dependency network')
    loads = defaultdict(int)
    for pipe, duration in zip(net.pipes, net.duration):
        if pipe[1] in ('PIPE_M', 'PIPE_V'):
            loads[pipe] += duration
    return max(max(ends, default=0), max(loads.values(), default=0),
               sum(d for d, uses in zip(net.duration, net.ddr) if uses))


def simulate(net, *, trace=False, check=lambda: None):
    degree = list(net.degree)
    release, starts, ends = ([0] * len(degree) for _ in range(3))
    ddr, normal, wake = {}, [], []
    now = done = events = 0
    for i, n in enumerate(degree):
        if not n:
            heapq.heappush(wake, (0, i))
    while done < len(degree):
        if events % 128 == 0:
            check()
        finished = []
        while normal and normal[0][0] <= now:
            _, i = heapq.heappop(normal); finished.append(i)
        finished.extend(i for i, work in ddr.items() if work <= 1e-8)
        for i in finished:
            ddr.pop(i, None); ends[i] = now; done += 1
            for child, lag in net.successors[i]:
                release[child] = max(release[child], now + lag)
                degree[child] -= 1
                if degree[child] == 0:
                    heapq.heappush(wake, (release[child], child))
        while wake and wake[0][0] <= now:
            _, i = heapq.heappop(wake); starts[i] = now
            if net.ddr[i]:
                ddr[i] = float(net.duration[i])
            else:
                heapq.heappush(normal, (now + net.duration[i], i))
        if done == len(degree):
            break
        targets = []
        if normal: targets.append(normal[0][0])
        if wake: targets.append(wake[0][0])
        if ddr: targets.append(math.ceil(now + min(ddr.values()) * len(ddr) - 1e-9))
        if not targets:
            raise ModelUnavailable('event model has no progress')
        nxt = min(targets)
        if nxt <= now:
            raise ModelUnavailable('non-increasing event clock')
        elapsed = float(nxt - now)
        active = [i for i, work in ddr.items() if work > 1e-9]
        while elapsed > 1e-9 and active:
            part = min(elapsed, min(ddr[i] for i in active) * len(active))
            for i in active:
                ddr[i] = max(0., ddr[i] - part / len(active))
            elapsed -= part
            active = [i for i in active if ddr[i] > 1e-9]
        now = nxt; events += 1
    check()
    return dict(makespan=max(ends, default=0), events=events,
                starts=tuple(starts) if trace else (), ends=tuple(ends) if trace else ())
