"""按计算锚点和当前核数重建整个区域；只生成候选，不替代官方评估。

阶段标签沿每条原始依赖单调不减。阶段内的弱分量互不依赖，因而可以
横向重新打包；所有原始依赖仍由 quotient 逐边恢复并再次检查无环。
"""
from __future__ import annotations

from collections import defaultdict

from .graph import components
from .partition import pack, quotient


def anchor_phases(graph, pipe, minimum_cycles=0):
    """最长路径上的锚点计数；锚点后轻操作一直保留到下个锚点之前。

    使用所有真实前驱取 max，因此残差/跳跃依赖不会被丢掉。第一个
    锚点的编号是 1，纯准备操作编号是 0；编号不是操作 ID 或深度。
    """
    anchors = {v for v in graph.ops
               if graph.ops[v]["pipe"] == pipe and graph.cost[v] >= minimum_cycles}
    phases = {}
    for v in graph.order:
        phases[v] = max((phases[p] for p in graph.pred[v]), default=0) + int(v in anchors)
    return phases, anchors


def _phase_units(graph, phases, span=1):
    bands = defaultdict(set)
    for v in graph.order:
        # Keep any shared preparation stage separate from first compute anchor.
        band = 0 if phases[v] == 0 else 1 + (phases[v] - 1) // span
        bands[band].add(v)
    return [(band, components(nodes, graph.pred, graph.succ))
            for band, nodes in sorted(bands.items())]


def _stable_membership(graph, count):
    """整条原始独立分支只分配一次；各阶段延续同一批成员。

    仅用于原图确有多个弱分量的情况。只有一个弱分量时，不能把它
    假定为可并行分支；调用方使用阶段内部的弱分量进行重新分组。
    """
    groups = pack(graph, graph.components, count)
    return {v: i for i, group in enumerate(groups) for v in group}


def _pack_phases(graph, units_by_phase, count, stable=None, retained=()):
    blocks, block_phases = list(retained), [None] * len(retained)
    for phase, units in units_by_phase:
        if stable is None:
            packed = pack(graph, units, count)
        else:
            groups = defaultdict(set)
            for unit in units:
                # Every within-phase weak component lies in one original
                # weak component. The assertion protects this invariant.
                members = {stable[v] for v in unit}
                if len(members) != 1:
                    raise ValueError("stable unit crosses independent branch groups")
                groups[next(iter(members))].update(unit)
            packed = [frozenset(nodes) for _, nodes in sorted(groups.items())]
        blocks.extend(packed)
        block_phases.extend([phase] * len(packed))
    quotient(graph, blocks)
    return blocks, block_phases


def _partition_key(blocks):
    return frozenset(frozenset(b) for b in blocks)


def fork_phases(graph, fanout):
    """仅在共享生产者之后递增阶段，避免把汇合前每个节点都切碎。"""
    phases = {}
    for v in graph.order:
        phases[v] = max((phases[p] + int(len(graph.succ[p]) >= fanout)
                         for p in graph.pred[v]), default=0)
    return phases


