"""批量入口的契约测试；最小图取自赛题附录 B.7。"""

from contextlib import redirect_stderr, redirect_stdout
import copy
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import problem_1_baseline as baseline


def minimal_graph():
    return {
        "tensors": [
            {"id": 1, "pos": "DDR", "size": 16},
            {"id": 2, "pos": "UB", "size": 16},
            {"id": 3, "pos": "UB", "size": 16},
            {"id": 4, "pos": "DDR", "size": 16},
        ],
        "ops": [
            {"id": 10, "op": "COPY_IN", "pipe": "PIPE_MTE2", "cycles": 1},
            {"id": 11, "op": "ADD", "pipe": "PIPE_V", "cycles": 4},
            {"id": 12, "op": "COPY_OUT", "pipe": "PIPE_MTE3", "cycles": 1},
        ],
        "edges": [{"source": source, "target": target} for source, target in
                  [(1, 10), (10, 2), (2, 11), (11, 3), (3, 12), (12, 4)]],
    }


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="problem_1_baseline_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.output = self.root / "outputs"

    def write_graph(self, name="case_001.json", graph=None):
        path = self.inputs / name
        path.write_text(json.dumps(minimal_graph() if graph is None else graph), encoding="utf-8")
        return path

    def run_main(self, source=None, output=None, *, workers=2, resume=False):
        args = [str(source or self.inputs), "--output-dir", str(output or self.output),
                "--workers", str(workers)]
        if resume:
            args.append("--resume")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return baseline.main(args)

    def read_summary(self):
        with (self.output / "summary.csv").open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_minimal_cli_from_another_working_directory(self):
        graph = self.write_graph("minimal graph.json")
        completed = subprocess.run(
            [sys.executable, str(ROOT / "src" / "problem_1_baseline.py"), str(graph),
             "--output-dir", str(self.output)],
            cwd=self.root, capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"}, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        row, = self.read_summary()
        self.assertEqual(row["case"], "minimal graph")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(int(row["makespan_cycles"]), 6)
        self.assertEqual(int(row["original_graph_copy_bytes"]), 32)
        self.assertEqual(int(row["scheduled_copy_bytes"]), 32)
        self.assertEqual(int(row["added_copy_bytes"]), 0)
        case_dir = self.output / "minimal graph"
        self.assertEqual(len(list(case_dir.iterdir())), 3)
        result = json.loads((case_dir / "minimal graph_singlecore_res.json").read_text(encoding="utf-8"))
        trace = json.loads((case_dir / "minimal graph_singlecore_trace.json").read_text(encoding="utf-8"))
        self.assertEqual(result["makespan"], trace["otherData"]["makespan"])
        self.assertEqual(result["per_core_timeline"][0]["tasks"][0]["start"], 0)
        info = json.loads((self.output / "run_info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["status"], "complete")
        self.assertEqual(info["successful_cases"], 1)
        self.assertEqual(info["input_sha256"][str(graph.resolve())], baseline.sha256(graph))
        self.assertEqual(len(info["official_code_sha256"]), 10)

    def test_directory_sorting_and_unrelated_json_ignored(self):
        self.write_graph("case_002.json")
        self.write_graph("case_001.json")
        self.write_graph("unrelated.json", {})
        self.assertEqual(self.run_main(), 0)
        rows = self.read_summary()
        self.assertEqual([row["case"] for row in rows], ["case_001", "case_002"])
        self.assertTrue(all(row["makespan_cycles"] == "6" for row in rows))

    def test_empty_and_missing_input_fail_before_output(self):
        self.assertEqual(self.run_main(), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.run_main(self.root / "missing.json"), 1)
        self.assertFalse(self.output.exists())

    def test_full_suite_requires_exact_001_to_100(self):
        self.write_graph()
        with self.assertRaisesRegex(ValueError, "001"):
            baseline.select_cases(self.inputs, full_suite=True)
        for index in range(2, 101):
            self.write_graph(f"case_{index:03d}.json")
        self.assertEqual(len(baseline.select_cases(self.inputs, full_suite=True)), 100)
        self.write_graph("case_101.json")
        with self.assertRaises(ValueError):
            baseline.select_cases(self.inputs, full_suite=True)

    def test_existing_output_is_not_overwritten(self):
        self.write_graph()
        self.output.mkdir()
        marker = self.output / "previous-result.txt"
        marker.write_text("keep me", encoding="utf-8")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(list(self.output.iterdir()), [marker])

    def test_missing_materials_and_changed_config_fail_before_output(self):
        self.write_graph()
        with patch.object(baseline, "OFFICIAL", self.root / "missing-official"):
            self.assertEqual(self.run_main(), 1)
        self.assertFalse(self.output.exists())
        changed = self.root / "config.txt"
        changed.write_bytes(baseline.CONFIG.read_bytes().replace(b"bandwidth 60", b"bandwidth 61"))
        with patch.object(baseline, "CONFIG", changed):
            self.assertEqual(self.run_main(), 1)
        self.assertFalse(self.output.exists())

    def test_invalid_graph_does_not_stop_following_cases(self):
        self.write_graph("case_001.json", {"ops": [], "tensors": [], "edges": [{"source": 1, "target": 2}]})
        self.write_graph("case_002.json")
        self.assertEqual(self.run_main(), 1)
        failed, succeeded = self.read_summary()
        self.assertEqual(failed["status"], "failed")
        self.assertIn("EVALUATION ERROR", failed["error"])
        for field in ("makespan_cycles", *baseline.MOVEMENT_FIELDS, "l1_peak_bytes", "ub_peak_bytes"):
            self.assertEqual(failed[field], "")
        self.assertEqual(succeeded["status"], "ok")
        info = json.loads((self.output / "run_info.json").read_text(encoding="utf-8"))
        self.assertEqual((info["status"], info["completed_cases"], info["failed_cases"]), ("failed", 2, 1))
        self.assertFalse((self.output / "case_001" / "case_001_singlecore_res.json").exists())

    def test_subprocess_start_failure_is_recorded(self):
        self.write_graph()
        with patch.object(baseline.subprocess, "run", side_effect=OSError("cannot launch interpreter")):
            self.assertEqual(self.run_main(), 1)
        row, = self.read_summary()
        self.assertEqual(row["status"], "failed")
        self.assertIn("cannot launch interpreter", row["error"])
        self.assertEqual(row["makespan_cycles"], "")

    def test_missing_success_artifacts_are_not_counted_as_success(self):
        self.write_graph()
        with patch.object(baseline.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(self.run_main(), 1)
        row, = self.read_summary()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["makespan_cycles"], "")

    def test_results_repeat_and_bad_invariants_are_rejected(self):
        graph = self.write_graph()
        self.assertEqual(self.run_main(graph), 0)
        second = self.root / "repeat"
        self.assertEqual(self.run_main(graph, second), 0)
        relative = Path("case_001/case_001_singlecore_res.json")
        self.assertEqual((self.output / relative).read_bytes(), (second / relative).read_bytes())
        result = json.loads((self.output / relative).read_text(encoding="utf-8"))
        bad = copy.deepcopy(result)
        bad["memory_peak_by_core"]["0"]["UB"] = baseline.CAPACITY["UB"] + 1
        with self.assertRaises(ValueError):
            baseline.result_metrics(bad)
        bad = copy.deepcopy(result)
        bad["per_core_timeline"][0]["tasks"][0]["start"] = 100
        with self.assertRaises(ValueError):
            baseline.result_metrics(bad)
        # 最终指标不能误取 Step3 的局部时间；Spill 额外搬运也不能被清零。
        result["step3_by_task"]["0"]["local_makespan"] = 999
        result["data_movement_bytes"].update(
            scheduled_copy_bytes=48, added_copy_bytes=16, spill_added_copy_bytes=16)
        metrics = baseline.result_metrics(result)
        self.assertEqual(metrics["makespan_cycles"], 6)
        self.assertEqual(metrics["spill_added_copy_bytes"], 16)

    def test_official_files_match_original_archive_when_available(self):
        archives = list(ROOT.glob("*.zip"))
        if len(archives) != 1:
            self.skipTest("本地没有唯一的原始附件压缩包")
        with zipfile.ZipFile(archives[0]) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or entry.filename.startswith("data/case_"):
                    continue
                with self.subTest(file=entry.filename):
                    self.assertEqual((baseline.OFFICIAL / entry.filename).read_bytes(), archive.read(entry))

    def test_cpu_defaults_and_invalid_worker_count(self):
        for cpus, expected in [(1, 1), (8, 4), (24, 8), (None, 1)]:
            with self.subTest(cpus=cpus), patch.object(baseline.os, "cpu_count", return_value=cpus):
                self.assertEqual(baseline.default_workers(), expected)
        self.write_graph()
        self.assertEqual(self.run_main(workers=0), 1)
        self.assertFalse(self.output.exists())

    def test_parallel_execution_matches_serial_artifacts(self):
        self.write_graph("case_001.json")
        self.write_graph("case_002.json")
        barrier = threading.Barrier(2, timeout=5)
        original = baseline.run_case

        def concurrent_case(*args):
            # 两个 worker 都进入后才放行；实际官方评估仍然执行。
            barrier.wait()
            return original(*args)

        with patch.object(baseline, "run_case", side_effect=concurrent_case):
            self.assertEqual(self.run_main(workers=2), 0)
        serial = self.root / "serial"
        self.assertEqual(self.run_main(output=serial, workers=1), 0)
        for path in self.output.glob("case_*/*"):
            self.assertEqual(path.read_bytes(), (serial / path.relative_to(self.output)).read_bytes())
        self.assertEqual([row["case"] for row in self.read_summary()], ["case_001", "case_002"])

    def test_resume_reuses_successes_and_retries_only_failed_case(self):
        self.write_graph("case_001.json")
        self.write_graph("case_002.json")
        original = baseline.run_case

        def first_run(graph, output_dir):
            if graph.stem == "case_002":
                return {"case": graph.stem, "status": "failed", "error": "temporary failure",
                        "runtime_seconds": 0}
            return original(graph, output_dir)

        with patch.object(baseline, "run_case", side_effect=first_run):
            self.assertEqual(self.run_main(), 1)
        saved = self.output / "case_001" / "case_001_singlecore_res.json"
        timestamp = saved.stat().st_mtime_ns
        with patch.object(baseline, "run_case", wraps=original) as run:
            self.assertEqual(self.run_main(resume=True), 0)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0].stem, "case_002")
        self.assertEqual(saved.stat().st_mtime_ns, timestamp)
        self.assertTrue(all(row["status"] == "ok" for row in self.read_summary()))
        info = json.loads((self.output / "run_info.json").read_text(encoding="utf-8"))
        self.assertEqual(len(info["invocations"]), 2)
        self.assertEqual(info["invocations"][-1]["reused_cases"], 1)

    def test_resume_rejects_changed_input_and_damaged_artifact(self):
        graph = self.write_graph()
        self.assertEqual(self.run_main(), 0)
        original_bytes = graph.read_bytes()
        graph.write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_main(resume=True), 1)
        graph.write_bytes(original_bytes)
        trace = self.output / "case_001" / "case_001_singlecore_trace.json"
        trace.write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_main(resume=True), 1)

    def test_output_lock_rejects_a_second_writer(self):
        self.write_graph()
        self.assertEqual(self.run_main(), 0)
        with baseline.output_lock(self.output):
            self.assertEqual(self.run_main(resume=True), 1)
        self.assertEqual(self.run_main(resume=True), 0)

    def test_interruption_saves_finished_work_and_can_resume(self):
        self.write_graph("case_001.json")
        self.write_graph("case_002.json")
        with patch.object(baseline, "as_completed", side_effect=KeyboardInterrupt):
            self.assertEqual(self.run_main(), 130)
        info = json.loads((self.output / "run_info.json").read_text(encoding="utf-8"))
        self.assertEqual(info["status"], "interrupted")
        self.assertEqual(self.run_main(resume=True), 0)
        self.assertEqual(len(self.read_summary()), 2)


if __name__ == "__main__":
    unittest.main()
