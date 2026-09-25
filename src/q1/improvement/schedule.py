"""关键后继优先、队尾 EFT；全局拓扑派发使新增队列边保持无环。"""
from __future__ import annotations

import heapq
import math
from .partition import boundary_bytes, quotient
from .graph import topological


CHEAP_PROXY_MODE = "cheap_internal_path_pipe_global_ddr_v2"
CHEAP_PROXY_NOTE = (
    "ranking proxy with internal dependency paths, pipe workload and shared DDR; "
    "no spill estimate or official task expansion; not official makespan"
)


from ..plan import validate_plan
from ..allocation import allocate


def _cheap_costs(graph, blocks, settings, owner, reads, writes):
    """一次原图拓扑扫描计算所有块内最长依赖路，避免逐块扫描整图。"""
    bandwidth = settings["bandwidth"]
    if not isinstance(bandwidth, (int, float)) or not math.isfinite(bandwidth) or bandwidth <= 0:
        raise ValueError("bandwidth must be finite and positive")
    local_ends, paths = {}, {i: 0 for i in range(len(blocks))}
    for v in graph.order:
        task = owner[v]
        local_ends[v] = graph.cost[v] + max(
            (local_ends[p] for p in graph.pred[v] if owner[p] == task), default=0)
        paths[task] = max(paths[task], local_ends[v])
    costs = {}
    for i, block in enumerate(blocks):
        loads = graph.workload(block)
        # COPY_IN/OUT 已排除在 graph.ops 外；边界流量已包含原始输入/输出，
        # 此处不能再把原图 COPY 工作量或原始 COPY bytes 叠加一次。
        costs[i] = max(1, paths[i], *loads.values(), (reads[i] + writes[i]) / bandwidth)
    return costs


def cheap_task_costs(graph, blocks, settings):
    """统一廉价 Task 时长：max(块内依赖最长路、各 Pipe 工作量、边界搬运、1)。

    不含 spill 和官方核内调度展开，不能据此声称已预测真实完成时间。
    返回键为当前 blocks 下标；分区变化后必须重新计算。
    """
    owner, _, _, _ = quotient(graph, blocks)
    reads, writes = boundary_bytes(graph, blocks)
    return _cheap_costs(graph, blocks, settings, owner, reads, writes)


def fixed_queue_state(graph, plan, blocks, costs, settings, base=None):
    """共享 EFT 内核；缓存块身份和前驱约束，只传播完成时间发生变化的后继。"""
    blocks = list(map(frozenset, blocks))
    _, pred, succ, _ = quotient(graph, blocks)
    data_pred = {i: set(p) for i, p in pred.items()}
    task_ids = sorted(set(plan["node_to_subgraph"].values()))
    remap = {task: i for i, task in enumerate(task_ids)}
    core, previous = {}, {}
    for c, queue in enumerate(plan["core_schedules"]):
        queue = [remap[task] for task in queue]
        core.update({i: c for i in queue})
        for a, b in zip(queue, queue[1:]):
            previous[b] = a
            pred[b].add(a)
            succ[a].add(b)
    constraints = {}
    for i, block in enumerate(blocks):
        constraints[block] = {blocks[p]: (settings["task_cross_core_wait_cycles"]
                              if core[p] != core[i] else 0) for p in data_pred[i]}
        if i in previous:
            p = blocks[previous[i]]
            constraints[block][p] = max(constraints[block].get(p, 0), settings["task_same_core_wait_cycles"])
    durations = {b: costs[i] for i, b in enumerate(blocks)}
    dirty = {b for b in blocks if base is None or b not in base["ends"] or
             constraints[b] != base["pred"].get(b) or durations[b] != base["durations"].get(b)}
    changed = len(dirty)
    starts = {b: base["starts"][b] for b in blocks if base and b in base["starts"]}
    ends = {b: base["ends"][b] for b in blocks if base and b in base["ends"]}
    propagated = 0
    for i in topological(pred, succ):
        block = blocks[i]
        if block not in dirty:
            continue
        propagated += 1
        start = max((ends[p] + lag for p, lag in constraints[block].items()), default=0)
        end = start + durations[block]
        if end != ends.get(block):
            dirty.update(blocks[j] for j in succ[i])
        starts[block], ends[block] = start, end
    return {"starts": starts, "ends": ends, "pred": constraints, "durations": durations,
            "core": {b: core[i] for i, b in enumerate(blocks)},
            "queued_cycles": max(ends.values(), default=0),
            "changed_tasks": changed, "propagated_tasks": propagated}


