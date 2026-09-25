"""公式独立重算、官方执行、小图边界及运行产物契约。"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from q1 import official
from q1.graph import GraphIndex, PIPES
from q1.partition import BETAS, TaskGrowth, partition
from q1.schedule import assign_cores
from q1.solver import initialize, InitializationError, repair_and_preanalyze
from q1.run import (fingerprint, metrics, provenance, reusable, run_case,
                    run_cases, summarize, write_json)

SETTINGS = official.Settings.read(official.REFERENCE / "data" / "config.txt")


def graph_of(n, links=(), cycles=None, pipes=None, shared=True, final=True, size=12, pos="UB"):
    cycles = cycles if cycles is not None else [20 + 7 * v for v in range(n)]
    pipes = pipes if pipes is not None else [PIPES[v % 2] for v in range(n)]
    graph = {"ops": [{"id": v, "op": "ADD", "pipe": pipes[v], "cycles": cycles[v]}
                     for v in range(n)], "tensors": [], "edges": []}
    next_id = 1000

    def tensor(position):
        nonlocal next_id
        tid = next_id
        next_id += 1
        graph["tensors"].append({"id": tid, "pos": position, "size": size})
        return tid

    def copy(kind):
        nonlocal next_id
        oid = next_id
        next_id += 1
        graph["ops"].append({"id": oid, "op": kind,
                             "pipe": "PIPE_MTE2" if kind == "COPY_IN" else "PIPE_MTE3",
                             "cycles": max(1, (size + 59) // 60)})
        return oid

    def edge(a, b):
        graph["edges"].append({"source": a, "target": b})

    if shared and n:
        ddr, local, op = tensor("DDR"), tensor(pos), copy("COPY_IN")
        edge(ddr, op)
        edge(op, local)
        for v in range(n):
            edge(local, v)
    for v in range(n):
        out = tensor(pos)
        edge(v, out)
        users = [b for a, b in links if a == v]
        for u in users:
            edge(out, u)
        if final and not users:
            op, ddr = copy("COPY_OUT"), tensor("DDR")
            edge(out, op)
            edge(op, ddr)
    return graph


def reference(g, nodes, scope, closed=frozenset()):
    """独立按集合定义重算式 (7)—(20)，不调用 TaskGrowth 或边界增量。"""
    members = set(nodes)
    ins = {t for t, x in g.tensors.items() if x.consumers & members and x.producer not in members}
    outs = {t for t, x in g.tensors.items() if x.producer in members
            and (x.final_output or x.consumers - members)}
    q = sum(g.tensors[t].copy_cycles for t in ins) + sum(g.tensors[t].copy_cycles for t in outs)
    h = {}
    for v in nodes:
        h[v] = g.cycles(v) + max(max((h[u] for u in g.preds[v] & members), default=0),
                               sum(g.tensors[t].copy_cycles for t in g.inputs[v] & ins))
    work = g.workload(nodes)
    span = max((h[v] + sum(g.tensors[t].copy_cycles for t in g.outputs[v] & outs)
                for v in nodes), default=0)
    d0 = max(*work.values(), q, span)
    prefix, touched, peak = set(), set(), dict.fromkeys(g.settings.capacity, 0)
    for v in nodes:
        kept = {t for t in touched if g.tensors[t].consumers & (scope - closed - prefix)}
        needed = kept | g.inputs[v] | g.outputs[v]
        for p in peak:
            peak[p] = max(peak[p], sum(g.tensors[t].size for t in needed if g.tensors[t].pos == p))
        prefix.add(v)
        touched.update(g.inputs[v] | g.outputs[v])
    external = set().union(*(g.preds[v] for v in nodes)) - members if nodes else set()
    r = max((g.a[u] for u in external), default=0)
    penalty = sum(2 * ((max(0, peak[p] - cap) + g.settings.bandwidth - 1) // g.settings.bandwidth)
                  for p, cap in g.settings.capacity.items())
    return dict(q=q, d0=d0, duration=d0 + penalty, r=r, peak=peak, inputs=ins, outputs=outs)


class GrowthTests(unittest.TestCase):
    def test_shared_input_is_not_dependency_and_copy_counted_once(self):
        g = GraphIndex(graph_of(2), SETTINGS)
        self.assertEqual(len(g.components), 2)
        self.assertFalse(g.preds[1])
        state = TaskGrowth(g, Counter(t for v in g.ids for t in g.inputs[v]))
        state.accept(state.trial(0))
        self.assertEqual(state.copy_gain(1), 1)
        self.assertEqual(state.trial(1).q, 3)

    def test_last_consumer_removes_one_producer_write(self):
        g = GraphIndex(graph_of(3, [(0, 1), (0, 2)], shared=False), SETTINGS)
        state = TaskGrowth(g, Counter(t for v in g.ids for t in g.inputs[v]))
        state.accept(state.trial(0))
        self.assertEqual(state.copy_gain(1), 1)
        state.accept(state.trial(1))
        self.assertEqual(state.copy_gain(2), 2)

    def test_independent_and_dependent_pipes_have_different_span(self):
        graph = {"ops": [{"id": 0, "op": "M", "pipe": "PIPE_M", "cycles": 1000},
                         {"id": 1, "op": "V", "pipe": "PIPE_V", "cycles": 400}],
                 "tensors": [], "edges": []}
        for dependent, expected in ((False, 1000), (True, 1400)):
            graph["edges"] = [{"source": 0, "target": 1}] if dependent else []
            g = GraphIndex(deepcopy(graph), SETTINGS)
            state = TaskGrowth(g, Counter())
            state.accept(state.trial(0))
            self.assertEqual(state.trial(1).d0, expected)

    def test_trial_does_not_mutate_and_pressure_resets(self):
        small = official.Settings({"L1": 100, "UB": 100}, 60, 100, 1000)
        graph = graph_of(4, [(0, 2), (1, 3)], shared=False, size=60, pos="L1")
        # 两个生产者各输出 60；消费者改为无输出，确保单操作不超过容量。
        remove = {e["target"] for e in graph["edges"] if e["source"] in (2, 3)}
        graph["tensors"] = [t for t in graph["tensors"] if t["id"] not in remove]
        copy_remove = {e["target"] for e in graph["edges"] if e["source"] in remove}
        graph["ops"] = [o for o in graph["ops"] if o["id"] not in copy_remove]
        graph["edges"] = [e for e in graph["edges"]
                          if e["source"] not in remove | copy_remove and e["target"] not in remove]
        g = GraphIndex(graph, small)
        remaining = Counter(t for v in g.ids for t in g.inputs[v])
        state = TaskGrowth(g, remaining)
        state.accept(state.trial(0))
        before = deepcopy({k: v for k, v in state.__dict__.items() if k != "index"})
        trial = state.trial(1)
        self.assertEqual(before, {k: v for k, v in state.__dict__.items() if k != "index"})
        self.assertEqual(trial.peak["L1"], 120)
        self.assertEqual(trial.duration - trial.d0, 2)
        reset = TaskGrowth(g, remaining)
        self.assertEqual(reset.trial(1).peak["L1"], 60)
        self.assertEqual(reset.trial(1).peak["UB"], 0)

    def test_no_consumer_output_and_zero_bytes_copy(self):
        g = GraphIndex(graph_of(1, final=False, size=0), SETTINGS)
        expected = reference(g, [0], set(g.ids))
        self.assertEqual(len(expected["outputs"]), 1)
        self.assertEqual(expected["q"], 2)

    def test_span_cache_recomputes_when_last_consumer_reduces_winner(self):
        graph = graph_of(3, [(0, 2)], cycles=[1, 50, 1], shared=False,
                         final=False, size=1)
        produced_by_zero = next(e["target"] for e in graph["edges"] if e["source"] == 0)
        next(t for t in graph["tensors"] if t["id"] == produced_by_zero)["size"] = 6000
        g = GraphIndex(graph, SETTINGS)
        state = TaskGrowth(g, Counter(t for v in g.ids for t in g.inputs[v]))
        state.accept(state.trial(0))
        state.accept(state.trial(1))
        self.assertEqual(state.span_max, (101, 0))
        trial = state.trial(2)
        self.assertEqual(trial.d0, reference(g, [0, 1, 2], set(g.ids))["d0"])
        self.assertEqual(state.span_max, (101, 0))
        state.accept(trial)
        self.assertEqual(state.span_max, (51, 1))

    def test_copy_contraction_and_direct_dependency(self):
        graph = graph_of(2, shared=False)
        graph["ops"].append({"id": 990, "op": "COPY_IN", "pipe": "PIPE_MTE2", "cycles": 1})
        graph["edges"].extend([{"source": 0, "target": 990}, {"source": 990, "target": 1}])
        g = GraphIndex(graph, SETTINGS)
        self.assertEqual(g.preds[1], {0})

    def test_grain_oversized_seed_beta3_and_determinism(self):
        graph = graph_of(5, cycles=[1000, 0, 1, 20, 30], pipes=["PIPE_M"] * 5)
        g = GraphIndex(graph, SETTINGS)
        self.assertEqual(BETAS, (1, 2, 3, 4))
        tasks, records = partition(g, [set(g.ids)], 5, 3)
        self.assertEqual(sorted(v for t in tasks for v in t), list(g.ids))
        self.assertEqual((tasks, records), partition(g, [set(g.ids)], 5, 3))
        for record in records:
            self.assertLessEqual(max(record["work_cycles"].values()), record["grain_limit"])
        self.assertEqual(records[0]["grain_limit"], 1000)

    def test_ready_seed_tie_uses_smallest_id(self):
        g = GraphIndex(graph_of(2, cycles=[5, 5], pipes=["PIPE_M"] * 2), SETTINGS)
        tasks, _ = partition(g, [set(g.ids)], 2, 3)
        self.assertEqual(tasks[0], [0])

    def test_random_differential_and_official_execution(self):
        rng = random.Random(20260924)
        checked = 0
        for _ in range(24):
            n = rng.randint(3, 9)
            links = [(a, b) for a in range(n) for b in range(a + 1, n) if rng.random() < .24]
            graph = graph_of(n, links, cycles=[rng.randint(0, 90) for _ in range(n)],
                             pos=rng.choice(["UB", "L1"]))
            g = GraphIndex(graph, SETTINGS)
            scope, closed = set(g.ids), set()
            remaining = Counter(t for v in g.ids for t in g.inputs[v])
            state = TaskGrowth(g, remaining)
            for v in g.order:
                before = reference(g, state.nodes, scope, closed)
                after = reference(g, [*state.nodes, v], scope, closed)
                trial = state.trial(v)
                for name in ("q", "d0", "duration", "r", "peak"):
                    self.assertEqual(getattr(trial, name), after[name], (v, name))
                single = reference(g, [v], scope, closed | state.members)
                tail_s = max((g.b[u] for o in state.nodes for u in g.succs[o]
                              if u not in state.members | {v}), default=0)
                f_s = before["r"] + before["duration"]
                f_v = max(f_s + SETTINGS.same_core_wait, single["r"]) + single["duration"]
                gain = max(f_s + tail_s, f_v + g.tail[v]) - (
                    after["r"] + after["duration"] + max(tail_s, g.tail[v]))
                self.assertEqual(trial.gain, gain)
                self.assertEqual(trial.copy_gain, before["q"] + single["q"] - after["q"])
                self.assertGreaterEqual(trial.copy_gain, 0)
                state.accept(trial)
                checked += 1
                if len(state.nodes) == 3:
                    closed.update(state.members)
                    state = TaskGrowth(g, remaining)
            solved = initialize(graph, rng.choice([2, 3, 4, 5]), SETTINGS,
                                candidate_jobs=1, beta_search="grid")
            view = official.derive_multicore_plan(graph, solved.plan)
            official.validate_task_order(view)
            self.assertEqual(set(view["mapping"]), set(g.ids))
            self.assertEqual(solved.evaluation, official.evaluate(graph, solved.plan, SETTINGS))
        self.assertGreater(checked, 100)

    def test_shortlist_filter_matches_full_four_lists(self):
        rng = random.Random(8705)
        for _ in range(8):
            n = 45
            links = [(a, b) for a in range(n) for b in range(a + 1, n) if rng.random() < .025]
            g = GraphIndex(graph_of(n, links, shared=False,
                                    cycles=[rng.randrange(100) for _ in range(n)]), SETTINGS)
            state = TaskGrowth(g, Counter(t for v in g.ids for t in g.inputs[v]))
            for v in g.order:
                state.accept(state.trial(v))
                ready = {u for u in g.ids if u not in state.members and g.preds[u] <= state.members}
                limit = max(state.work.values()) + 50
                fit = [u for u in ready if state.work[g.ops[u]["pipe"]] + g.cycles(u) <= limit]
                rank = lambda u: (-g.b[u], u)
                gain = lambda u: state.q + g.single_q[u] - state.boundary_delta(u)[2]
                expected = set(sorted(fit, key=rank)[:8])
                expected.update(sorted(fit, key=lambda u: (-gain(u), -g.b[u], u))[:8])
                expected.update(sorted((u for u in fit if g.preds[u] & state.members), key=rank)[:8])
                if state.work[PIPES[0]] != state.work[PIPES[1]]:
                    lighter = min(PIPES, key=state.work.get)
                    expected.update(sorted((u for u in fit if g.ops[u]["pipe"] == lighter), key=rank)[:8])
                self.assertEqual(state.shortlist(ready, limit), sorted(expected))
                for u in fit:
                    self.assertEqual(state.copy_gain(u), gain(u))


class ScheduleTests(unittest.TestCase):
    def test_serial_repetition_preserves_results_and_fixed_order(self):
        graph = graph_of(7, [(0, 2), (1, 2), (2, 3), (4, 5)])
        for policy in ("fixed", "adaptive", "grid"):
            with self.subTest(policy=policy):
                serial = initialize(graph, 3, SETTINGS, candidate_jobs=1, beta_search=policy)
                events = []
                parallel = initialize(graph, 3, SETTINGS, candidate_jobs=1,
                                      beta_search=policy, progress=events.append)
                self.assertEqual(serial.plan, parallel.plan)
                self.assertEqual(serial.evaluation, parallel.evaluation)
                self.assertEqual(serial.selected, parallel.selected)
                self.assertEqual(serial.search, parallel.search)
                self.assertEqual(
                    [{k: v for k, v in c.items() if k != "elapsed_seconds"} for c in serial.candidates],
                    [{k: v for k, v in c.items() if k != "elapsed_seconds"} for c in parallel.candidates])
                self.assertEqual(sum(e["event"] == "candidate_end" for e in events), len(serial.candidates))
                self.assertEqual(sum(e["event"] == "candidate_start" for e in events), len(serial.candidates))
                self.assertTrue(any(e.get("phase") == "preanalysis" for e in events))
        with self.assertRaises(ValueError):
            initialize(graph, 3, SETTINGS, candidate_jobs=0)
        with self.assertRaises(ValueError):
            initialize(graph, 3, SETTINGS, beta_search="unknown")

    def test_independent_tasks_start_at_zero_and_cross_wait_is_max(self):
        graph = graph_of(3, [(0, 2), (1, 2)], shared=False)
        plan, trace = assign_cores(graph, official.make_plan([[0], [1], [2]], 2),
                                  {0: 300, 1: 200, 2: 20}, SETTINGS)
        self.assertEqual([r["reference_start"] for r in trace[:2]], [0, 0])
        self.assertEqual(trace[2]["reference_start"], 1200)
        self.assertEqual(trace[2]["reference_end"], 1220)
        self.assertEqual(plan["core_schedules"], [[0, 2], [1]])

    def test_repair_halves_each_task_and_keeps_order(self):
        graph = graph_of(5, [(i, i + 1) for i in range(4)])
        repairs = []
        calls = []
        original = official.preanalyze

        def limited(g, plan, settings):
            view = official.derive_multicore_plan(g, plan)
            lengths = [len(view["nodes_by_subgraph"][j]) for j in view["subgraph_ids"]]
            calls.append(lengths)
            if max(lengths) > 1:
                raise official.Step2SchedulingError("injected local scheduling failure")
            return original(g, plan, settings)

        with patch.object(official, "preanalyze", side_effect=limited):
            tasks, _, _ = repair_and_preanalyze(graph, [[0, 1, 2, 3, 4]], 2, SETTINGS, repairs)
        self.assertEqual(calls, [[5], [2, 3], [1, 1, 1, 2], [1, 1, 1, 1, 1]])
        self.assertEqual(tasks, [[0], [1], [2], [3], [4]])
        self.assertEqual(len(repairs), 3)

    def test_spill_success_does_not_trigger_repair(self):
        graph = graph_of(9, [(0, 1), (0, 2), (1, 5), (4, 5),
                             (2, 6), (4, 6), (1, 8), (7, 8)], size=30)
        settings = official.Settings({"UB": 128, "L1": 128}, 60, 100, 1000)
        repairs = []
        with patch.object(official, "preanalyze", wraps=official.preanalyze) as call:
            tasks, plan, _ = repair_and_preanalyze(graph, [list(range(9))], 2, settings, repairs)
        self.assertEqual(tasks, [list(range(9))])
        self.assertEqual(repairs, [])
        self.assertEqual(call.call_count, 1)
        result = official.evaluate(graph, plan, settings)
        self.assertEqual(result["data_movement_bytes"]["spill_added_copy_bytes"], 120)

    def test_unexpected_exception_is_not_repaired(self):
        with patch.object(official, "preanalyze", side_effect=ValueError("program error")):
            with self.assertRaisesRegex(ValueError, "program error"):
                initialize(graph_of(2, [(0, 1)]), 2, SETTINGS, candidate_jobs=1, beta_search="adaptive")

    def test_candidate_counts_and_tie_order(self):
        for links, count in (([], 8), ([(0, 1)], 4)):
            graph = graph_of(2, links)
            with patch.object(official, "evaluate", return_value={"makespan": 10,
                              "data_movement_bytes": {"added_copy_bytes": 0}}):
                solved = initialize(graph, 2, SETTINGS, candidate_jobs=1, beta_search="grid")
            self.assertEqual(len(solved.candidates), count)
            self.assertEqual(solved.selected["mode"], "global")
            self.assertEqual(solved.selected["beta"], 1)
            self.assertEqual([c["beta"] for c in solved.candidates[:4]], [1, 2, 3, 4])

    def test_fallback_only_after_all_candidates_fail(self):
        graph = graph_of(2, [(0, 1)])
        original = official.evaluate
        calls = []

        def fail_candidates(g, plan, settings):
            calls.append(plan)
            if len(calls) <= 4:
                raise official.SceneAEvaluationError("injected simulation failure")
            return original(g, plan, settings)

        with patch.object(official, "evaluate", side_effect=fail_candidates):
            solved = initialize(graph, 2, SETTINGS, candidate_jobs=1, beta_search="grid")
        self.assertEqual(len(calls), 5)
        self.assertEqual(solved.selected["mode"], "singleton_fallback")
        self.assertEqual(solved.plan["core_schedules"], [[0, 1], []])
        with patch.object(official, "evaluate", side_effect=official.SceneAEvaluationError("failed")):
            with self.assertRaises(InitializationError) as failure:
                initialize(graph, 2, SETTINGS, candidate_jobs=1, beta_search="grid")
        self.assertEqual(len(failure.exception.candidates), 5)

    def test_invalid_input_and_empty_computation(self):
        with self.assertRaises(ValueError):
            initialize(graph_of(1), 1, SETTINGS)
        graph = graph_of(1)
        graph["edges"].append({"source": 0, "target": 0})
        with self.assertRaises(ValueError):
            initialize(graph, 2, SETTINGS)
        solved = initialize(graph_of(0), 5, SETTINGS)
        self.assertEqual(solved.plan, {"node_to_subgraph": {}, "core_schedules": [[], [], [], [], []]})
        self.assertEqual(solved.evaluation["makespan"], 0)


class BetaSearchTests(unittest.TestCase):
    @staticmethod
    def evaluator(curves, calls):
        def evaluate(index, cores, mode, beta, scopes, progress=None):
            calls.append((mode, beta))
            value = curves[mode][beta - 1]
            record = {"mode": mode, "beta": beta, "elapsed_seconds": 0}
            if value is None:
                return {**record, "status": "failed", "error": "official scheduling failure"}, None, None
            time, copied = value if isinstance(value, tuple) else (value, 0)
            evaluation = {"makespan": time, "data_movement_bytes": {"added_copy_bytes": copied}}
            return {**record, "status": "success", **evaluation}, {"mode": mode, "beta": beta}, evaluation
        return evaluate

    def solve_curves(self, curves, policy="adaptive", graph=None):
        calls = []
        with patch("q1.solver._evaluate_candidate", side_effect=self.evaluator(curves, calls)):
            solved = initialize(graph if graph is not None else graph_of(2), 2, SETTINGS,
                                candidate_jobs=1, beta_search=policy)
        return solved, calls

    def test_fixed_ablation_keeps_both_modes_and_skips_duplicate_component(self):
        for links, expected in (([], ["global", "components"]), ([(0, 1)], ["global"])):
            calls = []
            with patch("q1.solver._evaluate_candidate", side_effect=self.evaluator(
                    {"global": [100], "components": [90]}, calls)):
                solved = initialize(graph_of(2, links), 2, SETTINGS, candidate_jobs=1, beta_search="fixed")
            self.assertEqual(calls, [(mode, 1) for mode in expected])
            self.assertEqual(solved.selected, {"mode": expected[-1], "beta": 1,
                                               "candidate_index": len(expected) - 1})
            self.assertEqual(solved.search["policy"], "fixed")

    def test_default_api_enables_adaptive_component(self):
        calls = []
        with patch("q1.solver._evaluate_candidate", side_effect=self.evaluator(
                {"global": [100, 90, 90], "components": [140, 130, 120, 80]}, calls)):
            solved = initialize(graph_of(2), 2, SETTINGS, candidate_jobs=1)
        self.assertEqual(calls, [("global", b) for b in (1, 2, 3)] +
                               [("components", b) for b in (1, 2, 3, 4)])
        self.assertEqual(solved.search["policy"], "adaptive")
        self.assertEqual(solved.selected, {"mode": "components", "beta": 4, "candidate_index": 6})

    def test_modes_improve_independently_and_selected_index_is_compact(self):
        solved, calls = self.solve_curves({"global": [100, 110, 1, 1],
                                           "components": [140, 130, 120, 90]})
        self.assertEqual(calls, [("global", 1), ("global", 2),
                                 *[("components", b) for b in (1, 2, 3, 4)]])
        self.assertEqual(solved.selected, {"mode": "components", "beta": 4, "candidate_index": 5})
        self.assertEqual([s["stop_reason"] for s in solved.search["modes"]],
                         ["no_makespan_improvement", "max_beta_reached"])
        self.assertEqual(solved.search["evaluated_candidates"], 6)
        self.assertFalse(solved.search["fallback_used"])

    def test_tie_stops_despite_less_copy_and_best_visited_candidate_is_retained(self):
        solved, calls = self.solve_curves({"global": [(100, 10), (100, 5), (1, 0)],
                                           "components": [150, 160, 1]})
        self.assertEqual(calls, [("global", 1), ("global", 2), ("components", 1), ("components", 2)])
        self.assertEqual(solved.selected, {"mode": "global", "beta": 2, "candidate_index": 1})
        self.assertFalse(solved.candidates[1]["search_decision"]["strictly_improved"])
        self.assertEqual(solved.search["modes"][1]["best_beta"], 1)

    def test_later_reversal_is_not_probed_and_single_component_has_one_search(self):
        solved, calls = self.solve_curves({"global": [100, 90, 95, 1]}, graph=graph_of(2, [(0, 1)]))
        self.assertEqual(calls, [("global", b) for b in (1, 2, 3)])
        self.assertEqual(solved.selected["beta"], 2)
        self.assertEqual(len(solved.search["modes"]), 1)

    def test_failed_candidate_stops_only_its_mode_and_keeps_incumbent(self):
        for curve in ([None, 1], [90, None, 1]):
            with self.subTest(curve=curve):
                solved, calls = self.solve_curves({"global": curve, "components": [100, 110]})
                self.assertEqual(calls[-2:], [("components", 1), ("components", 2)])
                self.assertEqual(solved.search["modes"][0]["stop_reason"], "candidate_failed")
                self.assertEqual(solved.selected["mode"], "components" if curve[0] is None else "global")
                self.assertFalse(solved.search["fallback_used"])

    def test_all_failed_modes_use_fallback_and_preserve_search_on_failure(self):
        solved, calls = self.solve_curves({"global": [None], "components": [None]})
        self.assertEqual(calls, [("global", 1), ("components", 1)])
        self.assertEqual(solved.selected, {"mode": "singleton_fallback", "beta": None, "candidate_index": 2})
        self.assertTrue(solved.search["fallback_used"])
        self.assertEqual(solved.plan["core_schedules"], [[0, 1], []])
        with patch.object(official, "evaluate", side_effect=official.SceneAEvaluationError("failed")):
            with self.assertRaises(InitializationError) as failure:
                self.solve_curves({"global": [None], "components": [None]})
        self.assertEqual(len(failure.exception.candidates), 3)
        self.assertEqual(failure.exception.search["evaluated_candidates"], 2)

    def test_parallel_modes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "parallelism"):
            initialize(graph_of(2), 2, SETTINGS, candidate_jobs=2)


class RunnerTests(unittest.TestCase):
    def test_cli_defaults_to_published_archive(self):
        from q1.reference_cli import main
        with patch("q1.reference_cli.run_cases", return_value=0) as run:
            self.assertEqual(main(["solve", "official/data/case_001.json", "--cores", "2"]), 0)
            self.assertEqual(run.call_args.args[-1], ROOT / "results/problem_1_singlecore")
            self.assertEqual(run.call_args.args[-2], 1)
            self.assertEqual(run.call_args.kwargs["beta_search"], "adaptive")
            self.assertEqual(main(["solve", "official/data/case_001.json", "--cores", "2",
                                   "--recompute-baseline"]), 0)
            self.assertIsNone(run.call_args.args[-1])
            for policy in ("fixed", "adaptive", "grid"):
                self.assertEqual(main(["solve", "official/data/case_001.json", "--beta-search", policy]), 0)
                self.assertEqual(run.call_args.kwargs["beta_search"], policy)

    def test_parallel_cli_and_independent_official_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, graph in (("case_a", graph_of(3)),
                                ("case_b", graph_of(4, [(0, 1), (1, 2), (1, 3)]))):
                write_json(root / (name + ".json"), graph)
            config = official.REFERENCE / "data" / "config.txt"
            command = [sys.executable, str(ROOT / "tools/reference_problem1.py"), "batch", "--data-dir", str(root),
                       "--config", str(config), "--cores", "2", "--jobs", "2",
                       "--candidate-jobs", "1",
                       "--recompute-baseline", "--output-root", str(root / "out")]
            process = subprocess.run(command, cwd=official.ROOT, capture_output=True, timeout=60)
            self.assertEqual(process.returncode, 0, (process.stdout, process.stderr))
            report = official.read_json(root / "out" / "summary.json")
            self.assertTrue(report["complete"])
            self.assertEqual(len(report["rows"]), 4)
            self.assertTrue(all(r["beta_search"] == "adaptive" for r in report["rows"] if r["num_cores"] == 2))
            for name in ("case_a", "case_b"):
                directory = root / "out" / name / "N2"
                checked = root / f"{name}_checked.json"
                process = subprocess.run([
                    sys.executable, str(official.CODE / "multicore_cut_evaluate_problem_1.py"),
                    str(root / (name + ".json")), str(directory / "plan.json"),
                    "--config", str(config), "-o", str(checked),
                    "--trace-output", str(root / f"{name}_trace.json"),
                    "--log-output", str(root / f"{name}.log")],
                    cwd=root, capture_output=True, timeout=60)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(official.read_json(checked), official.read_json(directory / "result.json"))

    def test_roundtrip_resume_tamper_and_parameter_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            root = Path(temporary)
            path = root / "case_test.json"
            write_json(path, graph_of(3, [(0, 2), (1, 2)]))
            config = official.REFERENCE / "data" / "config.txt"
            rows = run_case(str(path), str(config), str(root / "out"), [2], False)
            self.assertTrue(all(r["status"] == "success" for r in rows))
            directory = root / "out" / "case_test" / "N2"
            source = provenance(path, config)
            signature = fingerprint(source, 2)
            self.assertTrue(reusable(directory, signature)[0])
            with patch("q1.run.initialize", side_effect=AssertionError("must resume")), \
                    patch.object(official, "singlecore", side_effect=AssertionError("must resume")):
                again = run_case(str(path), str(config), str(root / "out"), [2], True)
            self.assertTrue(all(r["resumed"] for r in again))
            legacy = deepcopy(source)
            legacy["algorithm"]["betas"] = [1, 2, 4]
            self.assertFalse(reusable(directory, fingerprint(legacy, 2))[0])
            for policy in ("fixed", "grid"):
                source = provenance(path, config, beta_search=policy)
                self.assertFalse(reusable(directory, fingerprint(source, 2))[0])
            (directory / "plan.json").write_text("{}", encoding="utf-8")
            self.assertFalse(reusable(directory, signature)[0])

    def test_partial_report_keeps_denominator_and_arithmetic_mean(self):
        with tempfile.TemporaryDirectory() as temporary:
            rows = [{"case": "a", "num_cores": 1, "status": "success", "makespan_cycles": 100},
                    {"case": "a", "num_cores": 2, "status": "success", "makespan_cycles": 50},
                    {"case": "b", "num_cores": 1, "status": "success", "makespan_cycles": 90},
                    {"case": "b", "num_cores": 2, "status": "success", "makespan_cycles": 90}]
            report = summarize(rows, ["a", "b", "c"], [2], Path(temporary))
            avg = report["average_speedup"][1]
            self.assertEqual(avg["mean_speedup"], 1.5)
            self.assertEqual(avg["expected_cases"], 3)
            self.assertEqual(avg["status"], "partial")
            self.assertFalse(report["complete"])

    def test_failed_case_has_no_completion_marker(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            root = Path(temporary)
            path = root / "bad.json"
            path.write_text('{"ops": [], "ops": []}', encoding="utf-8")
            rows = run_case(str(path), str(official.REFERENCE / "data" / "config.txt"), str(root), [2], False)
            self.assertTrue(all(r["status"] == "failed" for r in rows))
            self.assertFalse((root / "bad" / "N2" / "complete.json").exists())
            self.assertTrue((root / "bad" / "N2" / "failure.json").exists())


class ArchiveRunnerTests(unittest.TestCase):
    """公共归档与初始化运行入口的实际衔接，使用官方最小图生成测试归档。"""

    def setUp(self):
        from test_baseline_catalog import write_manifest
        from test_problem_1_baseline import minimal_graph
        import problem_1_baseline

        (ROOT / "outputs").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="q1_archive_test_", dir=ROOT / "outputs")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.config = official.REFERENCE / "data/config.txt"
        self.archive = self.root / "baseline"
        for name in ("case_a", "case_b"):
            write_json(self.data / (name + ".json"), minimal_graph())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(problem_1_baseline.main([
                str(self.data), "--workers", "1", "--output-dir", str(self.archive)]), 0)
        write_manifest(self.archive)
        self.graphs = sorted(self.data.glob("case_*.json"))

    def test_archive_loaded_once_without_recomputing_or_copying_singlecore(self):
        from common.baseline import load_baseline_catalog

        output = self.root / "out"
        with patch("q1.run.load_baseline_catalog", wraps=load_baseline_catalog) as loader, \
                patch.object(official, "singlecore", side_effect=AssertionError("must reuse archive")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(run_cases(self.graphs, self.config, output, [2], 1, False,
                                       baseline_archive=self.archive), 0)
            self.assertEqual(loader.call_count, 1)
        report = official.read_json(output / "summary.json")
        for row in report["rows"]:
            self.assertEqual(row["baseline_ref"]["archive"], self.archive.relative_to(ROOT).as_posix())
            self.assertEqual(row["singlecore_makespan_cycles"], 6)
            self.assertEqual(row["speedup"], 6 / row["makespan_cycles"])
        self.assertFalse((output / "case_a/N1").exists())
        saved = official.read_json(output / "case_a/N2/provenance.json")
        source = provenance(self.graphs[0], self.config, saved["baseline"])
        self.assertTrue(reusable(output / "case_a/N2", fingerprint(source, 2))[0])
        source["baseline"]["baseline_ref"]["manifest_sha256"] = "0" * 64
        self.assertFalse(reusable(output / "case_a/N2", fingerprint(source, 2))[0])
        with patch("q1.run.initialize", side_effect=AssertionError("must resume")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(run_cases(self.graphs, self.config, output, [2], 1, True,
                                       baseline_archive=self.archive), 0)

    def test_changed_input_stops_before_initialization_with_diagnostic(self):
        self.graphs[0].write_text("{}", encoding="utf-8")
        output = self.root / "out"
        with patch("q1.run.initialize", side_effect=AssertionError("must not start")), \
                redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "输入 SHA-256"):
            run_cases(self.graphs, self.config, output, [2], 1, False, baseline_archive=self.archive)
        self.assertTrue((output / "baseline_failure.json").exists())
        self.assertFalse(official.read_json(output / "summary.json")["complete"])

    def test_archive_reference_cli_from_other_directory_serial_candidates(self):
        output = self.root / "out"
        process = subprocess.run([
            sys.executable, str(ROOT / "tools/reference_problem1.py"), "batch",
            "--data-dir", str(self.data), "--config", str(self.config), "--cores", "2",
            "--baseline-archive", str(self.archive), "--jobs", "2", "--candidate-jobs", "1",
            "--output-root", str(output)], cwd=self.root, capture_output=True, timeout=60)
        self.assertEqual(process.returncode, 0, (process.stdout, process.stderr))
        report = official.read_json(output / "summary.json")
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["rows"]), 4)
        self.assertTrue(all("baseline_ref" in row for row in report["rows"]))
        self.assertFalse((output / "case_a/N1").exists())


if __name__ == "__main__":
    unittest.main()
