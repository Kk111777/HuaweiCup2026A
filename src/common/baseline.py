"""只读校验已归档的官方单核基线；不会运行调度或修改历史结果。

当前支持 problem_1_baseline 的场景 A 归档格式。其他问题使用前必须
确认分子口径适用，不能仅凭目录名或 case 编号复用成绩。
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from collections.abc import Iterable

from problem_1_baseline import OFFICIAL_MODULES, load_case_metrics


ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _member(directory: Path, name: str) -> Path:
    """归档成员必须是目录内的规范相对路径（同时兼容 Windows/Linux）。"""
    if (not name or "\\" in name or ":" in name or PurePosixPath(name).is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ValueError(f"非法归档路径：{name!r}")
    path = (directory / name).resolve()
    if not path.is_relative_to(directory):
        raise ValueError(f"归档路径越界：{name}")
    return path


def _fingerprints_by_name(mapping: dict, label: str) -> dict[str, str]:
    """历史路径只用于识别文件名；实际读取路径由调用者提供。"""
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError(f"缺少{label} SHA-256 记录")
    result = {}
    for historical_path, digest in mapping.items():
        name = historical_path.replace("\\", "/").rsplit("/", 1)[-1]
        if (not name or name in result or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError(f"{label}指纹重复或非法：{historical_path}")
        result[name] = digest
    return result


def calculate_speedup(baseline_cycles: int, makespan_cycles: int) -> float:
    """同一输入的官方单核总 cycles / 多核总 cycles；两者须为正整数。"""
    for label, value in (("baseline_cycles", baseline_cycles), ("makespan_cycles", makespan_cycles)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{label} 必须是正整数 cycles")
    return baseline_cycles / makespan_cycles


def load_baseline_catalog(
    archive_dir: str | Path,
    input_paths: Iterable[str | Path],
    config_path: str | Path,
    official_code_dir: str | Path,
    *,
    repo_root: str | Path = ROOT,
) -> dict[str, dict]:
    """校验整个归档，并返回所请求输入对应的基线记录（按文件 stem 索引）。

    归档须位于 repo_root 内；返回的 baseline_ref 可直接写入新实验 JSON。
    配置、官方模块和所有请求输入必须与 run_info.json 的 SHA-256 一致。
    一次批量加载后，将单条记录传给各 worker，避免每个 worker 重验全归档。
    缺失文件抛出 FileNotFoundError；内容、版本或记录不一致抛出 ValueError。
    """
    root, archive = Path(repo_root).resolve(), Path(archive_dir).resolve()
    if not archive.is_relative_to(root):
        raise ValueError("基线归档必须位于 repo_root 内，以生成可移植的仓库相对引用")
    requested = [Path(path).resolve() for path in input_paths]
    if not requested or len({path.stem for path in requested}) != len(requested):
        raise ValueError("请求输入不能为空或包含重复 case")

    manifest_bytes = (archive / "manifest.sha256").read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    hashes = {}
    for line in manifest_bytes.decode("utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise ValueError("manifest.sha256 格式非法")
        digest, name = match.groups()
        if name in hashes:
            raise ValueError(f"归档清单重复：{name}")
        if _sha256(_member(archive, name)) != digest:
            raise ValueError(f"归档 SHA-256 不匹配：{name}")
        hashes[name] = digest
    if not {"run_info.json", "summary.csv"}.issubset(hashes):
        raise ValueError("归档清单缺少 run_info.json 或 summary.csv")

    info = json.loads((archive / "run_info.json").read_text(encoding="utf-8"))
    if (not isinstance(info, dict) or info.get("status") != "complete"
            or type(info.get("failed_cases")) is not int or info["failed_cases"] != 0):
        raise ValueError("基线运行未完整成功")
    with (archive / "summary.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    cases = {}
    expected_members = {"run_info.json", "summary.csv"}
    for row in rows:
        case = row.get("case", "")
        if not case or "/" in case or "\\" in case or case in cases:
            raise ValueError(f"汇总 case 为空、非法或重复：{case!r}")
        _member(archive, case)
        if row.get("status") != "ok" or row.get("error"):
            raise ValueError(f"基线用例失败：{case}")
        try:
            cycles = int(row["makespan_cycles"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"基线周期数非法：{case}") from error
        calculate_speedup(cycles, 1)
        cases[case] = row
        for suffix in ("res.json", "trace.json", "log.txt"):
            expected_members.add(f"{case}/{case}_singlecore_{suffix}")
    if not rows or set(hashes) != expected_members:
        raise ValueError("归档清单与汇总用例所需文件不一致")
    if any(type(info.get(key)) is not int or info[key] != len(rows)
           for key in ("completed_cases", "successful_cases")):
        raise ValueError("运行记录与汇总的成功用例数不一致")

    inputs = _fingerprints_by_name(info.get("input_sha256"), "输入")
    if set(inputs) != {f"{case}.json" for case in cases}:
        raise ValueError("运行记录与汇总的输入用例不一致")
    config = info.get("config")
    if not isinstance(config, dict) or _sha256(Path(config_path)) != config.get("sha256"):
        raise ValueError("配置 SHA-256 与基线不匹配")
    official = _fingerprints_by_name(info.get("official_code_sha256"), "官方程序")
    if set(official) != {f"{module}.py" for module in OFFICIAL_MODULES}:
        raise ValueError("官方程序指纹清单不完整")
    for name, digest in official.items():
        if _sha256(Path(official_code_dir) / name) != digest:
            raise ValueError(f"官方程序 SHA-256 与基线不匹配：{name}")

    # 检查全部归档成绩，避免只取一个 case 时掩盖归档其他行的错误。
    for case, row in cases.items():
        metrics = load_case_metrics(Path(f"{case}.json"), archive / case)
        for key, value in metrics.items():
            if str(value) != row.get(key):
                raise ValueError(f"汇总与官方结果不一致：{case}/{key}")

    catalog = {}
    relative_archive = archive.relative_to(root).as_posix()
    for graph in requested:
        case = graph.stem
        if graph.name not in inputs or case not in cases:
            raise ValueError(f"基线缺少请求用例：{graph.name}")
        if _sha256(graph) != inputs[graph.name]:
            raise ValueError(f"输入 SHA-256 与基线不匹配：{graph.name}")
        result_name = f"{case}/{case}_singlecore_res.json"
        catalog[case] = {
            "case": case,
            "makespan_cycles": int(cases[case]["makespan_cycles"]),
            "baseline_ref": {
                "archive": relative_archive,
                "manifest_sha256": manifest_hash,
                "case": case,
                "result": result_name,
                "result_sha256": hashes[result_name],
            },
        }
    return catalog
