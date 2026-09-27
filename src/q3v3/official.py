"""问题 3 官方编译、评估与独立 CLI 复核；不套用问题 1 的 Task 合法性。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT / "src", ROOT / "official/code"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from q3v3.runtime import run_command
from contest_io import (
    _read_json, format_multicore_result_log, format_scene_b_trace_json,
)
from evaluation_validation import read_evaluation_config, validate_execution
from multicore_cut_evaluate_problem_3 import (
    _build_scene_b_tasks, evaluate_problem_3, read_cache_config, read_scene_b_config,
)

PYTHON = sys._base_executable if os.name == "nt" else sys.executable


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def plan_digest(plan):
    """保留映射插入顺序、子图标签、分核与队列的完整提交指纹。"""
    payload = [list(plan['node_to_subgraph'].items()), plan['core_schedules']]
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        delays = (.02, .05, .15, .3, .6)
        for attempt in range(len(delays) + 1):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
    finally:
        temporary.unlink(missing_ok=True)


def read_settings(config):
    """从官方配置读取全部参数，无本地硬件模型默认值。"""
    config = str(config)
    return {**read_evaluation_config(config), **read_scene_b_config(config),
            **read_cache_config(config)}


def objective(result):
    """Legacy cycles/COPY pair, retained for historical audit compatibility.

    Current search selection is explicitly defined in q3v3.objective.key.
    """
    return result["makespan"], result["data_movement_bytes"]["added_copy_bytes"]


def evaluate(raw, plan, settings):
    """同步官方全局事件模拟，返回完整结果；错误原样上抛。"""
    return evaluate_problem_3(
        raw, plan, bandwidth=settings["bandwidth"], capacity=settings["capacity"],
        cross_core_copy_delay=settings["cross_core_copy_delay_cycles"],
        cache_capacity_bytes=settings["cache_capacity_bytes"],
        cache_bandwidth_bytes_per_cycle=settings["cache_bandwidth_bytes_per_cycle"])


def compile_plan(raw, plan, settings):
    """返回官方五元组 (tasks, cross_links, cross_traffic, traffic, plan_view)。

    每核编译 Step1/2/3 后，验证局部依赖、Pipe FIFO 和跨核 COPY 的全局组合。
    返回对象不保证不可变；调用方缓存时应避免就地修改。
    """
    compiled = _build_scene_b_tasks(
        raw, plan, settings["bandwidth"], settings["capacity"])
    validate_execution(compiled[0], compiled[1])
    return compiled


def metrics(result):
    """实际有操作的核心才算 used；逻辑 COPY 与物理 DDR 字节分别报告。"""
    timelines = result["per_core_timeline"]
    empty_ids = sorted(c["core_id"] for c in timelines if not c["ops"])
    peaks = result["memory_peak_by_core"]
    movement = result["data_movement_bytes"]
    cache = result["cache_stats"]
    hit_bytes, miss_bytes = cache["hit_bytes"], cache["miss_bytes"]
    physical_ddr = movement["scheduled_copy_bytes"] - hit_bytes
    if physical_ddr < 0:
        raise ValueError("Cache hit bytes exceed scheduled COPY bytes")
    # 当前官方的 hit_rate 是字节命中率，不能将它误作请求次数命中率。
    hits = cache["copy_in_hits"]
    misses = cache["copy_in_misses"]
    used = len(timelines) - len(empty_ids)
    return {
        "makespan_cycles": result["makespan"], **movement,
        "physical_ddr_bytes": physical_ddr,
        "num_cores": result["num_cores"], "used_cores": used,
        "empty_cores": len(empty_ids), "empty_core_ids": empty_ids,
        "task_count": result["task_count"], "active_task_count": used,
        "cache_hit_bytes": hit_bytes, "cache_miss_bytes": miss_bytes,
        "cache_hit_rate_bytes": hit_bytes / (hit_bytes + miss_bytes) if hit_bytes + miss_bytes else 0,
        "cache_hit_rate_requests": hits / (hits + misses) if hits + misses else 0,
        "cache_hits": hits, "cache_misses": misses,
        "cache_peak_bytes": max((e.get("used_bytes", 0)
                                 for e in result.get("cache_events", [])), default=0),
        "cache_evicted_entries": sum(len(e.get("evicted_tensor_ids", []))
                                      for e in result.get("cache_events", [])),
        "l1_peak_bytes": max((p["L1"] for p in peaks.values()), default=0),
        "ub_peak_bytes": max((p["UB"] for p in peaks.values()), default=0),
    }


def _validate_replay(result, trace, log, graph_name, plan_name, settings):
    if result.get("problem") != 3:
        raise ValueError("replay did not produce a problem 3 result")
    if result.get("input_graph") != graph_name or result.get("input_plan") != plan_name:
        raise ValueError("replay input identity mismatch")
    for key, expected in (
        ("bandwidth_bytes_per_cycle", settings["bandwidth"]),
        ("capacity_bytes", settings["capacity"]),
        ("cross_core_copy_delay_cycles", settings["cross_core_copy_delay_cycles"]),
        ("cache_capacity_bytes", settings["cache_capacity_bytes"]),
        ("cache_bandwidth_bytes_per_cycle", settings["cache_bandwidth_bytes_per_cycle"]),
    ):
        if result[key] != expected:
            raise ValueError(f"replay configuration mismatch: {key}")
    cores = result["per_core_timeline"]
    if sorted(c["core_id"] for c in cores) != list(range(result["num_cores"])):
        raise ValueError("replay core coverage mismatch")
    operations = [op for core in cores for op in core["ops"]]
    if any(op["start"] < 0 or op["duration"] != op["end"] - op["start"]
           or op["duration"] <= 0 for op in operations):
        raise ValueError("replay operation timing mismatch")
    if max((op["end"] for op in operations), default=0) != result["makespan"]:
        raise ValueError("replay makespan does not match operation timeline")
    # 比较完整事件、核心/Pipe 元数据和子图跨度，不能只比较 otherData.makespan。
    expected_trace = json.loads(format_scene_b_trace_json(graph_name, result))
    if trace != expected_trace:
        raise ValueError("replay trace does not match complete result timeline")
    if log != format_multicore_result_log(graph_name, result):
        raise ValueError("replay official log mismatch")
    metrics(result)


def _output_text(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")


def replay(graph_path, plan_path, config, output_dir, timeout=120):
    """独立官方 CLI 复核，写完整结果/Trace/日志并验证，成功返回 result。

    output_dir 必须尚不存在，防止失败时误读旧产物。失败及超时上抛，
    已建立的目录保留 process.log 和 replay.json；超时由 run_command 清理子进程树。
    """
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("replay timeout must be finite and positive")
    graph_path, plan_path, config = (Path(p).resolve() for p in (graph_path, plan_path, config))
    settings = read_settings(config)
    files = {"graph": graph_path, "plan": plan_path, "config": config}
    hashes = {name: sha256(path) for name, path in files.items()}
    source_hashes = {p.name: sha256(p) for p in sorted((ROOT / "official/code").glob("*.py"))}
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    command = [PYTHON, str(ROOT / "official/code/multicore_cut_evaluate_problem_3.py"),
               str(graph_path), str(plan_path), "--config", str(config),
               "--output", str(directory / "result.json"),
               "--trace-output", str(directory / "trace.json"),
               "--log-output", str(directory / "official.log")]
    meta = {"status": "failed", "command": command, "timeout_seconds": timeout,
            **{f"{name}_sha256": value for name, value in hashes.items()},
            "official_source_sha256": source_hashes}
    started = time.perf_counter()
    try:
        run = run_command(command, timeout=timeout, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        (directory / "process.log").write_text(run.stdout + run.stderr, encoding="utf-8")
        meta["returncode"] = run.returncode
        if run.returncode:
            raise ValueError(f"official Q3 replay failed: {run.stderr[-1500:]}")
        if hashes != {name: sha256(path) for name, path in files.items()}:
            raise ValueError("replay input files changed during evaluation")
        if source_hashes != {p.name: sha256(p) for p in sorted((ROOT / "official/code").glob("*.py"))}:
            raise ValueError("official source changed during replay")
        result = _read_json(directory / "result.json")
        trace = _read_json(directory / "trace.json")
        log = (directory / "official.log").read_text(encoding="utf-8")
        _validate_replay(result, trace, log, graph_path.name, plan_path.name, settings)
        meta.update(status="ok", complete_trace_validated=True,
                    official_log_validated=True, inputs_unchanged=True, **metrics(result))
        return result
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        meta.update(error=str(error), timed_out=isinstance(error, subprocess.TimeoutExpired))
        if isinstance(error, subprocess.TimeoutExpired):
            (directory / "process.log").write_text(
                _output_text(error.stdout) + _output_text(error.stderr) + "\n" + str(error),
                encoding="utf-8")
        elif not (directory / "process.log").exists():
            (directory / "process.log").write_text(str(error) + "\n", encoding="utf-8")
        raise
    finally:
        meta["seconds"] = time.perf_counter() - started
        write_json(directory / "replay.json", meta)


def evaluate_file(graph_path, plan, config, output_dir, timeout=120):
    """搜索候选的独立子进程官方评估；只保存完整 result，不生成 Trace。

    返回完整官方 result；失败/超时上抛并保存 evaluation.json。最终交付仍须 replay。
    output_dir 必须尚不存在。
    """
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("evaluation timeout must be finite and positive")
    graph_path, config = Path(graph_path).resolve(), Path(config).resolve()
    settings = read_settings(config)
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    plan_path = directory / "plan.json"
    write_json(plan_path, plan)
    files = {"graph": graph_path, "plan": plan_path, "config": config}
    hashes = {name: sha256(path) for name, path in files.items()}
    source_hashes = {p.name: sha256(p) for p in sorted((ROOT / "official/code").glob("*.py"))}
    command = [PYTHON, str(Path(__file__).resolve()), "--worker", str(graph_path),
               str(plan_path), "--config", str(config), "--output", str(directory / "result.json")]
    meta = {"status": "failed", "command": command, "timeout_seconds": timeout,
            **{f"{name}_sha256": value for name, value in hashes.items()},
            "official_source_sha256": source_hashes,
            "evaluator": "unmodified official evaluate_problem_3; isolated process; no trace"}
    started = time.perf_counter()
    try:
        run = run_command(command, timeout=timeout, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        (directory / "process.log").write_text(run.stdout + run.stderr, encoding="utf-8")
        meta["returncode"] = run.returncode
        if run.returncode:
            raise ValueError(f"official Q3 evaluation failed: {run.stderr[-1500:]}")
        if hashes != {name: sha256(path) for name, path in files.items()}:
            raise ValueError("evaluation input files changed during evaluation")
        if source_hashes != {p.name: sha256(p) for p in sorted((ROOT / "official/code").glob("*.py"))}:
            raise ValueError("official source changed during evaluation")
        result = _read_json(directory / "result.json")
        # Candidate artifacts do not claim Trace verification. Validate identity and parameters.
        if (result.get("problem") != 3 or result.get("input_graph") != graph_path.name
                or result.get("input_plan") != plan_path.name):
            raise ValueError("candidate evaluation input identity mismatch")
        if (result["bandwidth_bytes_per_cycle"] != settings["bandwidth"]
                or result["capacity_bytes"] != settings["capacity"]
                or result["cache_capacity_bytes"] != settings["cache_capacity_bytes"]
                or result["cache_bandwidth_bytes_per_cycle"] != settings["cache_bandwidth_bytes_per_cycle"]
                or result["cross_core_copy_delay_cycles"] != settings["cross_core_copy_delay_cycles"]):
            raise ValueError("candidate evaluation configuration mismatch")
        meta.update(status="ok", inputs_unchanged=True, **metrics(result))
        return result
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        meta.update(error=str(error), timed_out=isinstance(error, subprocess.TimeoutExpired))
        if isinstance(error, subprocess.TimeoutExpired):
            (directory / "process.log").write_text(
                _output_text(error.stdout) + _output_text(error.stderr) + "\n" + str(error),
                encoding="utf-8")
        elif not (directory / "process.log").exists():
            (directory / "process.log").write_text(str(error) + "\n", encoding="utf-8")
        raise
    finally:
        meta["seconds"] = time.perf_counter() - started
        write_json(directory / "evaluation.json", meta)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("graph", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(_read_json(args.graph), _read_json(args.plan), read_settings(args.config))
    result.update(input_graph=args.graph.name, input_plan=args.plan.name)
    write_json(args.output, result)


if __name__ == "__main__":
    main()
