"""式 (6)—(30)：确定性的就绪节点生长，试算与接受严格分离。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from heapq import nsmallest

from .graph import GraphIndex, PIPES, ceil_div

BETAS = (1, 2, 3, 4)
SHORTLIST_SIZE = 8


@dataclass(frozen=True)
class Trial:
    node: int
    q: int
    h: int
    r: int
    d0: int
    duration: int
    peak: dict[str, int]
    work: dict[str, int]
    added_inputs: frozenset[int]
    removed_outputs: frozenset[int]
    copy_gain: int
    gain: int


class TaskGrowth:
    def __init__(self, index: GraphIndex, remaining: Counter):
        self.index = index
        # 当前候选范围内尚未接受的消费者数，由 accept 更新；试算只读。
        self.remaining = remaining
        self.nodes = []
        self.members = set()
        self.inputs, self.outputs = set(), set()
        self.used = Counter()
        self.retained = set()
        self.retained_bytes = dict.fromkeys(index.settings.capacity, 0)
        self.peak = dict.fromkeys(index.settings.capacity, 0)
        self.work = dict.fromkeys(PIPES, 0)
        self.h, self.span = {}, {}
        self.span_max = (0, None)
        self.frontier = set()
        self.q = self.r = self.d0 = self.duration = 0

    def boundary_delta(self, v: int) -> tuple[frozenset[int], frozenset[int], int]:
        g = self.index
        added = frozenset(t for t in g.inputs[v]
                          if g.tensors[t].producer not in self.members and t not in self.inputs)
        removed = frozenset(t for t in g.inputs[v] if t in self.outputs
                            and not g.tensors[t].final_output
                            and self.used[t] + 1 == len(g.tensors[t].consumers))
        q = self.q + sum(g.tensors[t].copy_cycles for t in added | g.outputs[v])
        q -= sum(g.tensors[t].copy_cycles for t in removed)
        return added, removed, q

    def copy_gain(self, v: int) -> int:
        # 式 (11) 的精确输入侧增量；就绪追加的输出均是新边界，抵消。
        gain = 0
        for tid in self.index.inputs[v]:
            tensor = self.index.tensors[tid]
            if tensor.producer in self.members:
                gain += tensor.copy_cycles
                if not tensor.final_output and self.used[tid] + 1 == len(tensor.consumers):
                    gain += tensor.copy_cycles
            elif tid in self.inputs:
                gain += tensor.copy_cycles
        return gain

    def trial(self, v: int) -> Trial:
        """纯试算；依赖前驱已被接受是调用方的前置条件。"""
        g = self.index
        added, removed, q = self.boundary_delta(v)
        work = dict(self.work)
        work[g.ops[v]["pipe"]] += g.cycles(v)
        h = g.cycles(v) + max(
            max((self.h[u] for u in g.preds[v] & self.members), default=0),
            sum(g.tensors[t].copy_cycles for t in g.inputs[v]
                if g.tensors[t].producer not in self.members))
        r = max(self.r, max((g.a[u] for u in g.preds[v] - self.members), default=0))
        reduced = Counter()
        for t in removed:
            reduced[g.tensors[t].producer] += g.tensors[t].copy_cycles
        new_span = h + sum(g.tensors[t].copy_cycles for t in g.outputs[v])
        old_max, old_winner = self.span_max
        # 追加只会减少已有节点的输出边界费用。新跨度已达旧最大值，
        # 或旧最大值对应节点未受影响时，无需再次遍历整个 Task。
        if new_span >= old_max:
            span = new_span
        elif old_winner not in reduced:
            span = old_max
        else:
            span = max(new_span, max((self.span[u] - reduced[u] for u in self.nodes), default=0))
        d0 = max(*work.values(), q, span)
        required = dict(self.retained_bytes)
        for tid in (g.inputs[v] | g.outputs[v]) - self.retained:
            t = g.tensors[tid]
            required[t.pos] += t.size
        peak = {p: max(self.peak[p], required[p]) for p in self.peak}
        duration = d0 + g.pressure(peak)
        tail_s = max((g.b[u] for u in self.frontier if u != v), default=0)
        f_s = self.r + self.duration
        f_separate = max(f_s + g.settings.same_core_wait, g.single_r[v]) + g.single_d[v]
        separate = max(f_s + tail_s, f_separate + g.tail[v])
        join = r + duration + max(tail_s, g.tail[v])
        return Trial(v, q, h, r, d0, duration, peak, work, added, removed,
                     self.q + g.single_q[v] - q, separate - join)

    def accept(self, trial: Trial) -> None:
        g, v = self.index, trial.node
        if v in self.members:
            raise ValueError(f"duplicate acceptance of op {v}")
        self.nodes.append(v)
        self.members.add(v)
        self.inputs.update(trial.added_inputs)
        self.outputs.difference_update(trial.removed_outputs)
        self.outputs.update(g.outputs[v])
        self.h[v] = trial.h
        for t in trial.removed_outputs:
            self.span[g.tensors[t].producer] -= g.tensors[t].copy_cycles
        self.span[v] = trial.h + sum(g.tensors[t].copy_cycles for t in g.outputs[v])
        old_max, old_winner = self.span_max
        if self.span[v] >= old_max:
            self.span_max = (self.span[v], v)
        elif old_winner is not None and self.span[old_winner] != old_max:
            winner = max(self.span, key=self.span.get)
            self.span_max = (self.span[winner], winner)
        for tid in g.inputs[v]:
            self.used[tid] += 1
            self.remaining[tid] -= 1
        for tid in g.inputs[v] | g.outputs[v]:
            t = g.tensors[tid]
            keep = self.remaining[tid] > 0
            if keep and tid not in self.retained:
                self.retained.add(tid)
                self.retained_bytes[t.pos] += t.size
            elif not keep and tid in self.retained:
                self.retained.remove(tid)
                self.retained_bytes[t.pos] -= t.size
        self.frontier.discard(v)
        self.frontier.update(g.succs[v])
        self.peak, self.work = trial.peak, trial.work
        self.q, self.r, self.d0, self.duration = trial.q, trial.r, trial.d0, trial.duration

    def shortlist(self, ready: set[int], limit: int, size: int = SHORTLIST_SIZE) -> list[int]:
        g = self.index
        fit = [v for v in ready if self.work[g.ops[v]["pipe"]] + g.cycles(v) <= limit]
        rank = lambda v: (-g.b[v], v)
        candidates = set(nsmallest(size, fit, key=rank))
        reused_tensors = self.inputs | self.outputs
        # 有正复用收益的候选必须触及现有边界。若不足 K 个，搬运名单
        # 剩余的零收益节点必在前一份 b(v) 前 K 名中，合并去重结果相同。
        reuse_fit = (v for v in fit if not g.inputs[v].isdisjoint(reused_tensors))
        candidates.update(nsmallest(size, reuse_fit, key=lambda v: (-self.copy_gain(v), -g.b[v], v)))
        candidates.update(nsmallest(size, (v for v in fit if g.preds[v] & self.members), key=rank))
        if self.work[PIPES[0]] != self.work[PIPES[1]]:
            lighter = min(PIPES, key=self.work.get)
            candidates.update(nsmallest(size, (v for v in fit if g.ops[v]["pipe"] == lighter), key=rank))
        return sorted(candidates)


def partition(index: GraphIndex, scopes: list[set[int]], num_cores: int,
              beta: int, progress=None) -> tuple[list[list[int]], list[dict]]:
    if beta not in BETAS:
        raise ValueError(f"beta must be one of {BETAS}")
    target = max(1, ceil_div(max(index.work.values()), beta * num_cores))
    pending = {v: len(index.preds[v]) for v in index.ids}
    tasks, records = [], []
    processed = 0
    for scope_id, scope in enumerate(scopes):
        ready = {v for v in scope if pending[v] == 0}
        remaining = Counter(t for v in scope for t in index.inputs[v])
        unassigned = len(scope)
        while unassigned:
            if not ready:
                raise RuntimeError("no ready operation in an unfinished construction scope")
            state = TaskGrowth(index, remaining)
            seed = min(ready, key=lambda v: (-index.b[v], index.single_r[v], index.single_d[v], v))
            limit = max(target, index.cycles(seed))
            trial = state.trial(seed)
            gains = []
            while True:
                v = trial.node
                gains.append(None if not state.nodes else trial.gain)
                state.accept(trial)
                ready.remove(v)
                unassigned -= 1
                processed += 1
                if progress is not None and processed % 1000 == 0:
                    progress({"event": "partition_progress", "processed_ops": processed,
                              "total_ops": len(index.ids), "closed_tasks": len(tasks)})
                for u in sorted(index.succs[v]):
                    pending[u] -= 1
                    if pending[u] == 0:
                        ready.add(u)
                if not unassigned:
                    reason = "scope_exhausted"
                    break
                shortlist = state.shortlist(ready, limit)
                if not shortlist:
                    reason = "grain_limit"
                    break
                trial = max((state.trial(u) for u in shortlist), key=lambda t: (
                    t.gain, t.copy_gain, index.b[t.node], -t.node))
                if trial.gain < 0:
                    reason = "negative_gain"
                    break
            tasks.append(state.nodes)
            records.append({"scope": scope_id, "nodes": state.nodes, "grain_limit": limit,
                            "accepted_gains": gains, "close_reason": reason,
                            "work_cycles": state.work, "boundary_copy_cycles": state.q,
                            "virtual_peak_bytes": state.peak, "reference_duration": state.duration,
                            "reference_ready": state.r})
    return tasks, records
