"""产物、来源指纹、续跑及批量统计；不改变求解器决策。"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stderr
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import traceback
from time import perf_counter

from common.baseline import calculate_speedup, load_baseline_catalog

from . import official
from .graph import GraphIndex
from .partition import BETAS, SHORTLIST_SIZE
from .solver import initialize, InitializationError


def normalized(value):
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def provenance(graph_path: Path, config_path: Path, baseline: dict | None = None, *,
               beta_search: str = "adaptive") -> dict:
    source = {
        "schema_version": 1,
        "algorithm": {"name": "ready_growth_list_allocation", "betas": list(BETAS),
                      "shortlist_size": SHORTLIST_SIZE, "accept_zero_gain": True,
                      "modes": ["global", "components"], "beta_search": beta_search,
                      "adaptive_rule": "per_mode_strict_makespan_decrease",
                      "adaptive_failure": "stop_mode_keep_best"},
        "graph_sha256": file_hash(graph_path), "config_sha256": file_hash(config_path),
        "solver_sha256": {p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
        "official_sha256": {p.name: file_hash(p) for p in sorted(official.CODE.glob("*.py"))},
        "shared_sha256": {name: file_hash(official.ROOT / name) for name in (
            "src/common/baseline.py", "src/problem_1_baseline.py", "src/solve_problem1.py")},
        "python": platform.python_version(), "python_implementation": platform.python_implementation(),
    }
    if baseline is not None:
        source["baseline"] = baseline
    return source


def fingerprint(source: dict, cores: int) -> str:
    value = {"source": source, "num_cores": cores}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def reusable(directory: Path, expected: str) -> tuple[bool, str]:
    marker = directory / "complete.json"
    if not marker.is_file():
        return False, "no completion marker"
    try:
        done = official.read_json(marker)
        if done.get("fingerprint") != expected:
            return False, "input/config/code/parameters/runtime fingerprint changed"
        artifacts = done["artifacts_sha256"]
        required = {"result.json", "metrics.json", "provenance.json", "result.log", "trace.json"}
        if done["num_cores"] != 1:
            required.update(("plan.json", "construction.json"))
        if not required <= artifacts.keys():
            return False, "incomplete artifact manifest"
        for name, digest in artifacts.items():
            if Path(name).name != name or file_hash(directory / name) != digest:
                return False, f"artifact missing or changed: {name}"
    except (OSError, ValueError, KeyError, TypeError) as error:
        return False, f"cannot verify completion: {error}"
    return True, "verified fingerprint and artifact hashes"


def metrics(case: str, cores: int, result: dict, selected: dict, elapsed: float) -> dict:
    return {
        "case": case, "num_cores": cores, "status": "success",
        "mode": selected["mode"], "beta": selected.get("beta"),
        "makespan_cycles": result["makespan"], **result["data_movement_bytes"],
        "task_count": sum(len(c["tasks"]) for c in result["per_core_timeline"]),
        "memory_peak_L1_bytes": max((p["L1"] for p in result["memory_peak_by_core"].values()), default=0),
        "memory_peak_UB_bytes": max((p["UB"] for p in result["memory_peak_by_core"].values()), default=0),
        "elapsed_seconds": elapsed,
    }


def save_evaluation(directory: Path, graph_path: Path, result: dict) -> None:
    write_json(directory / "result.json", result)
    atomic_text(directory / "result.log", official.format_multicore_result_log(graph_path.name, result))
    atomic_text(directory / "trace.json", official.format_scene_a_trace_json(graph_path.name, result) + "\n")


def mark_complete(directory: Path, signature: str, cores: int) -> None:
    names = ["result.json", "metrics.json", "provenance.json", "result.log", "trace.json"]
    if cores != 1:
        names.extend(("plan.json", "construction.json"))
    write_json(directory / "complete.json", {
        "fingerprint": signature, "num_cores": cores,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "artifacts_sha256": {name: file_hash(directory / name) for name in names},
    })


def failure(directory: Path, case: str, cores: int, error: Exception) -> dict:
    # 旧结果即使仍在目录中，也没有有效完成标记，不能作为成功结果续跑。
    (directory / "complete.json").unlink(missing_ok=True)
    report = {"case": case, "num_cores": cores, "status": "failed",
              "error_type": type(error).__name__, "error": str(error),
              "traceback": traceback.format_exc()}
    if isinstance(error, InitializationError):
        write_json(directory / "construction.json", {"candidates": error.candidates,
                                                     "search": error.search})
    write_json(directory / "failure.json", report)
    print(f"[{case} N{cores}] FAILED: {type(error).__name__}: {error}", flush=True)
    return {k: v for k, v in report.items() if k != "traceback"}


def run_case(graph_file: str, config_file: str, output: str, cores: list[int], resume: bool,
             candidate_jobs: int = 1, baseline: dict | None = None, *,
             beta_search: str = "adaptive") -> list[dict]:
    graph_path, config_path, output_root = Path(graph_file), Path(config_file), Path(output)
    case = graph_path.stem
    case_dir = output_root / case
    rows = []
    requested = [1, *cores] if baseline is None else cores
    if baseline is not None:
        # 该记录由父进程统一通过公共归档校验取得；不复制历史官方产物。
        rows.append({"case": case, "num_cores": 1, "status": "success",
                     "mode": "archived_singlecore", "beta": None,
                     "makespan_cycles": baseline["makespan_cycles"],
                     "baseline_ref": baseline["baseline_ref"], "elapsed_seconds": 0.0})
    try:
        settings = official.Settings.read(config_path)
        graph = official.read_json(graph_path)
        source = provenance(graph_path, config_path, baseline, beta_search=beta_search)
        index = GraphIndex(graph, settings)
    except Exception as error:
        for n in requested:
            rows.append(failure(case_dir / f"N{n}", case, n, error))
        return rows
    for n in requested:
        directory = case_dir / f"N{n}"
        signature = fingerprint(source, n)
        if resume:
            valid, reason = reusable(directory, signature)
            if valid:
                row = official.read_json(directory / "metrics.json")
                row["resumed"] = True
                rows.append(row)
                print(f"[{case} N{n}] resume: {reason}", flush=True)
                continue
            print(f"[{case} N{n}] recompute: {reason}", flush=True)
        started = perf_counter()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "complete.json").unlink(missing_ok=True)
        (directory / "failure.json").unlink(missing_ok=True)
        write_json(directory / "provenance.json", {
            **source, "num_cores": n, "fingerprint": signature,
            "input_graph": str(graph_path), "config": str(config_path),
            "platform": platform.platform(), "candidate_jobs": candidate_jobs,
        })
        try:
            with (directory / "events.jsonl").open("w", encoding="utf-8") as events, \
                    (directory / "official.log").open("w", encoding="utf-8") as diagnostics, \
                    redirect_stderr(diagnostics):
                def progress(event):
                    events.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                                             **event}, ensure_ascii=False) + "\n")
                    events.flush()
                    print(f"[{case} N{n}] " + " ".join(f"{k}={v}" for k, v in event.items()), flush=True)

                if n == 1:
                    progress({"event": "singlecore_start"})
                    result = official.singlecore(graph, settings)
                    selected = {"mode": "singlecore", "beta": None}
                else:
                    solved = initialize(graph, n, settings, index=index, progress=progress,
                                        candidate_jobs=candidate_jobs, beta_search=beta_search)
                    write_json(directory / "construction.json", {
                        "selected": solved.selected, "candidates": solved.candidates,
                        "search": solved.search})
                    write_json(directory / "plan.json", solved.plan)
                    progress({"event": "serialized_plan_reevaluation"})
                    result = official.evaluate(graph, official.read_json(directory / "plan.json"), settings)
                    if normalized(result) != normalized(solved.evaluation):
                        raise RuntimeError("serialized plan reevaluation differs from selected evaluation")
                    selected = solved.selected
                result["input_graph"] = graph_path.name
                result["input_plan"] = "plan.json" if n != 1 else None
                save_evaluation(directory, graph_path, result)
                row = metrics(case, n, result, selected, perf_counter() - started)
                if n != 1:
                    row.update(beta_search=beta_search,
                               evaluated_candidates=solved.search["evaluated_candidates"],
                               fallback_used=solved.search["fallback_used"])
                if baseline is not None:
                    row["baseline_ref"] = baseline["baseline_ref"]
                    row["singlecore_makespan_cycles"] = baseline["makespan_cycles"]
                    row["speedup"] = calculate_speedup(baseline["makespan_cycles"], result["makespan"])
                write_json(directory / "metrics.json", row)
                progress({"event": "verified", "makespan": result["makespan"]})
            mark_complete(directory, signature, n)
            rows.append(row)
        except Exception as error:
            rows.append(failure(directory, case, n, error))
    return rows


def summarize(rows: list[dict], cases: list[str], cores: list[int], output: Path) -> dict:
    ordered = [dict(r) for r in sorted(rows, key=lambda r: (r["case"], r["num_cores"]))]
    baselines = {r["case"]: r["makespan_cycles"] for r in ordered
                 if r["num_cores"] == 1 and r["status"] == "success"}
    baseline_refs = {r["case"]: r["baseline_ref"] for r in ordered
                     if r["num_cores"] == 1 and "baseline_ref" in r}
    for row in ordered:
        baseline = baselines.get(row["case"])
        makespan = row.get("makespan_cycles")
        row["singlecore_makespan_cycles"] = baseline
        row["speedup"] = (calculate_speedup(baseline, makespan)
                          if baseline and makespan and row["status"] == "success" else None)
        if row["case"] in baseline_refs:
            row["baseline_ref"] = baseline_refs[row["case"]]
    averages = []
    for n in [1, *cores]:
        selected = [r for r in ordered if r["num_cores"] == n]
        ratios = [r["speedup"] for r in selected if r["speedup"] is not None]
        successes = sum(r["status"] == "success" for r in selected)
        averages.append({"num_cores": n, "successful_cases": successes,
                         "speedup_cases": len(ratios), "expected_cases": len(cases),
                         "status": "complete" if len(ratios) == len(cases) else "partial",
                         "mean_speedup": sum(ratios) / len(ratios) if ratios else None})
    report = {"scope": "cases and cores requested by this invocation", "cases": cases,
              "cores": [1, *cores], "rows": ordered, "average_speedup": averages,
              "complete": all(a["status"] == "complete" for a in averages)}
    write_json(output / "summary.json", report)
    for name, table in (("summary.csv", ordered), ("average_speedup.csv", averages)):
        fields = list(dict.fromkeys(key for row in table for key in row))
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: json.dumps(v, ensure_ascii=False, sort_keys=True)
                          if isinstance(v, (dict, list)) else v for k, v in row.items()}
                         for row in table)
        atomic_text(output / name, buffer.getvalue())
    return report


def run_cases(graphs: list[Path], config: Path, output: Path,
              cores: list[int], jobs: int, resume: bool, candidate_jobs: int = 1,
              baseline_archive: Path | None = official.ROOT / "results" / "problem_1_singlecore", *,
              beta_search: str = "adaptive") -> int:
    cases = [p.stem for p in graphs]
    if len(cases) != len(set(cases)):
        raise ValueError("case filenames must have distinct stems")
    rows = []
    # 先写部分状态，运行中不会把前一次调用的完整汇总误当成本次已完成。
    summarize(rows, cases, cores, output)
    catalog = {}
    if baseline_archive is not None:
        print(f"[BASELINE] verifying archive: {baseline_archive}", flush=True)
        try:
            catalog = load_baseline_catalog(baseline_archive, graphs, config, official.CODE,
                                            repo_root=official.ROOT)
        except (OSError, ValueError) as error:
            write_json(output / "baseline_failure.json", {
                "error_type": type(error).__name__, "error": str(error),
                "archive": str(baseline_archive)})
            raise
        print(f"[BASELINE] verified; reusing {len(catalog)} singlecore results", flush=True)
    (output / "baseline_failure.json").unlink(missing_ok=True)
    if jobs == 1:
        for graph in graphs:
            rows.extend(run_case(str(graph), str(config), str(output), cores, resume, candidate_jobs,
                                 catalog.get(graph.stem), beta_search=beta_search))
            summarize(rows, cases, cores, output)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(run_case, str(g), str(config), str(output), cores, resume, candidate_jobs,
                                   catalog.get(g.stem), beta_search=beta_search): g
                       for g in graphs}
            for future in as_completed(futures):
                try:
                    rows.extend(future.result())
                except Exception as error:
                    graph = futures[future]
                    for n in [1, *cores]:
                        rows.append(failure(output / graph.stem / f"N{n}", graph.stem, n, error))
                summarize(rows, cases, cores, output)
    report = summarize(rows, cases, cores, output)
    print(f"summary: {output / 'summary.json'}; complete={report['complete']}", flush=True)
    return 0 if report["complete"] else 1
