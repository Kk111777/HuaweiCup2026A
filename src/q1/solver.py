"""从完整候选中择优；不包含第二阶段改进，也不执行文件 I/O。"""
from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from contextlib import redirect_stderr
import io
import multiprocessing
from queue import Empty
from time import perf_counter
from typing import Callable

from . import official
from .graph import GraphIndex
from .partition import BETAS, SHORTLIST_SIZE, partition
from .schedule import assign_cores


@dataclass
class InitializationResult:
    plan: dict
    evaluation: dict
    selected: dict
    candidates: list[dict]
    search: dict


class InitializationError(RuntimeError):
    def __init__(self, message: str, candidates: list[dict], search: dict):
        super().__init__(message)
        self.candidates = candidates
        self.search = search


def repair_and_preanalyze(graph: dict, tasks: list[list[int]], num_cores: int,
                         settings: official.Settings, repairs: list[dict]):
    while True:
        plan = official.make_plan(tasks, num_cores)
        try:
            return tasks, plan, official.preanalyze(graph, plan, settings)
        except official.LOCAL_ERRORS as error:
            entry = {"round": len(repairs), "task_sizes": [len(t) for t in tasks],
                     "error_type": type(error).__name__, "error": str(error)}
            repairs.append(entry)
            if all(len(t) <= 1 for t in tasks):
                raise
            split = []
            for task in tasks:
                if len(task) <= 1:
                    split.append(task)
                else:
                    middle = len(task) // 2
                    split.extend((task[:middle], task[middle:]))
            tasks = split


def _evaluate_candidate(index, num_cores, mode, beta, scopes, progress=None):
    started = perf_counter()
    record = {"mode": mode, "beta": beta, "shortlist_size": SHORTLIST_SIZE,
              "repairs": [], "status": "running"}

    def emit(event):
        if progress is not None:
            progress({"mode": mode, "beta": beta, **event})

    emit({"event": "candidate_start"})
    tasks, growth = partition(index, scopes, num_cores, beta, progress=emit)
    record["growth"] = growth
    plan = evaluation = None
    try:
        emit({"event": "candidate_phase", "phase": "preanalysis", "task_count": len(tasks)})
        tasks, temporary, durations = repair_and_preanalyze(
            index.graph, tasks, num_cores, index.settings, record["repairs"])
        emit({"event": "candidate_phase", "phase": "allocation", "task_count": len(tasks)})
        plan, allocation = assign_cores(index.graph, temporary, durations, index.settings)
        record.update(task_nodes=tasks, local_makespan=durations, allocation=allocation, plan=plan)
        emit({"event": "candidate_phase", "phase": "evaluation"})
        evaluation = official.evaluate(index.graph, plan, index.settings)
        record.update(status="success", makespan=evaluation["makespan"],
                      data_movement_bytes=evaluation["data_movement_bytes"])
    except official.SCHEDULING_ERRORS as error:
        record.update(status="failed", error_type=type(error).__name__, error=str(error))
    record["elapsed_seconds"] = perf_counter() - started
    return record, plan, evaluation


def _candidate_worker_init(index, num_cores, events):
    global _worker_index, _worker_cores, _worker_scopes, _worker_events
    _worker_index, _worker_cores, _worker_events = index, num_cores, events
    _worker_scopes = dict(index.scopes(num_cores))


def _candidate_worker(mode, beta):
    # Windows spawn 下 stderr 不继承父进程的 Python 重定向，故随记录返回诊断。
    diagnostics = io.StringIO()
    with redirect_stderr(diagnostics):
        result = _evaluate_candidate(
            _worker_index, _worker_cores, mode, beta, _worker_scopes[mode],
            progress=_worker_events.put if _worker_events is not None else None)
    if diagnostics.getvalue():
        result[0]["official_diagnostics"] = diagnostics.getvalue()
    return result


