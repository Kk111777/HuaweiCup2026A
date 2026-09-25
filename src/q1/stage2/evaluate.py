"""隔离调用原版官方函数，保留中间记录；不修改模块全局状态或官方源文件。"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import pickle
from types import FunctionType

from .. import official
from .state import digest
import multicore_cut_evaluate_problem_1 as engine


def isolated(function, replacements):
    namespace = dict(function.__globals__)
    namespace.update(replacements)
    result = FunctionType(function.__code__, namespace, function.__name__,
                          function.__defaults__, function.__closure__)
    result.__kwdefaults__ = function.__kwdefaults__
    return result


class Evaluator:
    SECONDARY_CACHE_BYTES = 128 * 1024 * 1024

    def __init__(self, context, progress=None):
        self.context = context
        self.progress = progress or (lambda event: None)
        self.identity = digest({"graph": context.graph, "config": vars(context.settings),
                                "official": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                             for p in sorted(official.CODE.glob("*.py"))}})
        self.prepared_cache = OrderedDict()
        self.result_cache = OrderedDict()
        self.secondary_cache = OrderedDict()
        self.statistics = dict(prepared_hits=0, result_hits=0, preparations=0, evaluations=0,
                               budget_evaluations=0, secondary_hits=0,
                               secondary_bytes=0, secondary_evictions=0)

    def prepare(self, state):
        key = (self.identity, state.partition_signature)
        if key in self.prepared_cache:
            self.statistics["prepared_hits"] += 1
            self.prepared_cache.move_to_end(key)
            template = self.prepared_cache[key]
        else:
            captures = []

            def capture(graph, seq, capacity):
                result = engine.step2_spill_insertion(graph, seq, capacity)
                captures.append({"boundary_graph": graph, "step1": list(seq), "step2": result})
                self.progress({"event": "task_prepared_step2", "task_number": len(captures)})
                return result

            build = isolated(engine._build_scene_a_tasks, {"step2_spill_insertion": capture})
            cfg = self.context.settings
            tasks, cross, traffic, view = build(self.context.graph, state.plan, cfg.bandwidth, cfg.capacity)
            if len(captures) != len(tasks):
                raise RuntimeError("official preparation capture does not match Tasks")
            diagnostic = {j: record for j, record in zip(sorted(tasks), captures)}
            template = dict(tasks=tasks, cross=cross, traffic=traffic, diagnostics=diagnostic)
            self.prepared_cache[key] = template
            self.statistics["preparations"] += 1
            if len(self.prepared_cache) > 2:
                self.prepared_cache.popitem(last=False)
        return template

    def cached(self, state):
        key = digest((self.identity, state.signature))
        if key not in self.result_cache:
            return None
        self.statistics["result_hits"] += 1
        self.result_cache.move_to_end(key)
        return pickle.loads(self.result_cache[key])

    def evaluate(self, state, prepared=None):
        cached = self.cached(state)
        if cached is not None:
            return cached
        prepared = prepared or self.prepare(state)
        key = digest((self.identity, state.signature))
        if key in self.secondary_cache:
            # Only reached where the legacy implementation would simulate.
            # These bytes are generated in this evaluator, never loaded from files.
            payload = self.secondary_cache[key]
            result = pickle.loads(payload)
            self.secondary_cache.move_to_end(key)
            self.statistics["secondary_hits"] += 1
            self.statistics["budget_evaluations"] += 1
            self._remember_primary(key, payload)
            return result
        # The unmodified simulator only writes the outer task's pipe_cursor.
        # All inner op/tensor/dependency/pipe structures are read-only. Keep the
        # outer dictionaries and execution fields private to this invocation.
        tasks = {j: dict(task) for j, task in prepared["tasks"].items()}
        view = official.derive_multicore_plan(self.context.graph, state.plan)
        official.validate_task_order(view)
        for j, task in tasks.items():
            task["core_id"] = state.core[j]
            task["pred_tasks"] = set(state.preds[j])
            task["pipe_cursor"] = {pipe: 0 for pipe in task["pipe_ops"]}

        def supply(*args, **kwargs):
            return tasks, prepared["cross"], deepcopy(prepared["traffic"]), view

        simulate = isolated(engine.evaluate_scene_a, {"_build_scene_a_tasks": supply})
        cfg = self.context.settings
        result = simulate(self.context.graph, state.plan, bandwidth=cfg.bandwidth,
                          capacity=cfg.capacity, cross_core_wait=cfg.cross_core_wait,
                          same_core_wait=cfg.same_core_wait)
        self.statistics["evaluations"] += 1
        self.statistics["budget_evaluations"] += 1
        payload = pickle.dumps(result, protocol=5)
        self._remember_primary(key, payload)
        if len(payload) <= self.SECONDARY_CACHE_BYTES:
            while self.secondary_cache and self.statistics["secondary_bytes"] + len(payload) > self.SECONDARY_CACHE_BYTES:
                _, evicted = self.secondary_cache.popitem(last=False)
                self.statistics["secondary_bytes"] -= len(evicted)
                self.statistics["secondary_evictions"] += 1
            self.secondary_cache[key] = payload
            self.statistics["secondary_bytes"] += len(payload)
        return result

    def _remember_primary(self, key, payload):
        # Both tiers share immutable bytes; each lookup returns a detached result.
        # The four-entry LRU policy itself is unchanged.
        self.result_cache[key] = payload
        if len(self.result_cache) > 4:
            self.result_cache.popitem(last=False)
