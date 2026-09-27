"""Real micrographs exercise Q3 seed, search, isolated evaluation and final replay."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from q3v3 import search
from q3v3.neighborhoods import Candidate, generate_candidates
from q3v3.official import objective, read_settings, sha256, write_json
from q3v3.objective import key
from q3v3.q1_compat import DEFAULT_Q1_SOURCE


def graph_of(edges=(), costs=(101, 211, 307, 419, 503, 617)):
    return {
        "ops": [{"id": i, "op": "ADD", "pipe": "PIPE_V", "cycles": t}
                for i, t in enumerate(costs)],
        "tensors": [{"id": 1000 + i, "pos": "UB", "size": 16} for i in range(len(costs))],
        "edges": [{"source": i, "target": 1000 + i} for i in range(len(costs))]
                 + [{"source": 1000 + a, "target": b} for a, b in edges],
    }


class Q3SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = ROOT / "official/data/config.txt"
        cls.settings = read_settings(cls.config)

    def run_one(self, folder, raw, *, cores=2, warm_plan=None, enable_l2=True, **kwargs):
        graph = folder / "micro.json"
        write_json(graph, raw)
        output = folder / "solved"
        summary, plan = search.solve_one(
            graph, cores, self.config, output, DEFAULT_Q1_SOURCE,
            warm_plan=warm_plan, seconds=3, max_evaluations=3,
            near_bound_fraction=0, evaluation_timeout=20, enable_l2=enable_l2, **kwargs)
        return graph, output, summary, plan

    def check_delivery(self, graph, output, summary, plan):
        saved = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, summary)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["plan_sha256"], sha256(output / "plan.json"))
        self.assertEqual(summary["source_hashes"]["graph"], sha256(graph))
        self.assertEqual(summary["source_hashes"]["config"], sha256(self.config))
        self.assertEqual(plan, json.loads((output / "plan.json").read_text(encoding="utf-8")))
        self.assertLessEqual(summary["makespan_cycles"], summary["seed_makespan"])
        verified = json.loads((output / "verified/result.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["makespan_cycles"], verified["makespan"])
        replay = json.loads((output / "verified/replay.json").read_text(encoding="utf-8"))
        self.assertEqual(replay["status"], "ok")
        self.assertTrue(replay["complete_trace_validated"])
        self.assertTrue(replay["official_log_validated"])
        self.assertFalse(summary["singlecore_score_used"])
        provenance = summary["q1_source_provenance"]
        if provenance is not None:
            self.assertFalse(provenance["baseline_used"])
            self.assertEqual(provenance["q1_scoring_calls"], 0)
            self.assertTrue(provenance["process_isolated"])
        else:
            self.assertEqual(summary['warm_start_mode'], 'full_plan_only')
        self.assertLessEqual(summary["official_calls"], 3)
        return verified

    def test_without_warm_plan_exports_fresh_graph_only_seed_and_completes(self):
        with tempfile.TemporaryDirectory() as name:
            with patch("q3v3.search.export_seeds", wraps=search.export_seeds) as bridge:
                graph, output, summary, plan = self.run_one(Path(name), graph_of())
            bridge.assert_called_once()
            self.check_delivery(graph, output, summary, plan)
            seed = json.loads((output / "q1_seed/q1_seeds.json").read_text(encoding="utf-8"))
            self.assertEqual(seed["plans"][0]["score_source"], "unscored")
            self.assertEqual(seed["provenance"]["input_sha256"], sha256(graph))
            self.assertNotIn("warm_plan", summary["source_hashes"])
            self.assertEqual(summary["official_calls"], 3)
            self.assertEqual(summary["stop_reason"], "official_call_budget")

    def test_same_core_chain_warm_start_and_empty_cores_survive_final_replay(self):
        raw = graph_of([(0, 1), (1, 2)], costs=(100, 100, 100))
        warm = {"node_to_subgraph": {str(i): i for i in range(3)},
                "core_schedules": [[0, 1, 2], [], []]}
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            warm_path = folder / "warm.json"
            write_json(warm_path, warm)
            warm_hash = sha256(warm_path)
            graph, output, summary, plan = self.run_one(folder, raw, cores=3, warm_plan=warm_path)
            verified = self.check_delivery(graph, output, summary, plan)
            self.assertEqual(sha256(warm_path), warm_hash)
            self.assertEqual(summary["source_hashes"]["warm_plan"], warm_hash)
            history = json.loads((output / "history.json").read_text(encoding="utf-8"))
            self.assertEqual(history[0]["name"], "provided_q1_plan")
            self.assertEqual(history[0]["status"], "ok")
            self.assertEqual(history[0]["used_cores"], 1)
            self.assertEqual(history[0]["empty_cores"], 2)
            self.assertEqual(verified["num_cores"], 3)

    def test_official_rejected_candidate_preserves_verified_incumbent(self):
        raw = graph_of([(0, 1), (2, 3)], costs=(100, 100, 100, 100))
        invalid = {"node_to_subgraph": {str(i): i for i in range(4)},
                   "core_schedules": [[1, 2], [3, 0]]}
        candidate = Candidate(invalid, "order", "Exercise real global FIFO rejection")
        with tempfile.TemporaryDirectory() as name:
            with patch("q3v3.search.construction_candidates", return_value=iter(())), \
                 patch("q3v3.neighborhoods.generate_candidates", return_value=[candidate]):
                graph, output, summary, plan = self.run_one(Path(name), raw, enable_global=False, enable_joint=False)
            verified = self.check_delivery(graph, output, summary, plan)
            self.assertGreaterEqual(summary["rejected_calls"], 1)
            history = json.loads((output / "history.json").read_text(encoding="utf-8"))
            failed = [r for r in history if r["status"] == "official_rejected"]
            self.assertIn("global execution", failed[0]["error"])
            good_seeds = [r for r in history if r["family"] == "seed" and r["status"] == "ok"]
            self.assertEqual(objective(verified), min(
                (r["makespan_cycles"], r["added_copy_bytes"]) for r in good_seeds))

    def test_no_l2_disables_cache_candidates_but_keeps_official_q3(self):
        with tempfile.TemporaryDirectory() as name:
            with patch("q3v3.neighborhoods.generate_candidates", wraps=generate_candidates) as generate:
                graph, output, summary, plan = self.run_one(Path(name), graph_of(), enable_l2=False)
            verified = self.check_delivery(graph, output, summary, plan)
            self.assertFalse(summary["enable_l2"])
            self.assertEqual(verified["problem"], 3)
            self.assertEqual(verified["cache_capacity_bytes"], self.settings["cache_capacity_bytes"])
            self.assertTrue(generate.called)
            self.assertTrue(all(call.kwargs["enable_l2"] is False for call in generate.call_args_list))
            history = json.loads((output / "history.json").read_text(encoding="utf-8"))
            self.assertFalse(any(r["family"].startswith("l2_") for r in history))

    def test_cli_single_graph_two_core_counts_produces_complete_report(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            graph, output = folder / "micro_cli.json", folder / "cli"
            write_json(graph, graph_of())
            command = [sys.executable, str(ROOT / "src/solve_q3v3.py"), str(graph),
                       "--cores", "2", "3", "--workers", "1", "--q1-source", str(DEFAULT_Q1_SOURCE),
                       "--config", str(self.config), "--output-dir", str(output),
                       "--seconds", "3", "--max-evaluations", "3", "--near-bound-fraction", "0",
                       "--evaluation-timeout", "20", "--no-l2", "--no-global", "--no-joint"]
            run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=40)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            info = json.loads((output / "run_info.json").read_text(encoding="utf-8"))
            rows = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(info["status"], "complete")
            self.assertEqual(info["completed"], 2)
            self.assertEqual(info["expected"], 2)
            self.assertTrue(info["sources_unchanged"])
            self.assertIn('q1_snapshot',info)
            self.assertTrue(Path(info['q1_source']).is_relative_to(output))
            self.assertFalse(any(p.replace('\\','/').startswith('src/q1/') for p in info['source_hashes']))
            timing=json.loads((output/'timing.json').read_text(encoding='utf-8'))
            self.assertEqual(timing['batch_wall_seconds'],info['elapsed_seconds'])
            self.assertEqual(info["workers"], 1)
            self.assertFalse(info['settings']['enable_global'])
            self.assertFalse(info['settings']['enable_joint'])
            self.assertEqual(info["simulated_cores"], [2, 3])
            from report_q3v3 import report
            audited=report(output)
            self.assertEqual(audited['status'],'passed')
            self.assertEqual(audited['count'],2)
            self.assertEqual(audited['q1_source_versions'],[info['q1_snapshot']['source_sha256']])
            self.assertGreater(audited['case_timing'][0]['total_seconds'],0)
            self.assertEqual([r["num_cores"] for r in rows], [2, 3])
            self.assertTrue(all(r["status"] == "ok" for r in rows))
            self.assertTrue(all(r["official_calls"] <= 3 for r in rows))
            self.assertLessEqual(rows[1]["makespan_cycles"], rows[0]["makespan_cycles"])
            self.assertTrue((output / "REPORT.md").is_file())


if __name__ == "__main__":
    unittest.main()
