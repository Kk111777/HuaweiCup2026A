"""批量复现问题一的整图、单 Task、单核基线；评估完全交给官方程序。"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = ROOT / "official"
CONFIG = OFFICIAL / "data" / "config.txt"
ENTRYPOINT = OFFICIAL / "code" / "singlecore_evaluate.py"
CONFIG_SHA256 = "dcd10de54b23f8366428fb24e828812b1da9549e6eae4a3c3f38604fe5ae77b9"
CAPACITY = {"L1": 524288, "UB": 131072}
PIPES = {"PIPE_MTE2", "PIPE_MTE3", "PIPE_M", "PIPE_V"}
OFFICIAL_MODULES = (
    "contest_io", "evaluation_validation", "multicore_cut_evaluate_problem_1",
    "multicore_cut_evaluate_problem_2", "multicore_cut_evaluate_problem_3",
    "schedule_step1", "schedule_step2", "schedule_step3", "singlecore_evaluate",
    "stub_multicore_cut_and_schedule",
)
MOVEMENT_FIELDS = (
    "original_graph_copy_bytes", "scheduled_copy_bytes", "added_copy_bytes",
    "partition_added_copy_bytes", "spill_added_copy_bytes",
)
SUMMARY_FIELDS = (
    "case", "status", "makespan_cycles", *MOVEMENT_FIELDS,
    "l1_peak_bytes", "ub_peak_bytes", "runtime_seconds", "error",
)


def default_workers() -> int:
    """留出桌面使用余量，默认最多同时启动 8 个官方评估进程。"""
    return min(8, max(1, (os.cpu_count() or 1) // 2))


@contextmanager
def output_lock(directory: Path):
    """操作系统文件锁阻止两个批处理同时写同一目录，进程退出后自动释放。"""
    with (directory / ".run.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError(f"输出目录正在被另一批处理使用：{directory}") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def display_path(path: Path) -> str:
    """仓库内路径记为相对路径，仓库外输入保留绝对路径。"""
    path = path.resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)


def select_cases(source: Path, *, full_suite: bool = False) -> list[Path]:
    if source.is_file():
        if source.suffix.lower() != ".json":
            raise ValueError(f"计算图必须是 JSON 文件：{source}")
        cases = [source.resolve()]
    elif source.is_dir():
        cases = sorted(path.resolve() for path in source.glob("case_*.json") if path.is_file())
    else:
        raise FileNotFoundError(f"输入不存在：{source}")
    if not cases:
        raise ValueError(f"未找到 case_*.json：{source}")
    if full_suite:
        expected = {f"case_{index:03d}.json" for index in range(1, 101)}
        actual = {case.name for case in cases}
        if actual != expected:
            raise ValueError(
                f"正式用例须完整覆盖 001–100；缺少 {sorted(expected - actual)}；"
                f"多出 {sorted(actual - expected)}"
            )
    return cases


def verify_materials() -> dict[str, str]:
    files = [OFFICIAL / "code" / f"{name}.py" for name in OFFICIAL_MODULES]
    for path in [*files, CONFIG]:
        if not path.is_file():
            raise FileNotFoundError(f"缺少官方材料：{path}；请按 README 将附件解压到 official/")
    if sha256(CONFIG) != CONFIG_SHA256:
        raise ValueError("官方 config.txt 与附件固定配置不一致，请恢复原文件（包括编码和换行）")
    return {display_path(path): sha256(path) for path in files}


def result_metrics(result: dict) -> dict:
    """检查单核基线不变量，仅提取官方给出的指标，不重新实现调度。"""
    if (result["scene"] != "A" or result["execution_mode"] != "singlecore"
            or result["num_cores"] != 1 or result["input_plan"] is not None):
        raise ValueError("结果不是场景 A 的单核基线")
    if (result["capacity_bytes"] != CAPACITY or result["bandwidth_bytes_per_cycle"] != 60
            or result["task_same_core_wait_cycles"] != 100
            or result["task_cross_core_wait_cycles"] != 1000):
        raise ValueError("结果使用了非官方固定配置")
    if result["task_dependencies"] or result["cross_task_traffic"] != 0:
        raise ValueError("单 Task 基线不能有跨 Task 依赖或流量")
    cores = result["per_core_timeline"]
    if len(cores) != 1 or cores[0]["core_id"] != 0:
        raise ValueError("时间线必须只有 core 0")
    tasks = cores[0]["tasks"]
    makespan = result["makespan"]
    if type(makespan) is not int or makespan <= 0:
        raise ValueError("makespan 必须是正整数周期数")
    if (len(tasks) != 1 or tasks[0] != {
            "task_id": 0, "subgraph_id": 0, "start": 0,
            "end": makespan, "duration": makespan}):
        raise ValueError("必须恰有一个从 0 开始、在 makespan 结束的 Task 0")
    ops = cores[0]["ops"]
    if not ops or max(op["end"] for op in ops) != makespan:
        raise ValueError("操作时间线末端与 makespan 不一致")
    if len({op["op_id"] for op in ops}) != len(ops):
        raise ValueError("时间线包含重复操作")
    for op in ops:
        if (op["task_id"] != 0 or op["pipe"] not in PIPES
                or not 0 <= op["start"] <= op["end"] <= makespan
                or op["duration"] != op["end"] - op["start"]):
            raise ValueError(f"操作时间线非法：{op['op_id']}")
    peaks = result["memory_peak_by_core"]["0"]
    if any(not 0 <= peaks[pos] <= limit for pos, limit in CAPACITY.items()):
        raise ValueError("官方报告的 L1/UB 峰值超过固定容量")
    movement = {key: result["data_movement_bytes"][key] for key in MOVEMENT_FIELDS}
    if any(type(value) is not int or value < 0 for value in movement.values()):
        raise ValueError("搬运量必须是非负整数字节数")
    if (movement["scheduled_copy_bytes"] - movement["original_graph_copy_bytes"]
            != movement["added_copy_bytes"]
            or movement["partition_added_copy_bytes"] + movement["spill_added_copy_bytes"]
            != movement["added_copy_bytes"]):
        raise ValueError("数据搬运量统计不一致")
    return {
        "makespan_cycles": makespan, **movement,
        "l1_peak_bytes": peaks["L1"], "ub_peak_bytes": peaks["UB"],
    }


def load_case_metrics(graph: Path, case_dir: Path) -> dict:
    result_path = case_dir / f"{graph.stem}_singlecore_res.json"
    trace_path = case_dir / f"{graph.stem}_singlecore_trace.json"
    log_path = case_dir / f"{graph.stem}_singlecore_log.txt"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    metrics = result_metrics(result)
    if result["input_graph"] != graph.name:
        raise ValueError("结果对应的输入文件名不一致")
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    events = [event for event in trace["traceEvents"]
              if event.get("ph") == "X" and event.get("cat") in PIPES]
    if (trace["otherData"]["makespan"] != metrics["makespan_cycles"]
            or len(events) != len(result["per_core_timeline"][0]["ops"])):
        raise ValueError("Trace 与结果的耗时或操作数量不一致")
    if f"makespan: {metrics['makespan_cycles']}" not in log_path.read_text(encoding="utf-8").splitlines():
        raise ValueError("精简日志与结果的 makespan 不一致")
    return metrics


def run_case(graph: Path, output_dir: Path) -> dict:
    """隔离调用官方 CLI；失败保留诊断，成功才填写数值指标。"""
    started = time.perf_counter()
    row = {field: "" for field in SUMMARY_FIELDS}
    row.update(case=graph.stem, status="failed")
    case_dir = output_dir / graph.stem
    case_dir.mkdir(exist_ok=True)
    print(f"[START] {graph.stem}", flush=True)
    result_path = case_dir / f"{graph.stem}_singlecore_res.json"
    trace_path = case_dir / f"{graph.stem}_singlecore_trace.json"
    log_path = case_dir / f"{graph.stem}_singlecore_log.txt"
    command = [
        sys.executable, str(ENTRYPOINT), str(graph), "--config", str(CONFIG),
        "--output", str(result_path), "--trace-output", str(trace_path),
        "--log-output", str(log_path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            check=False,
        )
        if completed.returncode:
            diagnostic = completed.stderr.strip() or completed.stdout.strip() or "无错误说明"
            raise ValueError(f"官方程序退出码 {completed.returncode}：{diagnostic}")
        metrics = load_case_metrics(graph, case_dir)
        row.update(status="ok", **metrics)
    except (OSError, ValueError, KeyError, TypeError) as error:
        row["error"] = str(error)
    row["runtime_seconds"] = round(time.perf_counter() - started, 3)
    return row


def write_summary(path: Path, rows: list[dict]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def write_run_info(path: Path, info: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def resume_rows(output_dir: Path, cases: list[Path], current: dict) -> tuple[dict, list[dict]]:
    """只复用来源完全一致、三类产物与汇总均核验通过的成功用例。"""
    info = json.loads((output_dir / "run_info.json").read_text(encoding="utf-8"))
    for key in ("input", "config", "official_code_sha256", "input_sha256"):
        if info[key] != current[key]:
            raise ValueError(f"续跑来源不一致（{key}），请使用新的输出目录")
    graphs = {graph.stem: graph for graph in cases}
    with (output_dir / "summary.csv").open(encoding="utf-8-sig", newline="") as stream:
        recorded = list(csv.DictReader(stream))
    seen, rows = set(), []
    for row in recorded:
        name = row["case"]
        if name not in graphs or name in seen or row["status"] not in {"ok", "failed"}:
            raise ValueError("已有汇总含未知、重复用例或非法状态")
        seen.add(name)
        if row["status"] == "ok":
            metrics = load_case_metrics(graphs[name], output_dir / name)
            if any(str(value) != row[key] for key, value in metrics.items()):
                raise ValueError(f"{name} 的已有汇总与产物不一致，请另选输出目录")
            rows.append(row)
    return info, rows


def run_batch(args, source: Path, cases: list[Path], code_hashes: dict) -> int:
    output_dir = args.output_dir.resolve()
    current = {
        "python_version": platform.python_version(), "python_executable": sys.executable,
        "platform": platform.platform(), "started_at": datetime.now(timezone.utc).isoformat(),
        "arguments": args.command_arguments, "workers": args.workers,
        "input": display_path(source), "output_dir": display_path(output_dir),
        "config": {"path": display_path(CONFIG), "sha256": sha256(CONFIG)},
        "official_code_sha256": code_hashes, "runner_sha256": sha256(Path(__file__)),
        "input_sha256": {display_path(path): sha256(path) for path in cases},
    }
    if args.resume:
        info, rows = resume_rows(output_dir, cases, current)
        if "invocations" not in info:
            # 兼容首次串行复现的记录，保留原解释器与原入口版本的来源。
            info["invocations"] = [{key: info[key] for key in (
                "python_version", "python_executable", "arguments", "runner_sha256",
                "started_at", "finished_at", "runtime_seconds", "status") if key in info}]
            info["invocations"][0]["workers"] = 1
    else:
        if any(path.name != ".run.lock" for path in output_dir.iterdir()):
            raise ValueError(f"输出目录已有内容，请另选空目录：{output_dir}")
        info, rows = dict(current), []
        info["invocations"] = []
    invocation = {key: current[key] for key in (
        "python_version", "python_executable", "arguments", "runner_sha256", "started_at", "workers")}
    invocation["reused_cases"] = len(rows)
    info["invocations"].append(invocation)
    info.update({key: current[key] for key in (
        "python_version", "python_executable", "arguments", "runner_sha256", "workers")})
    info.update(status="running", expected_cases=len(cases))
    info.pop("finished_at", None)
    completed = {row["case"] for row in rows}
    # 大图优先进入进程池，避免尾部只剩一个大例；汇总始终按用例名排序。
    pending = sorted((graph for graph in cases if graph.stem not in completed),
                     key=lambda graph: (-graph.stat().st_size, graph.name))
    run_path = output_dir / "run_info.json"

    def save_progress():
        rows.sort(key=lambda row: row["case"])
        info.update(completed_cases=len(rows),
                    successful_cases=sum(row["status"] == "ok" for row in rows),
                    failed_cases=sum(row["status"] == "failed" for row in rows))
        write_summary(output_dir / "summary.csv", rows)
        write_run_info(run_path, info)

    save_progress()
    print(f"并发数 {args.workers}；已验证并复用 {len(rows)} 例；待运行 {len(pending)} 例", flush=True)
    started = time.perf_counter()
    interrupted = False
    # 线程仅管理独立的 Python 子进程；实际评估在不同进程中使用不同 CPU 核。
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_case, graph, output_dir): graph for graph in pending}
        try:
            for future in as_completed(futures):
                row = future.result()
                rows.append(row)
                save_progress()
                detail = f"{row['makespan_cycles']} cycles" if row["status"] == "ok" else row["error"]
                print(f"[{len(rows)}/{len(cases)}] {row['case']} {row['status']}: {detail} "
                      f"({row['runtime_seconds']} s)", flush=True)
        except KeyboardInterrupt:
            interrupted = True
            for future in futures:
                future.cancel()
            print("\n已取消尚未开始的用例；等待正在运行的评估结束后保存，可用 --resume 续跑。", flush=True)
    if interrupted:
        recorded = {row["case"] for row in rows}
        rows.extend(future.result() for future, graph in futures.items()
                    if not future.cancelled() and graph.stem not in recorded)
    info.update(status="interrupted" if interrupted else "failed" if any(
                    row["status"] == "failed" for row in rows) else "complete",
                finished_at=datetime.now(timezone.utc).isoformat())
    invocation.update(status=info["status"], finished_at=info["finished_at"],
                      runtime_seconds=round(time.perf_counter() - started, 3))
    # 总历时包含两次续跑之间的停顿；各次实际批处理耗时单独记在 invocations。
    info["runtime_seconds"] = round((datetime.now(timezone.utc)
                                     - datetime.fromisoformat(info["started_at"])).total_seconds(), 3)
    save_progress()
    print(f"完成：成功 {info['successful_cases']}，失败 {info['failed_cases']}；"
          f"汇总：{output_dir / 'summary.csv'}", flush=True)
    return 130 if interrupted else int(bool(info["failed_cases"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, help="单个计算图或 case_*.json 目录；默认全部正式用例")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "problem_1_singlecore",
                        help="空的输出目录；默认 outputs/problem_1_singlecore/")
    parser.add_argument("--workers", type=int, default=default_workers(),
                        help="同时运行的官方评估进程数；默认逻辑 CPU 数的一半，最多 8 个")
    parser.add_argument("--resume", action="store_true", help="校验来源与产物后复用成功用例，续跑未完成或失败用例")
    args = parser.parse_args(argv)
    args.command_arguments = sys.argv[1:] if argv is None else argv
    try:
        if args.workers < 1:
            raise ValueError("--workers 必须至少为 1")
        source = (args.input or OFFICIAL / "data").resolve()
        cases = select_cases(source, full_suite=source == (OFFICIAL / "data").resolve())
        code_hashes = verify_materials()
        output_dir = args.output_dir.resolve()
        if not args.resume and output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
            raise ValueError(f"输出目录已有内容，请另选空目录：{output_dir}")
        if args.resume and not output_dir.is_dir():
            raise ValueError(f"没有可续跑的输出目录：{output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)
        with output_lock(output_dir):
            return run_batch(args, source, cases, code_hashes)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"[BASELINE ERROR] {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[BASELINE INTERRUPTED] 已完成用例已保存；本次运行不完整。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
