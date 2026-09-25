"""阶段一归档接入、独立复评、隔离产物、续跑及按用例并行。"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stderr
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import threading
from time import perf_counter
import traceback
import zipfile

from common.baseline import load_baseline_catalog
from .. import official
from ..run import (atomic_text, file_hash, fingerprint, metrics, normalized,
                   save_evaluation, summarize, write_json)
from .search import improve
from .state import SearchOptions, digest, objective


def clean_result(result):
    return normalized({k: v for k, v in result.items() if k not in ("input_graph", "input_plan")})


def read_initial(root, case, cores, graph_path, config_path, *, standalone=False):
    root = Path(root)
    archive = None
    if standalone:
        if not (root.parent / "complete.json").exists() or root.name != "plan.json":
            plan = official.read_json(root)
            return plan, None, {"kind": "standalone", "plan_sha256": file_hash(root), "selected": {}}
        directory = root.parent
    else:
        directory = root / case / f"N{cores}"
        if not directory.exists():
            archive = root / "cases" / f"{case}.zip"
            if not archive.is_file():
                archive = root / f"{case}.zip"
            if not archive.is_file():
                raise FileNotFoundError(f"no initial result for {case}/N{cores}: {root}")
    container = zipfile.ZipFile(archive) if archive else None
    prefix = f"{case}/N{cores}/"

    def read(name):
        if Path(name).name != name or "/" in name or "\\" in name:
            raise ValueError("initial artifact name must be a basename")
        return container.read(prefix+name) if container else (directory/name).read_bytes()

    try:
        done = json.loads(read("complete.json"))
        provenance = json.loads(read("provenance.json"))
        required = {"plan.json", "result.json", "metrics.json", "construction.json", "provenance.json", "result.log", "trace.json"}
        if not required <= done["artifacts_sha256"].keys() or done["num_cores"] != cores:
            raise ValueError("initial completion marker is incomplete or has the wrong core count")
        for name, expected in done["artifacts_sha256"].items():
            if Path(name).name != name or "/" in name or "\\" in name:
                raise ValueError("unsafe initial artifact name")
            sha = hashlib.sha256()
            stream = container.open(prefix+name) if container else (directory/name).open("rb")
            with stream:
                for chunk in iter(lambda: stream.read(1024*1024), b""):
                    sha.update(chunk)
            if sha.hexdigest() != expected:
                raise ValueError(f"initial artifact hash mismatch: {name}")
        if provenance["graph_sha256"] != file_hash(graph_path) or provenance["config_sha256"] != file_hash(config_path):
            raise ValueError("initial graph or configuration differs from the requested input")
        actual = {p.name: file_hash(p) for p in official.CODE.glob("*.py")}
        if provenance["official_sha256"] != actual:
            raise ValueError("initial result used different official code")
        keys = ("schema_version", "algorithm", "graph_sha256", "config_sha256", "solver_sha256",
                "official_sha256", "shared_sha256", "python", "python_implementation", "baseline")
        source = {key: provenance[key] for key in keys if key in provenance}
        if fingerprint(source, cores) != done["fingerprint"] or done["fingerprint"] != provenance["fingerprint"]:
            raise ValueError("initial provenance fingerprint mismatch")
        plan = json.loads(read("plan.json"))
        result = json.loads(read("result.json"))
        row = json.loads(read("metrics.json"))
        selected = json.loads(read("construction.json"))["selected"]
        if len(plan["core_schedules"]) != cores or row["makespan_cycles"] != result["makespan"]:
            raise ValueError("initial plan, metrics and result disagree")
        identity = dict(kind="stage1_archive" if archive else "stage1_directory",
                        producer_fingerprint=done["fingerprint"],
                        plan_sha256=done["artifacts_sha256"]["plan.json"], selected=selected,
                        artifacts_sha256=done["artifacts_sha256"])
        return plan, result, identity
    finally:
        if container:
            container.close()


def source_identity(graph, config, initial, options, baseline):
    sources = list((official.ROOT/"src"/"q1").rglob("*.py"))
    sources += list((official.ROOT/"src"/"common").glob("*.py"))
    sources += [official.ROOT/"src"/"problem_1_baseline.py", official.ROOT/"src"/"solve_problem1.py"]
    return dict(schema_version=1, algorithm="stage2_feedback_joint_search", options=options.record(),
                graph_sha256=file_hash(graph), config_sha256=file_hash(config), initial=initial, baseline=baseline,
                solver_sha256={p.relative_to(official.ROOT).as_posix(): file_hash(p) for p in sorted(sources)},
                official_sha256={p.name: file_hash(p) for p in sorted(official.CODE.glob("*.py"))},
                python=platform.python_version(), python_implementation=platform.python_implementation())


def reusable(directory, signature):
    marker = directory/"complete.json"
    if not marker.exists():
        return False
    done = official.read_json(marker)
    required = {"plan.json", "result.json", "initial_result.json", "optimization.json", "metrics.json",
                "provenance.json", "result.log", "trace.json"}
    if done.get("fingerprint") != signature or not required <= done.get("artifacts_sha256", {}).keys():
        return False
    return all(Path(name).name == name and "/" not in name and "\\" not in name
               and (directory/name).is_file() and file_hash(directory/name) == sha
               for name, sha in done["artifacts_sha256"].items())


def inherit_completed(graphs, config, initial_root, output, cores, options, catalog, manifest):
    """显式迁移已验证版本的完整结果；不改写旧指纹，不继承搜索中间状态。"""
    previous = Path(manifest["previous_output"]).resolve()
    output = Path(output).resolve()
    if previous == output or previous.is_relative_to(output) or output.is_relative_to(previous):
        raise ValueError("inherited and new output roots must be separate")
    if not previous.is_dir() or any(g.stem not in catalog for g in graphs):
        raise ValueError("inheritance requires an existing output root and verified baselines")
    pending = []
    for graph in graphs:
        for n in cores:
            old = previous/graph.stem/f"N{n}"
            if not (old/"complete.json").exists():
                continue
            _, _, initial = read_initial(initial_root, graph.stem, n, graph, config)
            source = normalized(source_identity(graph, config, initial, options, catalog[graph.stem]))
            if source["solver_sha256"] != manifest["current_solver_sha256"]:
                raise ValueError("inheritance manifest does not authorize the current source")
            source["solver_sha256"] = manifest["previous_solver_sha256"]
            signature = digest((source, n))
            provenance = official.read_json(old/"provenance.json")
            done = official.read_json(old/"complete.json")
            row = official.read_json(old/"metrics.json")
            if (any(provenance.get(k) != v for k, v in source.items())
                    or provenance.get("fingerprint") != signature
                    or provenance.get("num_cores") != n or done.get("num_cores") != n
                    or done.get("official_replay_verified") is not True
                    or not reusable(old, signature)
                    or row.get("case") != graph.stem or row.get("num_cores") != n
                    or row.get("status") != "success"):
                raise ValueError(f"inherited result failed identity or artifact validation: {old}")
            target = output/graph.stem/f"N{n}"
            if target.exists() and not reusable(target, signature):
                raise ValueError(f"inheritance destination already contains different results: {target}")
            pending.append((old, target, signature, row))
    # 全部验证后才复制，完整目录一次性就位；源文件及其 provenance 保持不变。
    for old, target, signature, _ in pending:
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".inherit-", dir=target.parent) as tmp:
            staged = Path(tmp)/"result"
            shutil.copytree(old, staged)
            if not reusable(staged, signature):
                raise ValueError(f"inherited result changed while copying: {old}")
            write_json(staged/"inherited.json", dict(source_directory=str(old),
                       producer_fingerprint=signature, inherited_at=datetime.now(timezone.utc).isoformat()))
            staged.rename(target)
    return [{**row, "resumed": True, "inherited": True,
             "producer_fingerprint": signature, "inherited_from": str(old)}
            for old, _, signature, row in pending]


def run_case(graph_name, config_name, initial_root, output_root, cores, options, resume,
             baseline=None, standalone=False):
    graph_path, config_path = Path(graph_name), Path(config_name)
    case = graph_path.stem
    rows = []
    try:
        graph = official.read_json(graph_path)
        settings = official.Settings.read(config_path)
        if baseline is None:
            evaluated = official.singlecore(graph, settings)
            baseline = dict(makespan_cycles=evaluated["makespan"], baseline_ref="explicit_recomputation")
        rows.append(dict(case=case, num_cores=1, status="success", mode="singlecore_reference",
                         makespan_cycles=baseline["makespan_cycles"], baseline_ref=baseline["baseline_ref"]))
    except Exception as error:
        for n in cores:
            directory = Path(output_root)/case/f"N{n}"
            report = dict(case=case, num_cores=n, status="failed", error_type=type(error).__name__,
                          error=str(error), traceback=traceback.format_exc())
            write_json(directory/"attempt_failure.json", report)
            rows.append(report)
        return rows
    for n in cores:
        directory = Path(output_root)/case/f"N{n}"
        try:
            plan, expected, initial = read_initial(initial_root, case, n, graph_path, config_path, standalone=standalone)
            source = source_identity(graph_path, config_path, initial, options, baseline)
            signature = digest((source, n))
            if resume and reusable(directory, signature):
                rows.append({**official.read_json(directory/"metrics.json"), "resumed": True})
                print(f"[{case} N{n}] resume: verified output", flush=True)
                continue
            if directory.exists() and any(directory.iterdir()):
                raise FileExistsError(f"output exists without a reusable signature; choose a new output root: {directory}")
            directory.mkdir(parents=True, exist_ok=True)
            write_json(directory/"provenance.json", {**source, "fingerprint": signature, "num_cores": n,
                       "input_graph": str(graph_path), "config": str(config_path), "platform": platform.platform()})
            started = perf_counter()
            stopped = threading.Event()
            lock = threading.Lock()
            phase = {"name": "initial_evaluation"}
            with (directory/"events.jsonl").open("w", encoding="utf-8") as events, \
                    (directory/"official.log").open("w", encoding="utf-8") as diagnostics, redirect_stderr(diagnostics):
                def progress(event):
                    with lock:
                        if event["event"] == "candidate_phase":
                            phase["name"] = event["phase"]
                        elif event["event"] in ("initial_evaluation", "layer_start", "serialized_plan_reevaluation"):
                            phase["name"] = event["event"]
                        events.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False)+"\n")
                        events.flush()
                    if event["event"] == "accepted":
                        write_json(directory/"checkpoint_plan.json", event["plan"])
                        write_json(directory/"checkpoint.json", {k: v for k, v in event.items() if k != "plan"})
                    if event["event"] in ("accepted", "layer_start", "initial_evaluation", "serialized_plan_reevaluation", "heartbeat"):
                        detail = {k: v for k, v in event.items() if k in ("event", "layer", "version", "delta_cycles", "delta_added_bytes", "phase", "elapsed_seconds")}
                        print(f"[{case} N{n}] {detail}", flush=True)

                def heartbeat():
                    while not stopped.wait(10):
                        progress(dict(event="heartbeat", phase=phase["name"], elapsed_seconds=perf_counter()-started))

                worker = threading.Thread(target=heartbeat, daemon=True)
                worker.start()
                try:
                    solved = improve(graph, plan, settings, options=options, progress=progress, expected_initial=expected)
                    if expected is not None and clean_result(expected) != clean_result(solved.initial_evaluation):
                        raise RuntimeError("initial result differs from archived official result")
                    write_json(directory/"initial_result.json", solved.initial_evaluation)
                    write_json(directory/"plan.json", solved.plan)
                    write_json(directory/"optimization.json", dict(status=solved.status, initial=initial,
                               options=options.record(), history=solved.history, records=solved.records, statistics=solved.statistics))
                    progress({"event": "serialized_plan_reevaluation"})
                    replay_started = perf_counter()
                    result = official.evaluate(graph, official.read_json(directory/"plan.json"), settings)
                    if clean_result(result) != clean_result(solved.evaluation):
                        raise RuntimeError("unmodified official replay differs from search result")
                    if objective(result) > objective(solved.initial_evaluation):
                        raise RuntimeError("final result is worse than the input")
                    replay_seconds = perf_counter()-replay_started
                finally:
                    stopped.set()
                    worker.join()
            result.update(input_graph=graph_path.name, input_plan="plan.json")
            save_evaluation(directory, graph_path, result)
            selected = initial["selected"]
            row = metrics(case, n, result, {"mode": "stage2", "beta": selected.get("beta")}, perf_counter()-started)
            row.update(search_status=solved.status, initial_mode=selected.get("mode"), initial_beta=selected.get("beta"),
                       initial_makespan_cycles=solved.initial_evaluation["makespan"],
                       initial_added_copy_bytes=objective(solved.initial_evaluation)[1],
                       delta_cycles=objective(solved.initial_evaluation)[0]-objective(result)[0],
                       delta_added_bytes=objective(solved.initial_evaluation)[1]-objective(result)[1],
                       improvement_ratio=1-result["makespan"]/solved.initial_evaluation["makespan"],
                       accepted_moves=len(solved.history), official_evaluations=solved.statistics["evaluations"],
                       budget_evaluations=solved.statistics["budget_evaluations"],
                       secondary_hits=solved.statistics["secondary_hits"],
                       secondary_bytes=solved.statistics["secondary_bytes"],
                       secondary_evictions=solved.statistics["secondary_evictions"],
                       pruned_candidates=solved.statistics.get("PRUNED", 0),
                       initial_evaluation_seconds=solved.statistics["initial_evaluation_seconds"],
                       search_seconds=solved.statistics["search_seconds"], final_replay_seconds=replay_seconds,
                       singlecore_makespan_cycles=baseline["makespan_cycles"],
                       speedup=baseline["makespan_cycles"]/result["makespan"], baseline_ref=baseline["baseline_ref"])
            write_json(directory/"metrics.json", row)
            names = ("plan.json", "result.json", "initial_result.json", "optimization.json", "metrics.json",
                     "provenance.json", "result.log", "trace.json", "events.jsonl", "official.log")
            write_json(directory/"complete.json", dict(fingerprint=signature, num_cores=n,
                       search_status=solved.status, official_replay_verified=True,
                       completed_at=datetime.now(timezone.utc).isoformat(),
                       artifacts_sha256={name: file_hash(directory/name) for name in names}))
            rows.append(row)
            print(f"[{case} N{n}] verified: {row['initial_makespan_cycles']} -> {row['makespan_cycles']}; {solved.status}", flush=True)
        except Exception as error:
            report = dict(case=case, num_cores=n, status="failed", error_type=type(error).__name__,
                          error=str(error), traceback=traceback.format_exc())
            write_json(directory/"attempt_failure.json", report)
            rows.append(report)
            print(f"[{case} N{n}] FAILED: {error}", flush=True)
    return rows


def run_cases(graphs, config, initial_root, output, cores, options, jobs=1, resume=False,
              baseline_archive=None, standalone=False, inheritance=None):
    if jobs < 1 or not graphs or len({p.stem for p in graphs}) != len(graphs):
        raise ValueError("positive jobs and distinct nonempty graph list required")
    catalog = {}
    if baseline_archive is not None:
        existing = [g for g in graphs if g.is_file()]
        if existing:
            catalog = load_baseline_catalog(baseline_archive, existing, config, official.CODE, repo_root=official.ROOT)
    cases = [g.stem for g in graphs]
    rows = []
    if inheritance is not None:
        if not resume or standalone:
            raise ValueError("--inherit-completed requires improve-batch --resume")
        rows = inherit_completed(graphs, config, initial_root, output, cores, options, catalog, inheritance)
        print(f"[INHERITED] verified {len(rows)} completed combinations", flush=True)
    inherited = {(r["case"], r["num_cores"]) for r in rows}
    summarize(rows, cases, cores, output)
    args = [(str(g), str(config), str(initial_root), str(output),
             [n for n in cores if (g.stem, n) not in inherited], options, resume,
             catalog.get(g.stem), standalone) for g in graphs]
    if jobs == 1:
        for arguments in args:
            rows.extend(run_case(*arguments))
            summarize(rows, cases, cores, output)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            pending = {pool.submit(run_case, *arguments): arguments for arguments in args}
            for future in as_completed(pending):
                try:
                    rows.extend(future.result())
                except Exception as error:
                    case = Path(pending[future][0]).stem
                    for n in pending[future][4]:
                        report = dict(case=case, num_cores=n, status="failed", error=str(error), traceback=traceback.format_exc())
                        write_json(output/case/f"N{n}"/"attempt_failure.json", report)
                        rows.append(report)
                summarize(rows, cases, cores, output)
    report = summarize(rows, cases, cores, output)
    return 0 if report["complete"] else 1


def add_commands(commands):
    for name in ("improve", "improve-batch"):
        p = commands.add_parser(name, help="第二阶段反馈诊断与联合优化")
        if name == "improve":
            p.add_argument("graph", type=Path)
            p.add_argument("--initial-plan", type=Path, required=True)
        else:
            p.add_argument("--initial-root", type=Path, required=True)
            p.add_argument("--data-dir", type=Path, default=official.REFERENCE/"data")
            p.add_argument("--cases", nargs="+", default=["all"])
            p.add_argument("--cores", nargs="+", type=int, choices=(2, 3, 4, 5), default=[2, 3, 4, 5])
            p.add_argument("--jobs", type=int, default=1)
            p.add_argument("--inherit-completed", type=Path,
                           help="显式继承清单：旧输出目录及已验证的新旧源码哈希；须配合 --resume")
        p.add_argument("--config", type=Path)
        p.add_argument("--output-root", type=Path, default=official.ROOT/"outputs"/"q1_stage2")
        p.add_argument("--search-config", type=Path)
        p.add_argument("--search-seconds", type=float)
        p.add_argument("--region-seconds", type=float)
        p.add_argument("--region-evaluations", type=int)
        p.add_argument("--resume", action="store_true")
        baseline = p.add_mutually_exclusive_group()
        baseline.add_argument("--baseline-archive", type=Path, default=official.ROOT/"results"/"problem_1_singlecore")
        baseline.add_argument("--recompute-baseline", action="store_true")


def dispatch(args):
    settings = official.read_json(args.search_config) if args.search_config else {}
    for key in ("search_seconds", "region_seconds", "region_evaluations"):
        if getattr(args, key) is not None:
            settings[key] = getattr(args, key)
    options = SearchOptions(**settings)
    standalone = args.command == "improve"
    if standalone:
        graphs = [args.graph.resolve()]
        config = args.config or graphs[0].parent/"config.txt"
        initial = args.initial_plan.resolve()
        cores = [len(official.read_json(initial)["core_schedules"])]
        if cores[0] not in (2, 3, 4, 5):
            raise ValueError("initial plan must have 2--5 core lists")
        jobs = 1
    else:
        if "all" in args.cases and args.cases != ["all"]:
            raise ValueError("all cannot be combined with individual cases")
        cases = [f"case_{i:03d}" for i in range(1, 101)] if args.cases == ["all"] else args.cases
        if any(Path(c).name != c or "/" in c or "\\" in c for c in cases):
            raise ValueError("--cases requires basenames")
        graphs = sorted({args.data_dir.resolve()/(c if c.endswith(".json") else c+".json") for c in cases})
        config = args.config or args.data_dir/"config.txt"
        initial, cores, jobs = args.initial_root.resolve(), sorted(set(args.cores)), args.jobs
    return run_cases(graphs, config.resolve(), initial, args.output_root.resolve(), cores, options,
                     jobs, args.resume, None if args.recompute_baseline else args.baseline_archive.resolve(), standalone,
                     official.read_json(args.inherit_completed) if getattr(args, "inherit_completed", None) else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description="第一问第二版：基于严格剪枝的多原则候选方案分层改进策略")
    add_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    try:
        return dispatch(args)
    except (OSError, ValueError, TypeError) as error:
        print(f"[STAGE2 ERROR] {error}", file=sys.stderr)
        return 1
