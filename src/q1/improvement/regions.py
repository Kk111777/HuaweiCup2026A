"""后支配区域与可展开覆盖：共享输入保留，局部分叉不再强制成为切点。"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from .graph import components
from .partition import pack, quotient


def discover_regions(graph):
    """在反向 DAG 上增量建后支配树；LCA 避免存储二次规模可达矩阵。

    真实输出也接虚拟出口，包括仍有内部消费者的中间输出。区域允许
    多个外部输入，但从分叉到真实直接后支配点不能漏掉任何路径。
    过大的区域只跳过该层，其内部区域和原始链仍可用于覆盖。
    """
    order, rank = graph.order, graph.rank
    n = len(order)
    height = max(1, (n + 1).bit_length())
    up = [None] * n + [[n] * height]
    depth = [0] * (n + 1)
    parent = [n] * n
    terminals = {v for v in order if not graph.succ[v] or any(
        any(graph.all_ops[c]["op"] == "COPY_OUT" for c in graph.consumers[t])
        for t in graph.outputs[v])}

    def lca(a, b):
        if depth[a] < depth[b]:
            a, b = b, a
        difference = depth[a] - depth[b]
        for k in range(height):
            if difference >> k & 1:
                a = up[a][k]
        if a == b:
            return a
        for k in reversed(range(height)):
            if up[a][k] != up[b][k]:
                a, b = up[a][k], up[b][k]
        return up[a][0]

    for v in reversed(order):
        successors = [rank[w] for w in sorted(graph.succ[v])]
        if v in terminals:
            successors.append(n)
        p = successors[0]
        for w in successors[1:]:
            p = lca(p, w)
        i = rank[v]
        parent[i], depth[i] = p, depth[p] + 1
        up[i] = [p]
        for k in range(1, height):
            up[i].append(up[up[i][k - 1]][k - 1])

    regions = set()
    for v in order:
        p = parent[rank[v]]
        if len(graph.succ[v]) < 2 or p == n:
            continue
        end = order[p]
        members, todo = {v}, [v]
        for w in todo:
            if w != end:
                for u in sorted(graph.succ[w] - members):
                    members.add(u)
                    todo.append(u)
            if len(members) > 4096:
                break
        else:
            regions.add(frozenset(members))
    return tuple(sorted(regions, key=lambda b: (-len(b), min(b))))


def region_cover(graph, nodes=None, *, opened=False):
    """完整区域、内部区域和链共同覆盖 nodes；opened 将外层展开一级。"""
    nodes = frozenset(graph.ops if nodes is None else nodes)
    left, blocks = set(nodes), []
    outer = []
    for region in graph.regions:
        if region <= left:
            outer.append(region)
            left.difference_update(region)
    if opened:
        left = set(nodes)
        excluded = set(outer)
        for region in graph.regions:
            if region not in excluded and region <= left:
                blocks.append(region)
                left.difference_update(region)
    else:
        blocks = outer
    for chain in graph.chains:
        remainder = chain & left
        if remainder:
            blocks.extend(components(remainder, graph.pred, graph.succ))
    return blocks


def _region_layouts(graph, units, cores, retained=()):
    """区域上计算粗细两种标签；根据后继汇合打包，不把标签写成依赖。"""
    all_units = units + list(retained)
    _, pred, succ, order = quotient(graph, all_units)
    inside = set(range(len(units)))
    work = {i: max(graph.workload(b).values(), default=1) for i, b in enumerate(units)}
    substantial = [work[i] for i, b in enumerate(units) if len(b) > 1]
    threshold = median(substantial or list(work.values()))
    heavy = {i: work[i] >= threshold for i in inside}
    levels, phases = {}, {}
    for i in order:
        if i not in inside:
            continue
        parents = pred[i] & inside
        levels[i] = max((levels[p] + 1 for p in parents), default=0)
        phases[i] = max((phases[p] + int(heavy[p] != heavy[i]) for p in parents), default=0)
    # 越过唯一消费链观察真正汇合点；不实际融合这些链，尾部仍可单独释放。
    following = {}
    for i in reversed(order):
        following[i] = (following[next(iter(succ[i]))]
                        if len(pred[i]) == 1 and len(succ[i]) == 1 else i)
    variants = [
        ("cost", 1, 0, cores, "successor", "release", 1),
        ("cost", 1, 0, cores, "successor", "uniform", 1),
        ("cost", 2, 1, cores, "successor", "uniform", 1),
        ("cost", 2, 0, cores, "successor", "uniform", 1),
        ("cost", 1, 0, cores, "successor", "release", max(1, cores // 2)),
        ("cost", 1, 0, cores, "balanced", "release", 1),
        ("cost", 1, 0, cores, "balanced", "uniform", 1),
    ]
    for count in dict.fromkeys((max(1, cores - 1), cores + 1, 2 * cores)):
        variants.append(("cost", 1, 0, count, "successor", "release", 1))
    variants += [("level", 1, 0, cores, "successor", "uniform", cores),
                 ("level", 2, 1, cores, "balanced", "uniform", cores)]
    seen = set()
    for kind, span, offset, count, policy, grain, preparation in variants:
        labels = phases if kind == "cost" else levels
        bands = defaultdict(set)
        for i in sorted(inside):
            bands[(labels[i] + offset) // span].add(i)
        blocks = []
        for band in bands.values():
            groups = components(band, pred, succ)
            if policy == "successor":
                related = defaultdict(set)
                for group in groups:
                    exits = frozenset().union(*(succ[i] for i in group)) - band
                    key = ("outputs", tuple(sorted({following[i] for i in exits}))) if exits else (
                        "terminal", min(group))
                    related[key].update(group)
                groups = [frozenset(g) for g in related.values()]
            parts = [frozenset().union(*(units[i] for i in group)) for group in groups]
            q = count
            # 只有轻准备区收粗；后续轻连接区保留独立单元，主区按 N 装桶。
            if kind == "cost" and not any(heavy[i] for i in band):
                if not any(pred[i] & (inside - band) for i in band):
                    q = preparation
                elif grain == "release":
                    q = len(parts)
            blocks.extend(pack(graph, parts, q))
        blocks += list(retained)
        key = frozenset(blocks)
        if key in seen:
            continue
        try:
            quotient(graph, blocks)
        except ValueError:
            continue
        seen.add(key)
        name = f"{kind}_w{span}o{offset}_{policy}_{grain}_g{count}p{preparation}"
        yield name, blocks, {"label": kind, "span": span, "offset": offset,
                             "packing": policy, "granularity": grain, "bucket_count": count,
                             "preparation_buckets": preparation, "work_threshold": threshold}


def region_partitions(graph, cores, settings, params):
    """生成全图多层区域分区，实际搜索的初始化入口。"""
    del settings
    limit = params.get("max_region_candidates", 24)
    if not limit or not graph.ops:
        return []
    candidates, seen = [], set()
    for opened in (False, True):
        units = region_cover(graph, opened=opened)
        for name, blocks, metadata in _region_layouts(graph, units, cores):
            key = frozenset(blocks)
            if key in seen:
                continue
            seen.add(key)
            name = f"region_{'open' if opened else 'closed'}_{name}"
            candidates.append((name, blocks, {**metadata, "family": "regions",
                "regions": len(graph.regions), "coarse_units": len(units),
                "opened": opened, "task_count": len(blocks)}))
            if len(candidates) >= limit:
                return candidates
    return candidates


def rebuild_regions(graph, blocks, result, cores, scale):
    """取出多个旧 Task 并一次修复；区域外成员保持不动，调度由调用方重建。"""
    owner, pred, succ, _ = quotient(graph, blocks)
    limit = (4, 8, 16)[scale]
    fragmented = [(len({owner[v] for v in r}), r) for r in graph.regions]
    fragmented = sorted((item for item in fragmented if item[0] > 1),
                        key=lambda item: (-item[0], -len(item[1]), min(item[1])))
    seeds = [{owner[v] for v in region} for _, region in fragmented[:3]]
    # 官方时间线可能使用非连续 Task ID，按 validate_plan 的排序对应块下标。
    tasks = {t["task_id"]: t for c in result["per_core_timeline"] for t in c["tasks"]}
    ids = sorted(tasks)
    for task in sorted(tasks, key=lambda t: (-tasks[t]["end"], -tasks[t]["duration"], t))[:2]:
        i = ids.index(task)
        seeds.append({i} | pred[i] | succ[i])
    seen_zones, seen_parts = set(), {frozenset(blocks)}
    for selected in seeds:
        if not selected or len(selected) > limit:
            continue
        # 商图路径闭包，不能删掉从区域离开后又返回的中间依赖。
        def reach(adj):
            found, todo = set(selected), list(selected)
            for i in todo:
                for j in sorted(adj[i] - found):
                    found.add(j)
                    todo.append(j)
            return found
        selected = frozenset(reach(pred) & reach(succ))
        if len(selected) > limit or selected in seen_zones:
            continue
        seen_zones.add(selected)
        nodes = frozenset().union(*(blocks[i] for i in selected))
        retained = [b for i, b in enumerate(blocks) if i not in selected]
        for opened in (False, True):
            units = region_cover(graph, nodes, opened=opened)
            for index, (_, rebuilt, _) in enumerate(_region_layouts(graph, units, cores, retained)):
                if index >= 4:
                    break
                key = frozenset(rebuilt)
                if key not in seen_parts:
                    seen_parts.add(key)
                    yield rebuilt
