"""自适应评估：近似结果可接纳与保存，官方结果用于独立校准。"""
from __future__ import annotations

from collections import Counter, OrderedDict
import math
import time

from .schedule import validate_plan, fixed_queue_state
from .shared import plan_digest
from .approximation import task_profile, simulate_plan, original_copy_bytes


class IncrementalEvaluator:
    """复用局部 Pipe DAG，模拟共享 DDR 事件；内存压力作为未建模 Spill 的风险信号。"""

    def __init__(self, graph, settings, options):
        self.graph, self.settings = graph, settings
        self.task_limit = options.get("task_cache_operations", 100000)
        self.plan_limit = options.get("plan_cache_entries", 24)
        if self.task_limit < 1 or self.plan_limit < 1:
            raise ValueError("cache limits must be positive")
        self.tasks, self.plans = OrderedDict(), OrderedDict()
        self.cached_operations = 0
        self.stats = Counter()
        self.calibration = OrderedDict()
        self.original_copy_bytes = original_copy_bytes(graph)

    def _profile(self, block):
        if block in self.tasks:
            self.tasks.move_to_end(block)
            self.stats["task_cache_hits"] += 1
            return self.tasks[block]
        self.stats["task_cache_misses"] += 1
        profile = task_profile(self.graph, block, self.settings)
        self.tasks[block] = profile
        self.cached_operations += len(block)
        while self.cached_operations > self.task_limit and len(self.tasks) > 1:
            removed, _ = self.tasks.popitem(last=False)
            self.cached_operations -= len(removed)
        return profile

    def estimate(self, plan, reference=None):
        started = time.perf_counter()
        previous_stats = self.stats.copy()
        digest = plan_digest(plan)
        blocks = validate_plan(self.graph, plan)
        profiles = {block: self._profile(block) for block in blocks}
        cached = self.plans.get(digest)
        base = self.plans.get(reference)
        if cached is None:
            cached = fixed_queue_state(self.graph, plan, blocks,
                                       {i: profiles[b]["duration"] for i, b in enumerate(blocks)},
                                       self.settings, base)
            changed, propagated = cached["changed_tasks"], cached["propagated_tasks"]
            traffic = sum(p["boundary_bytes"] for p in profiles.values())
            cached["raw_cycles"] = max(1, simulate_plan(self.graph, plan, blocks, profiles, self.settings))
            cached["pressure_risk"] = any(p["pressure_ratio"] > 1 for p in profiles.values())
            raw = cached["raw_cycles"]
            workload = sum(p["duration"] for p in profiles.values())
            cores = max(1, sum(bool(q) for q in plan["core_schedules"]))
            ids = sorted(set(plan["node_to_subgraph"].values()))
            by_id = dict(zip(ids, blocks))
            # 只使用当前方案可计算的特征；官方 spill 字节数不是预测输入。
            cached["features"] = (
                math.log1p(max(p["pressure_ratio"] for p in profiles.values())),
                math.log1p(sum(p["pressure_ratio"] * p["duration"] for p in profiles.values()) / workload),
                sum(p["duration"] for p in profiles.values() if p["pressure_ratio"] > 1) / workload,
                math.log1p(traffic / self.settings["bandwidth"] / raw),
                max(p["duration"] for p in profiles.values()) / raw,
                workload / (raw * cores),
                math.log1p(len(blocks) / cores),
                math.log1p(sum(profiles[by_id[q[0]]]["read_cycles"]
                              for q in plan["core_schedules"] if q) / raw),
                math.log1p(cores),
            )
            self.plans[digest] = cached
            self.stats["propagated_tasks"] += propagated
        else:
            changed = propagated = 0
            self.stats["plan_cache_hits"] += 1
        self.plans.move_to_end(digest)
        while len(self.plans) > self.plan_limit:
            self.plans.popitem(last=False)
        self.stats["estimates"] += 1
        # 同图最近的已核验样本；不同压力区间避免互相污染，远距离收缩回物理模型。
        neighbors = []
        for features, log_ratio, spill_risk in reversed(self.calibration.values()):
            # A measured spill penalty cannot be transferred into a pressure-free plan.
            if spill_risk and not cached["pressure_risk"]:
                continue
            distance = sum(w * (a-b)**2 for w, a, b in
                           zip((4., 4., 2., 1., .5, .5, .15, 1., 2.), features, cached["features"]))
            neighbors.append((distance, log_ratio))
        neighbors.sort(key=lambda item: item[0])
        selected = neighbors[:3]
        weights = [math.exp(-2 * d) for d, _ in selected]
        confidence = math.exp(-.5 * selected[0][0]) if selected else 0.
        correction = (math.exp(confidence * sum(w * item[1] for w, item in zip(weights, selected)) /
                               sum(weights)) if sum(weights) > 1e-12 else 1.)
        predicted = cached["raw_cycles"] * correction
        elapsed = time.perf_counter() - started
        self.stats["estimate_seconds"] += elapsed
        meta = {"method": "incremental_approximation", "seconds": elapsed,
                "changed_tasks": changed, "propagated_tasks": propagated,
                "reused_task_timings": len(blocks) - propagated,
                "task_cache_hits": self.stats["task_cache_hits"] - previous_stats["task_cache_hits"],
                "task_cache_misses": self.stats["task_cache_misses"] - previous_stats["task_cache_misses"],
                "raw_cycles": cached["raw_cycles"],
                "calibration_factor": correction,
                "calibration_samples": len(self.calibration),
                "calibration_confidence": confidence,
                "pressure_risk_tasks": sum(p["pressure_excess_bytes"] > 0 for p in profiles.values()),
                "screening_margin_fraction": .45 if cached['pressure_risk'] else max(.02, .08*(1-confidence)),
                "margin_note": "heuristic screening tolerance, not a statistical confidence interval",
                "delta_cycles": (cached["raw_cycles"] - base["raw_cycles"]) if base else None,
                "approximation_note": "cached local Pipe schedule; dependency/queue propagation; "
                    "shared DDR event service; local reverse-DFS Pipe order; pressure is a risk flag, not measured spill; not official events"}
        return math.ceil(predicted), meta

    def observe(self, plan, result):
        """核验后学习残差；未调用此方法前的预测不会使用该候选的官方结果。"""
        if result.get('evaluation_source') == 'approximate':
            raise ValueError('approximate scores must not train their own calibration')
        movement = result.get('data_movement_bytes', {})
        reported_original = movement.get('original_graph_copy_bytes',
            movement.get('original_copy_bytes', self.original_copy_bytes))
        if reported_original != self.original_copy_bytes:
            raise ValueError('official COPY traffic does not match the current input graph')
        digest = plan_digest(plan)
        if digest not in self.plans:
            self.estimate(plan)
        cached = self.plans[digest]
        ratio = result["makespan"] / cached["raw_cycles"]
        if not math.isfinite(ratio) or ratio <= 0:
            return
        self.calibration[digest] = (cached["features"], math.log(min(4., max(.5, ratio))),
            bool(result.get("data_movement_bytes", {}).get("spill_added_copy_bytes", 0)) or cached['pressure_risk'])
        self.calibration.move_to_end(digest)
        while len(self.calibration) > 32:
            self.calibration.popitem(last=False)
        self.stats["calibration_updates"] += 1

    def result(self, plan, estimated=None, detail=None):
        """Materialize an explicitly approximate result usable by shared operators."""
        if estimated is None or detail is None:
            estimated, detail = self.estimate(plan)
        blocks = validate_plan(self.graph,plan)
        profiles = {b:self._profile(b) for b in blocks}
        trace = simulate_plan(self.graph,plan,blocks,profiles,self.settings,trace=True)
        factor = estimated/max(1,trace['makespan'])
        scaled = lambda n: round(n*factor)
        ids = sorted(set(plan['node_to_subgraph'].values()))
        index = {t:i for i,t in enumerate(ids)}
        core = {t:c for c,q in enumerate(plan['core_schedules']) for t in q}
        timelines = [dict(core_id=c,tasks=[],subgraphs=[],ops=[]) for c in range(len(plan['core_schedules']))]
        step3 = {}
        for t in ids:
            i=index[t];start=scaled(trace['task_starts'][i]);end=scaled(trace['task_ends'][i])
            timelines[core[t]]['tasks'].append(dict(task_id=t,start=start,end=end,duration=end-start))
            step3[str(t)] = dict(local_makespan=max(1,scaled(profiles[blocks[i]]['duration'])))
        for t,v,end in trace['operation_ends']:
            timelines[core[t]]['ops'].append(dict(task_id=t,op_id=v,end=scaled(end)))
        scheduled = sum(p['boundary_bytes'] for p in profiles.values())
        added = scheduled-self.original_copy_bytes
        return dict(scene='A',evaluation_source='approximate',makespan=estimated,
            approximation=detail,approximate_timeline=True,
            num_cores=len(timelines),per_core_timeline=timelines,step3_by_task=step3,
            data_movement_bytes=dict(original_graph_copy_bytes=self.original_copy_bytes,
                original_copy_bytes=self.original_copy_bytes,
                scheduled_copy_bytes=scheduled,added_copy_bytes=added,partition_added_copy_bytes=added,
                spill_added_copy_bytes=None),
            movement_note='Boundary traffic estimate; Spill bytes unmodelled, not measured zero.')