def initialize(graph: dict, num_cores: int, settings: official.Settings, *,
               index: GraphIndex | None = None, candidate_jobs: int = 2,
               beta_search: str = "adaptive",
               progress: Callable[[dict], None] | None = None) -> InitializationResult:
    if type(num_cores) is not int or num_cores not in (2, 3, 4, 5):
        raise ValueError("problem 1 initialization requires 2, 3, 4 or 5 cores")
    if type(candidate_jobs) is not int or candidate_jobs < 1:
        raise ValueError("candidate_jobs must be a positive integer")
    if beta_search not in ("fixed", "adaptive", "grid"):
        raise ValueError("beta_search must be fixed, adaptive or grid")
    if index is None:
        index = GraphIndex(graph, settings)
    elif index.graph is not graph or index.settings != settings:
        raise ValueError("GraphIndex must belong to this graph and configuration")
    modes = index.scopes(num_cores)
    specs = [(mode_order, mode, beta) for mode_order, (mode, _) in enumerate(modes) for beta in BETAS]
    records = [None] * len(specs)
    best = None
    mode_best = {}
    mode_search = {mode: {"mode": mode, "evaluated_betas": [], "best_beta": None,
                          "best_makespan": None, "stop_reason": None} for mode, _ in modes}
    search = {"policy": beta_search, "betas": [BETAS[0]] if beta_search == "fixed" else list(BETAS),
              "continuation": {"fixed": "beta_1_only", "adaptive": "strict_makespan_decrease",
                               "grid": "all_betas"}[beta_search],
              "modes": list(mode_search.values())}

    def accept_result(position, result):
        nonlocal best
        mode_order, mode, beta = specs[position]
        record, plan, evaluation = result
        records[position] = record
        state = mode_search[mode]
        state["evaluated_betas"] = sorted([*state["evaluated_betas"], beta])
        if record["status"] == "success":
            key = (evaluation["makespan"], evaluation["data_movement_bytes"]["added_copy_bytes"],
                   mode_order, beta)
            if best is None or key < best[0]:
                best = (key, plan, evaluation, {"mode": mode, "beta": beta, "candidate_index": position})
            if mode not in mode_best or key < mode_best[mode]:
                mode_best[mode] = key
                state.update(best_beta=beta, best_makespan=evaluation["makespan"])
        next_position = None
        if beta_search == "adaptive":
            previous = records[position - 1] if beta != BETAS[0] else None
            previous_time = previous["makespan"] if previous is not None else None
            improved = (evaluation["makespan"] < previous_time
                        if record["status"] == "success" and previous is not None else None)
            if record["status"] != "success":
                reason = "candidate_failed"
            elif previous is not None and not improved:
                reason = "no_makespan_improvement"
            elif beta == BETAS[-1]:
                reason = "max_beta_reached"
            else:
                reason = None
                next_position = position + 1
            state["stop_reason"] = reason
            record["search_decision"] = {
                "previous_makespan": previous_time, "strictly_improved": improved,
                "next_beta": specs[next_position][2] if next_position is not None else None,
                "stop_reason": reason}
        elif beta_search == "fixed":
            state["stop_reason"] = "fixed_beta_complete"
        elif len(state["evaluated_betas"]) == len(BETAS):
            state["stop_reason"] = "grid_exhausted"
        if progress:
            progress({"event": "candidate_end", "mode": mode, "beta": beta,
                      "status": record["status"], "makespan": record.get("makespan"),
                      "elapsed_seconds": record["elapsed_seconds"]})
            if state["stop_reason"] is not None:
                progress({"event": "mode_search_end", **state})
        return next_position

    initial = (list(range(0, len(specs), len(BETAS))) if beta_search != "grid"
               else list(range(len(specs))))
    workers = min(candidate_jobs, len(initial))
    if workers == 1:
        for position in initial:
            while position is not None:
                mode_order, mode, beta = specs[position]
                position = accept_result(position, _evaluate_candidate(
                    index, num_cores, mode, beta, modes[mode_order][1], progress))
    else:
        context = multiprocessing.get_context("spawn")
        events = context.Queue() if progress is not None else None

        def drain_events():
            if events is not None:
                while True:
                    try:
                        progress(events.get_nowait())
                    except Empty:
                        break

        try:
            with ProcessPoolExecutor(max_workers=workers, mp_context=context,
                                     initializer=_candidate_worker_init,
                                     initargs=(index, num_cores, events)) as pool:
                # 自适应先提交两个模式的 beta=1；仅在本模式返回改善后追加下一粒度。
                pending = {pool.submit(_candidate_worker, specs[p][1], specs[p][2]): p for p in initial}
                while pending:
                    done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    drain_events()
                    for future in done:
                        position = accept_result(pending.pop(future), future.result())
                        if position is not None:
                            _, mode, beta = specs[position]
                            pending[pool.submit(_candidate_worker, mode, beta)] = position
            drain_events()
        finally:
            if events is not None:
                events.close()
                events.join_thread()
    if best is not None:
        # 未执行的粒度不占候选序号；异步完成顺序不能影响落盘顺序。
        best[3]["candidate_index"] = sum(r is not None for r in records[:best[3]["candidate_index"]])
    records = [record for record in records if record is not None]
    search["evaluated_candidates"] = len(records)
    search["fallback_used"] = best is None
    if best is None:
        started = perf_counter()
        plan = official.make_plan([[v] for v in index.order], num_cores)
        record = {"mode": "singleton_fallback", "beta": None, "plan": plan}
        try:
            evaluation = official.evaluate(graph, plan, settings)
        except official.SCHEDULING_ERRORS as error:
            record.update(status="failed", error_type=type(error).__name__, error=str(error),
                          elapsed_seconds=perf_counter() - started)
            records.append(record)
            raise InitializationError("all evaluated initialization candidates and fallback failed",
                                      records, search) from error
        record.update(status="success", makespan=evaluation["makespan"],
                      data_movement_bytes=evaluation["data_movement_bytes"],
                      elapsed_seconds=perf_counter() - started)
        best = (None, plan, evaluation, {"mode": "singleton_fallback", "beta": None,
                                         "candidate_index": len(records)})
        records.append(record)
    return InitializationResult(best[1], best[2], best[3], records, search)
