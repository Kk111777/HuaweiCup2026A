"""式 (31)—(34)：就绪 Task 的确定性多核列表分配。"""
from __future__ import annotations

import heapq

from . import official


def assign_cores(graph: dict, plan: dict, durations: dict[int, int],
                 settings: official.Settings) -> tuple[dict, list[dict]]:
    view = official.derive_multicore_plan(graph, plan)
    preds, succs = view["subgraph_preds"], view["subgraph_succs"]
    # 临时方案的编号就是形成顺序；所有数据依赖必须向前。
    rank = {}
    for j in reversed(view["subgraph_ids"]):
        rank[j] = durations[j] + max((rank[u] for u in succs[j]), default=0)
    remaining = {j: len(preds[j]) for j in rank}
    ready = [(-rank[j], j) for j in rank if not remaining[j]]
    heapq.heapify(ready)
    orders = [[] for _ in range(view["num_cores"])]
    finish, core = {}, {}
    records = []
    while ready:
        _, j = heapq.heappop(ready)
        options = []
        for k, order in enumerate(orders):
            available = finish[order[-1]] + settings.same_core_wait if order else 0
            start = max(available, max((finish[i] + settings.cross_core_wait
                                       for i in preds[j] if core[i] != k), default=0))
            options.append((start + durations[j], available, k, start))
        end, available, k, start = min(options)
        orders[k].append(j)
        core[j], finish[j] = k, end
        records.append({"task": j, "core": k, "reference_start": start,
                        "reference_end": end, "core_available": available,
                        "tail_cycles": rank[j]})
        for u in sorted(succs[j]):
            remaining[u] -= 1
            if remaining[u] == 0:
                heapq.heappush(ready, (-rank[u], u))
    if len(finish) != len(durations):
        raise RuntimeError("list allocation did not cover all Tasks")
    result = {"node_to_subgraph": dict(plan["node_to_subgraph"]), "core_schedules": orders}
    official.validate_task_order(official.derive_multicore_plan(graph, result))
    return result, records
