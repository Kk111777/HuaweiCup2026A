"""Original candidate operators, extracted without changing ranking or generation rules."""
from collections import defaultdict
from .partition import chain_preserving, memory_first, staged, merge, split, release_prefix, quotient
from .schedule import schedule, validate_plan, cheap_plan_proxy, profile_costs_for_partition, CHEAP_PROXY_MODE, CHEAP_PROXY_NOTE
from .shared import plan_digest
from .regions import region_partitions
def legacy_initial_candidates(graph, cores, settings, params):
    candidates = []
    for batches in (1, 2, 4):
        blocks = chain_preserving(graph, cores, batches)
        plan, proxy = schedule(graph, blocks, cores, settings)
        candidates.append((f"chain_preserving_b{batches}", plan, proxy))
    blocks = memory_first(graph, cores, settings["capacity"], max_merges=params["memory_merges"],
                          soft_factors=params["memory_soft_factors"])
    plan, proxy = schedule(graph, blocks, cores, settings)
    candidates.append(("memory_first", plan, proxy))
    for width in params["stage_widths"]:
        for batches in params["stage_batches"]:
            blocks = staged(graph, cores, width, batches)
            plan, proxy = schedule(graph, blocks, cores, settings)
            candidates.append((f"stage_w{width}_b{batches}", plan, proxy))
    unique = {}
    for candidate in candidates:
        unique.setdefault(plan_digest(candidate[1]), candidate)
    candidates = list(unique.values())
    # 保留初始化家族多样性：完整分支、memory-first、两种不同阶段宽度。
    chosen = []
    for family in ("chain_", "memory_", "stage_"):
        options = [c for c in candidates if c[0].startswith(family)]
        if options:
            chosen.append(min(options, key=lambda c: (c[2], c[0])))
    # 每个阶段宽度至少留一项，避免代理偏差完全排除粗/细阶段。
    for width in params["stage_widths"]:
        options = [c for c in candidates if c[0].startswith(f"stage_w{width}_")]
        if options:
            item = min(options, key=lambda c: (c[2], c[0]))
            if item not in chosen:
                chosen.append(item)
    for item in sorted(candidates, key=lambda c: (c[2], c[0])):
        if item not in chosen:
            chosen.append(item)
    # 深图先给出阶段候选，避免慢的整分支仿真耗尽首次可行解预算。
    if max(graph.level.values(), default=0) >= 32:
        best_stage = next((c for c in chosen if c[0].startswith("stage_")), None)
        if best_stage:
            chosen.remove(best_stage)
            chosen.insert(0, best_stage)
    return chosen[:8]


def initial_candidates(graph, cores, settings, params, audit=None):
    """结构家族轮换，保留不同纵向粒度；核数改变时重新构造整套划分。"""
    legacy = legacy_initial_candidates(graph, cores, settings, params)
    if audit is not None:
        audit.extend({"action": name, "stage": "generated", "family": "legacy",
                      "proxy_cycles": proxy, "proxy_mode": CHEAP_PROXY_MODE}
                     for name, _, proxy in legacy)
    families = defaultdict(list)
    for name, blocks, metadata in region_partitions(graph, cores, settings, params):
        plan, proxy = schedule(graph, blocks, cores, settings)
        families["regions"].append((name, plan, proxy))
        if audit is not None:
            audit.append({"action": name, "stage": "generated", **metadata,
                          "proxy_cycles": proxy, "proxy_mode": CHEAP_PROXY_MODE})
    if params.get("enable_structural", True):
        from .structural import structural_partitions
        for name, blocks, metadata in structural_partitions(graph, cores, settings, params):
            plan, _ = schedule(graph, blocks, cores, settings)
            candidate = (name, plan, cheap_plan_proxy(graph, plan, settings))
            families[metadata.get("family", "structural")].append(candidate)
            if audit is not None:
                audit.append({"action": name, "stage": "generated", **metadata,
                              "proxy_cycles": candidate[2], "proxy_mode": CHEAP_PROXY_MODE})
    # structural.py 返回结构优先级顺序，保留纵向跨度/横向桶数差异。
    # 代理只在同家族代表后补位；不能重新全局排序而取消家族的实际评估机会。
    families["legacy"] = legacy
    selected, seen = [], set()
    # 浅图原有整分支划分很强，先给其一个位置，再轮换新结构家族。
    if max(graph.level.values(), default=0) < 8 and legacy:
        selected.append(legacy[0])
        seen.add(plan_digest(legacy[0][1]))
    # 新的区域骨架先获得实际机会；余下各家族仍轮换，未入首批者稍后补评。
    for candidate in families["regions"][:8]:
        digest = plan_digest(candidate[1])
        if digest not in seen:
            selected.append(candidate)
            seen.add(digest)
    for depth in range(max(map(len, families.values()), default=0)):
        for values in families.values():
            if depth >= len(values):
                continue
            candidate = values[depth]
            digest = plan_digest(candidate[1])
            if digest not in seen:
                selected.append(candidate)
                seen.add(digest)
    for candidate in selected[:params["max_initial_evaluations_per_core"]]:
        if audit is not None:
            audit.append({"action": candidate[0], "stage": "shortlist",
                          "proxy_cycles": candidate[2], "proxy_mode": CHEAP_PROXY_MODE})
    return selected


