"""用官方最小图产物验证归档读取、版本拒绝与跨目录引用。"""
from contextlib import redirect_stdout
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from common.baseline import calculate_speedup, load_baseline_catalog
import problem_1_baseline as runner
from test_problem_1_baseline import minimal_graph


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(archive):
    members = sorted(p for p in archive.rglob("*")
                     if p.is_file() and p.name not in {"manifest.sha256", ".run.lock"})
    (archive / "manifest.sha256").write_text("".join(
        f"{sha256(p)}  {p.relative_to(archive).as_posix()}\n" for p in members), encoding="utf-8")


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_temp = tempfile.TemporaryDirectory(prefix="baseline_fixture_")
        cls.addClassCleanup(cls.fixture_temp.cleanup)
        cls.fixture = Path(cls.fixture_temp.name)
        source = cls.fixture / "official" / "data"
        source.mkdir(parents=True)
        (source / "case_001.json").write_text(json.dumps(minimal_graph()), encoding="utf-8")
        shutil.copy2(ROOT / "official/data/config.txt", source / "config.txt")
        shutil.copytree(ROOT / "official/code", cls.fixture / "official/code",
                        ignore=shutil.ignore_patterns("__pycache__"))
        archive = cls.fixture / "results" / "baseline"
        with redirect_stdout(io.StringIO()):
            code = runner.main([str(source), "--workers", "1", "--output-dir", str(archive)])
        if code:
            raise RuntimeError("无法生成官方最小图测试产物")
        write_manifest(archive)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="catalog_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repository"
        shutil.copytree(self.fixture, self.root)
        self.archive = self.root / "results/baseline"
        self.graph = self.root / "official/data/case_001.json"
        self.config = self.root / "official/data/config.txt"
        self.code = self.root / "official/code"

    def load(self, inputs=None):
        return load_baseline_catalog(self.archive, [self.graph] if inputs is None else inputs,
                                     self.config, self.code, repo_root=self.root)

    def edit_json(self, name, edit):
        path = self.archive / name
        content = json.loads(path.read_text(encoding="utf-8"))
        edit(content)
        path.write_text(json.dumps(content), encoding="utf-8")
        write_manifest(self.archive)

    def edit_summary(self, **values):
        path = self.archive / "summary.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        rows[0].update(values)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        write_manifest(self.archive)

    def test_reads_real_official_cycles_without_mutating_archive(self):
        before = {p: sha256(p) for p in self.archive.rglob("*") if p.is_file()}
        row = self.load()["case_001"]
        self.assertEqual(row["makespan_cycles"], 6)
        ref = row["baseline_ref"]
        self.assertEqual(ref["archive"], "results/baseline")
        self.assertEqual(ref["manifest_sha256"], sha256(self.archive / "manifest.sha256"))
        self.assertEqual(ref["result_sha256"], sha256(self.archive / ref["result"]))
        self.assertEqual(calculate_speedup(row["makespan_cycles"], 3), 2.0)
        self.assertEqual(before, {p: sha256(p) for p in before})

    def test_reference_survives_repository_move_and_other_working_directory(self):
        original = self.load()
        moved = Path(self.temp.name) / "moved repository"
        shutil.copytree(self.root, moved)
        old_cwd = Path.cwd()
        try:
            os.chdir(self.temp.name)
            actual = load_baseline_catalog(moved / "results/baseline",
                [moved / "official/data/case_001.json"], moved / "official/data/config.txt",
                moved / "official/code", repo_root=moved)
        finally:
            os.chdir(old_cwd)
        self.assertEqual(actual, original)
        ref = actual["case_001"]["baseline_ref"]
        self.assertTrue((moved / ref["archive"] / ref["result"]).is_file())

    def test_missing_case_is_rejected(self):
        second = self.graph.with_name("case_002.json")
        shutil.copy2(self.graph, second)
        with self.assertRaisesRegex(ValueError, "缺少请求用例"):
            self.load([second])

    def test_missing_artifact_is_rejected(self):
        (self.archive / "case_001/case_001_singlecore_trace.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.load()

    def test_corrupt_artifact_is_rejected(self):
        (self.archive / "case_001/case_001_singlecore_log.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "归档 SHA-256"):
            self.load()

    def test_changed_input_is_rejected(self):
        self.graph.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "输入 SHA-256"):
            self.load()

    def test_changed_config_is_rejected(self):
        self.config.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "配置 SHA-256"):
            self.load()

    def test_changed_official_program_is_rejected(self):
        (self.code / "schedule_step3.py").write_text("# changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "官方程序 SHA-256"):
            self.load()

    def test_incomplete_official_fingerprints_are_rejected(self):
        self.edit_json("run_info.json", lambda info: info["official_code_sha256"].pop(
            next(iter(info["official_code_sha256"]))))
        with self.assertRaisesRegex(ValueError, "官方程序指纹清单不完整"):
            self.load()

    def test_failed_run_is_rejected(self):
        self.edit_json("run_info.json", lambda info: info.update(status="failed"))
        with self.assertRaisesRegex(ValueError, "未完整成功"):
            self.load()

    def test_missing_input_fingerprints_are_rejected(self):
        self.edit_json("run_info.json", lambda info: info.pop("input_sha256"))
        with self.assertRaisesRegex(ValueError, "缺少输入 SHA-256"):
            self.load()

    def test_inconsistent_success_count_is_rejected(self):
        self.edit_json("run_info.json", lambda info: info.update(successful_cases=2))
        with self.assertRaisesRegex(ValueError, "成功用例数不一致"):
            self.load()

    def test_duplicate_summary_case_is_rejected(self):
        summary = self.archive / "summary.csv"
        lines = summary.read_text(encoding="utf-8-sig").splitlines(keepends=True)
        summary.write_text("".join(lines + [lines[1]]), encoding="utf-8-sig")
        write_manifest(self.archive)
        with self.assertRaisesRegex(ValueError, "汇总 case 为空、非法或重复"):
            self.load()

    def test_failed_case_is_rejected(self):
        self.edit_summary(status="failed", error="test failure")
        with self.assertRaisesRegex(ValueError, "基线用例失败"):
            self.load()

    def test_result_summary_disagreement_is_rejected(self):
        self.edit_summary(makespan_cycles="7")
        with self.assertRaisesRegex(ValueError, "汇总与官方结果不一致"):
            self.load()

    def test_nonpositive_archive_cycles_are_rejected(self):
        self.edit_summary(makespan_cycles="0")
        with self.assertRaisesRegex(ValueError, "正整数 cycles"):
            self.load()

    def test_nonpositive_official_result_cycles_are_rejected(self):
        self.edit_json("case_001/case_001_singlecore_res.json", lambda result: result.update(makespan=0))
        with self.assertRaisesRegex(ValueError, "makespan 必须是正整数"):
            self.load()

    def test_empty_and_duplicate_requested_cases_are_rejected(self):
        for inputs in ([], [self.graph, self.graph]):
            with self.subTest(inputs=inputs), self.assertRaisesRegex(ValueError, "不能为空或包含重复"):
                self.load(inputs)

    def test_duplicate_manifest_entry_is_rejected(self):
        manifest = self.archive / "manifest.sha256"
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text + text.splitlines()[0] + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "清单重复"):
            self.load()

    def test_manifest_cannot_read_outside_archive(self):
        (self.archive / "manifest.sha256").write_text("0" * 64 + "  ../outside.json\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "非法归档路径"):
            self.load()

    def test_missing_manifest_member_is_rejected_even_with_matching_file_hashes(self):
        manifest = self.archive / "manifest.sha256"
        lines = manifest.read_text(encoding="utf-8").splitlines(keepends=True)
        manifest.write_text("".join(line for line in lines if "trace.json" not in line), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "归档清单与汇总"):
            self.load()


class SpeedupTests(unittest.TestCase):
    def test_speedup_and_slowdown(self):
        self.assertEqual(calculate_speedup(100, 25), 4.0)
        self.assertEqual(calculate_speedup(100, 200), 0.5)

    def test_invalid_cycles_in_either_argument(self):
        for invalid in (0, -1, 1.5, float("nan"), float("inf"), True, "10", None):
            for values in ((invalid, 10), (10, invalid)):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    calculate_speedup(*values)


if __name__ == "__main__":
    unittest.main()
