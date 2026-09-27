"""问题3薄适配器的真实官方微型图与独立CLI核验。"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from q3v3 import official
from multicore_cut_evaluate_problem_1 import evaluate_scene_a, read_scene_a_config


def graph_of(edges=(), costs=(100, 100), pipes=None, size=16):
    pipes = pipes or ["PIPE_V"] * len(costs)
    return {
        "ops": [{"id": i, "op": "ADD", "pipe": pipes[i], "cycles": cost}
                for i, cost in enumerate(costs)],
        "tensors": [{"id": 1000 + i, "pos": "UB", "size": size}
                    for i in range(len(costs))],
        "edges": [{"source": i, "target": 1000 + i} for i in range(len(costs))]
                 + [{"source": 1000 + a, "target": b} for a, b in edges],
    }


def opposite_queue_example(same_pipe=False):
    pipes = ["PIPE_V"] * 4 if same_pipe else ["PIPE_V", "PIPE_M"] * 2
    raw = graph_of([(0, 1), (2, 3)], costs=(10,) * 4, pipes=pipes)
    raw["tensors"] = [t for t in raw["tensors"] if t["id"] in {1000, 1002}]
    raw["edges"] = [e for e in raw["edges"] if e["target"] not in {1001, 1003}]
    plan = {"node_to_subgraph": {str(i): i for i in range(4)},
            "core_schedules": [[1, 2], [3, 0]]}
    return raw, plan


class OfficialQ3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = ROOT / "official/data/config.txt"
        cls.settings = official.read_settings(cls.config)
        cls.source_hashes = {str(p): official.sha256(p)
                             for p in [*sorted((ROOT / "official/code").glob("*.py")), cls.config]}

    @classmethod
    def tearDownClass(cls):
        actual = {p: official.sha256(p) for p in cls.source_hashes}
        if actual != cls.source_hashes:
            raise AssertionError("official source or configuration changed during adapter tests")

    def test_settings_are_loaded_from_official_config_and_missing_fails(self):
        self.assertEqual(set(self.settings), {"capacity", "bandwidth", "cross_core_copy_delay_cycles",
                         "cache_capacity_bytes", "cache_bandwidth_bytes_per_cycle"})
        self.assertEqual(self.settings["cross_core_copy_delay_cycles"], 500)
        self.assertEqual(self.settings["cache_capacity_bytes"], 1048576)
        with self.assertRaises(ValueError):
            official.read_settings(ROOT / "does-not-exist-q3-config.txt")

    def test_same_core_subgraphs_share_one_task_without_q1_barrier(self):
        raw = graph_of([(0, 1)])
        raw["tensors"] = raw["tensors"][:1]
        raw["edges"] = [e for e in raw["edges"] if e["target"] != 1001]
        split = {"node_to_subgraph": {"0": 0, "1": 1}, "core_schedules": [[0, 1], []]}
        merged = {"node_to_subgraph": {"0": 0, "1": 0}, "core_schedules": [[0], []]}
        a = official.evaluate(raw, split, self.settings)
        b = official.evaluate(raw, merged, self.settings)
        self.assertEqual(official.objective(a), official.objective(b))
        self.assertEqual(a["data_movement_bytes"]["partition_added_copy_bytes"], 0)
        self.assertEqual(len(a["per_core_timeline"][0]["tasks"]), 1)

    def test_q3_accepts_operation_pipeline_rejected_by_q1_task_order(self):
        raw, plan = opposite_queue_example()
        scene_a = read_scene_a_config(str(self.config))
        with self.assertRaisesRegex(ValueError, "task schedule.*cycle"):
            evaluate_scene_a(raw, plan, bandwidth=self.settings["bandwidth"],
                             capacity=self.settings["capacity"],
                             cross_core_wait=scene_a["task_cross_core_wait_cycles"],
                             same_core_wait=scene_a["task_same_core_wait_cycles"])
        tasks, links, traffic, movement, view = official.compile_plan(raw, plan, self.settings)
        result = official.evaluate(raw, plan, self.settings)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(len(links), 2)
        self.assertGreater(traffic, 0)
        self.assertEqual(movement, result["data_movement_bytes"])
        self.assertEqual(view["num_cores"], 2)
        self.assertEqual(result["makespan"], 524)

    def test_compile_checks_real_global_fifo_cycle(self):
        raw, plan = opposite_queue_example(same_pipe=True)
        with self.assertRaisesRegex(ValueError, "global execution.*cycle"):
            official.compile_plan(raw, plan, self.settings)

    def test_used_cores_uses_ops_instead_of_placeholder_tasks(self):
        raw = graph_of(costs=(10,))
        plan = {"node_to_subgraph": {"0": 0}, "core_schedules": [[0], [], []]}
        result = official.evaluate(raw, plan, self.settings)
        # Q3 publishes one Task record even for an empty core.
        self.assertTrue(result["per_core_timeline"][1]["tasks"])
        measured = official.metrics(result)
        self.assertEqual(measured["used_cores"], 1)
        self.assertEqual(measured["empty_cores"], 2)
        self.assertEqual(measured["empty_core_ids"], [1, 2])
        self.assertEqual(measured["active_task_count"], 1)
        self.assertEqual(measured["task_count"], 3)

    def test_cache_byte_rate_differs_from_request_rate_and_ddr_bytes(self):
        raw = {"ops": [{"id": i, "op": "ADD", "pipe": "PIPE_V", "cycles": 100}
                       for i in range(3)],
               "tensors": [{"id": 999, "pos": "UB", "size": 120000},
                           {"id": 1000, "pos": "UB", "size": 60000}],
               "edges": [{"source": 999, "target": 2},
                         {"source": 1000, "target": 0}, {"source": 1000, "target": 1}]}
        plan = {"node_to_subgraph": {str(i): i for i in range(3)}, "core_schedules": [[0], [2, 1]]}
        result = official.evaluate(raw, plan, self.settings)
        measured = official.metrics(result)
        self.assertEqual(measured["cache_hit_bytes"], 60000)
        self.assertEqual(measured["cache_miss_bytes"], 180000)
        self.assertEqual(measured["cache_hit_rate_bytes"], .25)
        self.assertAlmostEqual(measured["cache_hit_rate_requests"], 1 / 3)
        self.assertEqual(measured["physical_ddr_bytes"], 180000)
        self.assertEqual(measured["scheduled_copy_bytes"], 240000)

    def test_spill_metric_is_not_confused_with_memory_peak(self):
        raw = graph_of([(0, 3), (1, 4), (2, 5)], costs=(100,) * 6)
        for tensor in raw["tensors"][:3]:
            tensor["size"] = 49152
        raw["edges"] += [{"source": i, "target": i + 1} for i in range(5)]
        plan = {"node_to_subgraph": {str(i): 0 for i in range(6)}, "core_schedules": [[0]]}
        measured = official.metrics(official.evaluate(raw, plan, self.settings))
        self.assertGreater(measured["spill_added_copy_bytes"], 0)
        self.assertLessEqual(measured["ub_peak_bytes"], self.settings["capacity"]["UB"])

    def test_candidate_worker_and_complete_official_cli_agree(self):
        raw, plan = opposite_queue_example()
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            graph = directory / "micro.json"
            official.write_json(graph, raw)
            candidate_dir, replay_dir = directory / "candidate", directory / "replay"
            candidate = official.evaluate_file(graph, plan, self.config, candidate_dir, timeout=20)
            result = official.replay(graph, candidate_dir / "plan.json", self.config, replay_dir, timeout=20)
            self.assertEqual(candidate, result)
            self.assertEqual(official.objective(candidate), official.objective(official.evaluate(raw, plan, self.settings)))
            self.assertFalse((candidate_dir / "trace.json").exists())
            meta = json.loads((replay_dir / "replay.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["status"], "ok")
            self.assertTrue(meta["complete_trace_validated"])
            self.assertTrue(meta["official_log_validated"])
            self.assertEqual(meta["graph_sha256"], official.sha256(graph))
            self.assertTrue((replay_dir / "official.log").is_file())
            trace = json.loads((replay_dir / "trace.json").read_text(encoding="utf-8"))
            log = (replay_dir / "official.log").read_text(encoding="utf-8")
            # Trace makespan remains correct, but one actual op event is missing.
            bad_trace = copy.deepcopy(trace)
            index = next(i for i, e in enumerate(bad_trace["traceEvents"])
                         if e["ph"] == "X" and e["cat"].startswith("PIPE_"))
            bad_trace["traceEvents"].pop(index)
            with self.assertRaisesRegex(ValueError, "complete result timeline"):
                official._validate_replay(result, bad_trace, log, graph.name, "plan.json", self.settings)
            with self.assertRaisesRegex(ValueError, "official log mismatch"):
                official._validate_replay(result, trace, log + "tampered\n", graph.name, "plan.json", self.settings)

    def test_invalid_candidate_records_failure_and_is_not_accepted(self):
        raw, plan = opposite_queue_example(same_pipe=True)
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            graph = directory / "micro.json"
            official.write_json(graph, raw)
            with self.assertRaisesRegex(ValueError, "official Q3 evaluation failed"):
                official.evaluate_file(graph, plan, self.config, directory / "candidate", timeout=20)
            meta = json.loads((directory / "candidate/evaluation.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["status"], "failed")
            self.assertFalse((directory / "candidate/result.json").exists())
            self.assertIn("global execution", (directory / "candidate/process.log").read_text(encoding="utf-8"))

    def test_timeout_raises_and_preserves_failure_metadata(self):
        raw, plan = opposite_queue_example()
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            graph, plan_path = directory / "micro.json", directory / "plan.json"
            official.write_json(graph, raw)
            official.write_json(plan_path, plan)
            for label, call in (
                ("candidate", lambda: official.evaluate_file(graph, plan, self.config, directory / "candidate")),
                ("replay", lambda: official.replay(graph, plan_path, self.config, directory / "replay")),
            ):
                with patch("q3v3.official.run_command", side_effect=subprocess.TimeoutExpired("test", .01, output=b"partial")):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        call()
                record = "evaluation.json" if label == "candidate" else "replay.json"
                meta = json.loads((directory / label / record).read_text(encoding="utf-8"))
                self.assertEqual(meta["status"], "failed")
                self.assertTrue(meta["timed_out"])
                self.assertIn("partial", (directory / label / "process.log").read_text(encoding="utf-8"))

    def test_plan_digest_includes_core_assignment_and_order(self):
        raw, plan = opposite_queue_example()
        reassigned = copy.deepcopy(plan)
        reassigned["core_schedules"].reverse()
        reordered = copy.deepcopy(plan)
        reordered["core_schedules"][0].reverse()
        self.assertNotEqual(official.plan_digest(plan), official.plan_digest(reassigned))
        self.assertNotEqual(official.plan_digest(plan), official.plan_digest(reordered))


if __name__ == "__main__":
    unittest.main()