def _fork_candidates(graph, cores, seen):
    maximum = max((len(graph.succ[v]) for v in graph.ops), default=0)
    if maximum < 3:
        return []
    # A maximal fan-out producer is a structural landmark; a second, smaller
    # threshold explores more regions. Neither threshold depends on case IDs.
    thresholds = list(dict.fromkeys((maximum, max(3, (maximum + 1) // 2))))
    candidates = []
    for threshold in thresholds:
        phases = fork_phases(graph, threshold)
        units_by_phase = _phase_units(graph, phases)
        for count in (cores, 2 * cores):
            blocks, block_phases = _pack_phases(graph, units_by_phase, count)
            key = _partition_key(blocks)
            if key in seen:
                continue
            seen.add(key)
            metadata = {
                "family": "shared_preparation", "priority": len(candidates),
                "fanout_threshold": threshold, "cores": cores,
                "bucket_count": count, "grouping": "balanced",
                "phase_count": len(units_by_phase),
                "phase_unit_counts": [len(units) for _, units in units_by_phase],
                "block_phases": block_phases, "task_count": len(blocks),
                "operation": "joint_repartition_after_shared_producers",
                "legality": "exact coverage and acyclic quotient verified",
            }
            candidates.append((f"fork_f{threshold}_g{count}", blocks, metadata))
    return candidates


def _connector_candidates(graph, cores, seen):
    """把廉价汇合/分叉连接区与相邻完整重链一起重分区。

    只在存在明显成本分离及低成本多度节点时提出这一家族。连续的
    同类操作保持同阶段，因此不会把一条重链按每个重操作单独切碎。
    阶段标签在轻/重转换时加一，所有真实依赖仍原样恢复。
    """
    costs = sorted(graph.cost.values())
    q90 = costs[round((len(costs) - 1) * .9)]
    threshold = q90 / 4
    light = {v: graph.cost[v] <= threshold for v in graph.ops}
    connectors = [v for v in graph.ops if light[v]
                  and (len(graph.pred[v]) > 1 or len(graph.succ[v]) > 1)]
    if not connectors or all(light.values()):
        return []
    phases = {}
    for v in graph.order:
        phases[v] = max((phases[p] + int(light[p] != light[v])
                         for p in graph.pred[v]), default=0)
    global_load = max(graph.workload(graph.ops).values(), default=0) / cores
    heavy_components = [block for block in graph.components
                        if max(graph.workload(block).values(), default=0) > global_load
                        and any(v in block for v in connectors)]
    variants = []
    if heavy_components and len(heavy_components) < len(graph.components):
        selected = frozenset().union(*heavy_components)
        retained = [block for block in graph.components if block.isdisjoint(selected)]
        variants.append(("regions", selected, retained, cores))
    variants.append(("whole", frozenset(graph.ops), [], cores))
    if len(variants) == 1:
        variants.append(("whole", frozenset(graph.ops), [], 2 * cores))
    candidates = []
    for mode, selected, retained, count in variants:
        bands = defaultdict(set)
        for v in graph.order:
            if v in selected:
                bands[phases[v], light[v]].add(v)
        units = [(key, components(nodes, graph.pred, graph.succ))
                 for key, nodes in sorted(bands.items())]
        blocks, block_phases = _pack_phases(graph, units, count, retained=retained)
        key = _partition_key(blocks)
        if key in seen:
            continue
        seen.add(key)
        name = f"connector_regions_g{count}" if mode == "regions" else f"connector_g{count}"
        candidates.append((name, blocks, {
            "family": "light_connectors", "priority": len(candidates),
            "cores": cores, "bucket_count": count,
            "light_max_cycles": threshold, "cost_q90": q90,
            "light_connector_count": len(connectors), "grouping": "balanced",
            "phase_count": len(units), "block_phases": block_phases,
            "region_mode": mode, "repartitioned_operations": len(selected),
            "retained_whole_components": len(retained),
            "heavy_component_work_threshold": global_load,
            "task_count": len(blocks),
            "operation": "joint_repartition_heavy_chains_and_light_connectors",
            "legality": "exact coverage and acyclic quotient verified",
        }))
    return candidates


def structural_partitions(graph, cores, settings, params):
    """返回 [(name, blocks, metadata)]，默认至多 12 个完整结构候选。

    所有桶数由当前模拟核数 N 决定，与宿主 workers 无关。2N 是流水
    候选，N 是较粗候选，2(N-1) 探索邻近批数，不能保证桶越多越好。
    全阶段一次重组是组合动作，不要求单个中间拆分先得到即时收益。
    settings 保留为统一候选 API；此处不把内存代理用作硬可行性约束。
    """
    del settings
    if cores < 1:
        raise ValueError("cores must be positive")
    if not graph.ops:
        return []
    limit = max(0, int(params.get("max_structural_candidates", 12)))
    if not limit:
        return []
    loads = graph.workload(graph.ops)
    pipe = min(loads, key=lambda p: (-loads[p], p))
    dominant_costs = sorted(graph.cost[v] for v in graph.ops if graph.ops[v]["pipe"] == pipe)
    median = dominant_costs[len(dominant_costs) // 2]
    counts = list(dict.fromkeys((2 * cores, max(cores, 2 * (cores - 1)), cores)))
    base_phases, base_anchors = anchor_phases(graph, pipe)
    variants = [("all", base_phases, base_anchors, 1, 0),
                ("paired", base_phases, base_anchors, 2, 0)]
    if median > dominant_costs[0]:
        phases, anchors = anchor_phases(graph, pipe, median)
        variants.append(("heavy", phases, anchors, 1, median))
    stable_by_count = {count: _stable_membership(graph, count) for count in counts}
    candidates, seen = [], set()
    # Each variant contributes its complete region partition. Deduplication is
    # by operation membership, not task numbering or potentially lucky IDs.
    for kind, phases, anchors, span, threshold in variants:
        units_by_phase = _phase_units(graph, phases, span)
        for count in counts:
            modes = ("stable", "balanced") if len(graph.components) > 1 else ("balanced",)
            for mode in modes:
                stable = stable_by_count[count] if mode == "stable" else None
                blocks, block_phases = _pack_phases(graph, units_by_phase, count, stable)
                key = _partition_key(blocks)
                if key in seen:
                    continue
                seen.add(key)
                metadata = {
                    "family": "compute_anchor", "dominant_pipe": pipe,
                    "priority": len(candidates),
                    "dominant_pipe_fraction": loads[pipe] / max(1, sum(loads.values())),
                    "anchor_count": len(anchors), "anchor_min_cycles": threshold,
                    "anchor_phase_count": len(set(phases.values())), "phase_span": span,
                    "phase_count": len(units_by_phase), "bucket_count": count,
                    "cores": cores, "grouping": mode,
                    "phase_unit_counts": [len(units) for _, units in units_by_phase],
                    "block_phases": block_phases,
                    "task_count": len(blocks),
                    "original_weak_components": len(graph.components),
                    "operation": "joint_repartition_all_phases",
                    "legality": "exact coverage and acyclic quotient verified",
                }
                candidates.append((f"anchor_{kind}_{mode}_g{count}", blocks, metadata))
    forks = _fork_candidates(graph, cores, seen)
    connectors = _connector_candidates(graph, cores, seen)
    # Reserve separate family slots: appending fork candidates after a slice
    # would silently suppress them on exactly the large graphs that need them.
    fork_slots = min(len(forks), max(1, limit // 3)) if forks else 0
    connector_slots = min(len(connectors), max(1, limit // 6)) if connectors else 0
    # Very small user limits still need to remain exact, including limit=1.
    anchor_slots = max(1, limit - fork_slots - connector_slots)
    return (candidates[:anchor_slots] + forks[:fork_slots] + connectors[:connector_slots])[:limit]
