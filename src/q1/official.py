"""原附件的薄适配层；所有实际调度和搬运统计仍由官方程序计算。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "official"
CODE = REFERENCE / "code"
if str(CODE) not in sys.path:
    sys.path.insert(0, str(CODE))

from contest_io import (  # noqa: E402
    _read_json as read_json, format_multicore_result_log, format_scene_a_trace_json,
)
from evaluation_validation import (  # noqa: E402
    validate_graph, validate_task_order, read_evaluation_config,
)
from stub_multicore_cut_and_schedule import (  # noqa: E402
    EXCLUDED_COPY_TYPES, _build_op_adjacency, _contract_excluded_copy_nodes,
    derive_multicore_plan,
)
from multicore_cut_evaluate_problem_1 import (  # noqa: E402
    _build_scene_a_tasks, evaluate_scene_a, read_scene_a_config, SceneAEvaluationError,
)
from schedule_step2 import Step2SchedulingError  # noqa: E402
from schedule_step3 import Step3SchedulingError  # noqa: E402
from singlecore_evaluate import evaluate_singlecore  # noqa: E402

LOCAL_ERRORS = (Step2SchedulingError, Step3SchedulingError)
SCHEDULING_ERRORS = (*LOCAL_ERRORS, SceneAEvaluationError)


@dataclass(frozen=True)
class Settings:
    capacity: dict[str, int]
    bandwidth: int
    same_core_wait: int
    cross_core_wait: int

    @classmethod
    def read(cls, path: str | Path) -> "Settings":
        config = read_evaluation_config(str(path))
        scene = read_scene_a_config(str(path))
        return cls(config["capacity"], config["bandwidth"],
                   scene["task_same_core_wait_cycles"],
                   scene["task_cross_core_wait_cycles"])


def make_plan(tasks: list[list[int]], num_cores: int) -> dict:
    """临时方案按形成顺序串行；空核心也写入提交格式。"""
    return {
        "node_to_subgraph": {str(v): j for j, nodes in enumerate(tasks) for v in nodes},
        "core_schedules": [list(range(len(tasks)))] + [[] for _ in range(num_cores - 1)],
    }


def preanalyze(graph: dict, plan: dict, settings: Settings) -> dict[int, int]:
    tasks, _, _, _ = _build_scene_a_tasks(
        graph, plan, bandwidth=settings.bandwidth, capacity=settings.capacity)
    return {j: task["step3"]["makespan"] for j, task in tasks.items()}


def evaluate(graph: dict, plan: dict, settings: Settings) -> dict:
    return evaluate_scene_a(
        graph, plan, bandwidth=settings.bandwidth, capacity=settings.capacity,
        same_core_wait=settings.same_core_wait, cross_core_wait=settings.cross_core_wait)


def singlecore(graph: dict, settings: Settings) -> dict:
    return evaluate_singlecore(graph, bandwidth=settings.bandwidth, capacity=settings.capacity,
                               same_core_wait=settings.same_core_wait,
                               cross_core_wait=settings.cross_core_wait)