def _retain_states(states, incoming, limit):
    """质量名额之外保留不同分区，探索状态不覆盖已核验的最终最好解。"""
    unique = {}
    for state in states + [incoming]:
        signature = state["signature"]
        if signature not in unique or state["objective"] < unique[signature]["objective"]:
            unique[signature] = state
    ordered = sorted(unique.values(), key=lambda s: (s["objective"], s["digest"]))
    eligible = [s for s in ordered if s["objective"][0] <= ordered[0]["objective"][0] * 1.1]
    chosen = eligible[:max(1, limit // 2)]
    remaining = eligible[len(chosen):]
    while remaining and len(chosen) < limit:
        def diversity(state):
            a = state["signature"]
            return min(len(a ^ s["signature"]) / max(1, len(a | s["signature"])) for s in chosen)
        selected = max(remaining, key=diversity)
        chosen.append(selected)
        remaining.remove(selected)
    return chosen


def local_candidates(graph, plan, result, settings, params):
    cores = len(plan["core_schedules"])
    blocks = validate_plan(graph, plan, cores)
    task_ids = sorted(set(plan["node_to_subgraph"].values()))
    task_index = {task: i for i, task in enumerate(task_ids)}
    _, pred, succ, _ = quotient(graph, blocks)
    source_profiles = {int(i): p["local_makespan"] for i, p in result["step3_by_task"].items()}
    profiles = profile_costs_for_partition(graph, blocks, plan, source_profiles)
    timeline = {task_index[t["task_id"]]: t for c in result["per_core_timeline"] for t in c["tasks"]}
    proposals = []
    seen = {plan_digest(plan)}
    def add(name, new_blocks=None, new_plan=None, durations=None, priority="critical"):
        if len(proposals) >= params["max_proposals_per_round"]:
            return
        try:
            if new_plan is None:
                new_plan, proxy = schedule(graph, new_blocks, cores, settings, durations, priority)
            else:
                validate_plan(graph, new_plan, cores)
            # 官方profile只指导同分区的构造；所有完整候选以同精度模型比较。
            proxy = cheap_plan_proxy(graph, new_plan, settings)
            digest = plan_digest(new_plan)
            if digest not in seen:
                seen.add(digest)
                proposals.append((name, new_plan, proxy))
        except ValueError:
            pass
    for priority in ("critical", "fifo", "largest"):
        add("profile_reorder_" + priority, blocks, durations=profiles, priority=priority)

    # 真实长尾 + 长 Task；参考压力只定位，不等同于官方 spill。
    hot = sorted(timeline, key=lambda i: (-timeline[i]["end"], -timeline[i]["duration"], i))[:3]
    hot += sorted(profiles, key=lambda i: (-profiles[i], i))[:3]
    for i in dict.fromkeys(hot):
        block = blocks[i]
        order = sorted(block, key=graph.rank.__getitem__)
        cuts = []
        for fraction in (.25, .5, .75):
            cut = int(len(order) * fraction)
            if 0 < cut < len(order):
                cuts.append(("pressure_split", frozenset(order[:cut])))
        # 实际操作完成到 Task 完成的间隔，结合静态近关键后继定位发布门。
        op_end = {o["op_id"]: o["end"] for c in result["per_core_timeline"]
                  for o in c["ops"] if o["task_id"] == task_ids[i]}
        external = [v for v in block if graph.succ[v] - block]
        external.sort(key=lambda v: (
            -(timeline[i]["end"] - op_end.get(v, timeline[i]["end"])) *
            (1 + max((graph.backward[w] for w in graph.succ[v] - block), default=0)), v))
        for v in external[:3]:
            cuts.append(("release_split", release_prefix(graph, block, v)))
        # 无换出参考峰值突增处：在最大同时触及张量量的操作前切分。
        if len(order) > 1:
            v = max(order[1:], key=lambda v: sum(graph.tensors[t]["size"]
                    for t in graph.inputs[v] | graph.outputs[v]))
            cuts.append(("pressure_spike_split", frozenset(order[:order.index(v)])))
        for kind, prefix in cuts[:params["max_cuts_per_task"]]:
            try:
                add(kind, split(graph, blocks, i, prefix))
            except ValueError:
                pass
        for j in sorted(pred[i] | succ[i])[:3]:
            try:
                add("boundary_merge", merge(graph, blocks, {i, j}))
            except ValueError:
                pass

    # 热点 tensor 的两个消费块或生产消费块，最多 12 次完整合法性检查。
    owner = {v: i for i, b in enumerate(blocks) for v in b}
    tensor_hotspots = []
    for tid, tensor in graph.tensors.items():
        p = {owner[v] for v in graph.producers[tid] if v in owner}
        c = {owner[v] for v in graph.consumers[tid] if v in owner}
        remote = c - p
        if len(p | c) > 1:
            tensor_hotspots.append((tensor["size"] * (len(remote) + bool(remote and p)), tid, p, c))
    for _, tid, p, c in sorted(tensor_hotspots, key=lambda x: (-x[0], x[1]))[:12]:
        ids = sorted(p | c)[:2]
        if len(ids) == 2:
            try:
                add("tensor_merge", merge(graph, blocks, ids))
            except ValueError:
                pass

    # 队尾整块迁移、相邻重排和尾批合并；每次均检查组合图。
    for c, queue in enumerate(plan["core_schedules"]):
        if not queue:
            continue
        for target in range(cores):
            if target == c:
                continue
            queues = [list(q) for q in plan["core_schedules"]]
            task = queues[c].pop()
            queues[target].append(task)
            add("tail_move", new_plan={"node_to_subgraph": dict(plan["node_to_subgraph"]),
                                       "core_schedules": queues})
        if len(queue) >= 2:
            queues = [list(q) for q in plan["core_schedules"]]
            queues[c][-2:] = reversed(queues[c][-2:])
            add("tail_reorder", new_plan={"node_to_subgraph": dict(plan["node_to_subgraph"]),
                                          "core_schedules": queues})
            try:
                add("tail_merge", merge(graph, blocks, [task_index[t] for t in queue[-2:]]))
            except ValueError:
                pass
    # 各动作家族先保留最好的一个，再按代理补齐；避免一种代理淹没发布门。
    by_kind = defaultdict(list)
    for proposal in proposals:
        by_kind[proposal[0]].append(proposal)
    selected = sorted((min(v, key=lambda c: c[2]) for v in by_kind.values()), key=lambda c: c[2])
    for proposal in sorted(proposals, key=lambda c: c[2]):
        if proposal not in selected:
            selected.append(proposal)
    return selected[:params["proxy_keep"]], len(proposals)