def cheap_proxy_details(graph, plan, settings):
    """完整既定方案的同精度评分及显式模型标签，用于候选比较和诊断。

    全部 Task 共享 DDR，因此取 max(固定队列传播结果、总边界 bytes/带宽)。
    这没有模拟带宽事件争用、spill 或 COPY 与计算重叠的实际顺序。
    """
    blocks = validate_plan(graph, plan)
    owner = {v: i for i, block in enumerate(blocks) for v in block}
    reads, writes = boundary_bytes(graph, blocks)
    costs = _cheap_costs(graph, blocks, settings, owner, reads, writes)
    queued = fixed_queue_state(graph, plan, blocks, costs, settings)["queued_cycles"]
    traffic = sum(reads.values()) + sum(writes.values())
    bandwidth_floor = traffic / settings["bandwidth"]
    task_ids = sorted(set(plan["node_to_subgraph"].values()))
    return {
        "mode": CHEAP_PROXY_MODE, "cycles": max(queued, bandwidth_floor),
        "fixed_queue_cycles": queued, "global_bandwidth_cycles": bandwidth_floor,
        "boundary_bytes": traffic,
        "task_cycles": {task: costs[i] for i, task in enumerate(task_ids)},
        "note": CHEAP_PROXY_NOTE,
    }


def cheap_plan_proxy(graph, plan, settings):
    """所有候选统一使用此函数评分；不会读取或混用官方 local profile。"""
    return cheap_proxy_details(graph, plan, settings)["cycles"]


def profile_costs_for_partition(graph, blocks, source_plan, profiles):
    """仅为相同完整划分匹配官方 profile，允许 Task 重编号，不允许 split/merge。

    同一个原图和同一完整分区保证块内容及边界接口不变。source_plan 必须是
    产生 profiles 的方案，且 profile 仅用于该图、该配置下的重排候选构造；
    shortlist 仍应统一调用 cheap_plan_proxy，而非混用这里的官方局部时长。
    """
    quotient(graph, blocks)
    source_blocks = validate_plan(graph, source_plan)
    source_ids = sorted(set(source_plan["node_to_subgraph"].values()))
    by_content = {block: task for block, task in zip(source_blocks, source_ids)}
    if set(map(frozenset, blocks)) != set(by_content):
        raise ValueError("official profiles require the same complete partition")
    normalized = {int(task): value for task, value in profiles.items()}
    if set(normalized) != set(source_ids):
        raise ValueError("official profiles must cover the source partition exactly")
    for value in normalized.values():
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("official profile duration must be finite and positive")
    return {i: normalized[by_content[frozenset(block)]] for i, block in enumerate(blocks)}


def schedule(graph, blocks, cores, settings, durations=None, priority="critical", preferred=None):
    """构造合法队列，返回 (plan, proxy_cycles)。

    durations 保留兼容，仅应来自同一划分的官方 local profile；建议先调用
    profile_costs_for_partition 校验。不同划分的候选比较必须统一以
    cheap_plan_proxy 重打分，不能将此函数的 profile 返回值和廉价值混排。
    """
    if type(cores) is not int or cores < 1:
        raise ValueError("cores must be a positive integer")
    owner, pred, succ, order = quotient(graph, blocks)
    reads, writes = boundary_bytes(graph, blocks)
    costs = _cheap_costs(graph, blocks, settings, owner, reads, writes)
    if durations:
        for i in costs:
            if i in durations:
                value = durations[i]
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError("task duration must be finite and positive")
                costs[i] = value
    queues, ends, _ = allocate(pred, succ, costs, cores,
        settings["task_same_core_wait_cycles"], settings["task_cross_core_wait_cycles"],
        priority=priority, fifo={i: min(graph.rank[v] for v in blocks[i]) for i in order},
        preferred=preferred, tie="affinity")
    plan = {"node_to_subgraph": {str(v): i for i, b in enumerate(blocks) for v in sorted(b)},
            "core_schedules": queues}
    validate_plan(graph, plan, cores)
    bandwidth_floor = (sum(reads.values()) + sum(writes.values())) / settings["bandwidth"]
    return plan, max(max(ends.values(), default=0), bandwidth_floor)