class EvaluationPolicy:
    """便宜时逐个全量，昂贵时分批筛选；预留末次官方核验的预测耗时。"""

    def __init__(self, target, options):
        self.threshold = max(2., target * options.get("full_cost_fraction", .01))
        self.batch_size = options.get("approximate_batch_size", 6)
        if not math.isfinite(self.threshold) or self.threshold <= 0 or type(self.batch_size) is not int or self.batch_size < 2:
            raise ValueError("invalid adaptive evaluation policy")
        self.samples, self.errors = [], []
        self.timeout_floor = 0.
        self.calls = 0
        self.seconds = 0.

    @property
    def predicted_seconds(self):
        return max(self.timeout_floor, 1.25 * max(self.samples[-3:], default=0.))

    @property
    def batched(self):
        return self.predicted_seconds > self.threshold

    @property
    def reserve_seconds(self):
        return 2 * self.predicted_seconds + 1

    @property
    def interval(self):
        # 局部模型近期偏差大时增加核验频率，但仍由成本/剩余预算限制调用。
        return max(2, self.batch_size // 2) if self.errors and max(self.errors[-3:]) > .15 else self.batch_size

    def observe(self, record, estimated_cycles=None, actual_cycles=None):
        self.calls += 1
        self.seconds += record.get("seconds", 0)
        if record["status"] == "ok":
            self.samples.append(record["seconds"])
            self.timeout_floor = 0.
            if estimated_cycles is not None and actual_cycles:
                self.errors.append(abs(estimated_cycles / actual_cycles - 1))
        elif record.get("timed_out"):
            self.timeout_floor = max(self.timeout_floor, 1.25 * record["timeout_seconds"])

    def metadata(self):
        return {"mode": "batched" if self.batched else "full", "full_calls": self.calls,
                "cost_source": "observed_multicore" if self.calls else "unmeasured",
                "full_seconds": self.seconds, "predicted_full_seconds": self.predicted_seconds,
                "full_cost_threshold_seconds": self.threshold, "reserve_seconds": self.reserve_seconds,
                "batch_interval": self.interval, "recent_absolute_errors": self.errors[-3:]}
