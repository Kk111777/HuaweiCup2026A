"""无环商图、前驱封闭切分、保链分阶段打包及有界热点合并。"""
from __future__ import annotations

from collections import Counter, defaultdict
from .graph import components, topological


from ..plan import quotient


def merge(graph, blocks, indices, *, closure_limit=0):
    selected = set(indices)
    _, pred, succ, _ = quotient(graph, blocks)
    if not selected or not selected <= pred.keys():
        raise ValueError("invalid merge selection")
    def reach(adj):
        found, todo = set(selected), list(selected)
        for v in todo:
            for w in sorted(adj[v] - found):
                found.add(w)
                todo.append(w)
        return found
    closure = reach(pred) & reach(succ)
    if closure != selected:
        if not closure_limit or sum(len(blocks[i]) for i in closure) > closure_limit:
            raise ValueError("merge would create a quotient cycle")
        selected = closure
    merged = frozenset().union(*(blocks[i] for i in sorted(selected)))
    result = [b for i, b in enumerate(blocks) if i not in selected] + [merged]
    quotient(graph, result)
    return result


def split(graph, blocks, index, prefix):
    prefix = frozenset(prefix)
    block = blocks[index]
    if not prefix or not prefix < block:
        raise ValueError("split must have two nonempty parts")
    if any((graph.pred[v] & block) - prefix for v in prefix):
        raise ValueError("split prefix is not predecessor closed")
    result = blocks[:index] + [prefix, block - prefix] + blocks[index + 1:]
    quotient(graph, result)
    return result


def release_prefix(graph, block, vertex):
    if vertex not in block:
        raise ValueError("release vertex is outside task")
    prefix, queue = {vertex}, [vertex]
    for v in queue:
        for w in sorted((graph.pred[v] & block) - prefix):
            prefix.add(w)
            queue.append(w)
    return frozenset(prefix)


def pack(graph, units, count):
    """互不相依的单元以 Pipe 负载均衡打包，共享输入仅作同负载时的偏好。"""
    bins = [set() for _ in range(min(count, len(units)))]
    loads, inputs = [Counter() for _ in bins], [set() for _ in bins]
    ordered = sorted(units, key=lambda b: (-max(graph.workload(b).values(), default=0), min(b)))
    for unit in ordered:
        work = graph.workload(unit)
        tins = set().union(*(graph.inputs[v] for v in unit))
        best = min(range(len(bins)), key=lambda i: (
            max((loads[i][p] + work[p] for p in loads[i].keys() | work.keys()), default=0),
            -sum(graph.tensors[t]["size"] for t in tins & inputs[i]), i))
        bins[best].update(unit)
        loads[best].update(work)
        inputs[best].update(tins)
    return [frozenset(b) for b in bins if b]


def staged(graph, cores, width, batches=1):
    """深度带内取弱分量，纵向分阶段、横向打包；不添加阶段屏障。"""
    bands = defaultdict(set)
    for v in graph.order:
        bands[graph.level[v] // width].add(v)
    blocks = []
    for band in sorted(bands):
        units = components(bands[band], graph.pred, graph.succ)
        blocks.extend(pack(graph, units, cores * batches))
    quotient(graph, blocks)
    return blocks


def chain_preserving(graph, cores, batches=1):
    # 独立完整分支优先，尤其避免把短 M/V 链沿 Pipe 切断。
    return pack(graph, graph.components, cores * batches)


def memory_first(graph, cores, capacity, *, max_merges=128, soft_factors=None):
    blocks = list(graph.chains)
    soft_factors = soft_factors or {"short": .9, "ordinary": .8, "complex": .7}
    # 只遍历少数热点及其消费者，不枚举子集或递归吸收全部祖先。
    hotspots = sorted(graph.tensors, key=lambda t: (
        -graph.tensors[t]["size"] / capacity.get(graph.tensors[t]["pos"], capacity["UB"]), t))
    attempts = 0
    # The ownership map changes only after an accepted merge. Rebuilding it for
    # every tensor made unchanged partitions quadratic on large unknown graphs.
    owner = {v: i for i, block in enumerate(blocks) for v in block}
    for tid in hotspots:
        if attempts >= max_merges:
            break
        producers = sorted(graph.producers[tid] & graph.ops.keys())
        consumers = sorted(graph.consumers[tid] & graph.ops.keys())
        if not producers or not consumers:
            continue
        source = owner[producers[0]]
        targets = sorted({owner[v] for v in consumers} - {source})[:2]
        for target in targets[:1]:
            attempts += 1
            united = blocks[source] | blocks[target]
            # 软阈值只控制候选合并倾向，不拒绝输入图或宣布无解。
            if len(united) > 96:
                continue
            pressure = graph.reference_pressure(united)
            complexity = sum(len(graph.pred[v]) > 1 or len(graph.succ[v]) > 1 for v in united)
            category = "short" if len(united) <= 8 and not complexity else "complex" if complexity > 2 else "ordinary"
            if any(pressure.get(p, 0) > soft_factors[category] * cap for p, cap in capacity.items()):
                continue
            try:
                blocks = merge(graph, blocks, {source, target})
                owner = {v: i for i, block in enumerate(blocks) for v in block}
            except ValueError:
                pass
    _, pred, succ, order = quotient(graph, blocks)
    levels = {}
    layers = defaultdict(list)
    for i in order:
        levels[i] = max((levels[p] + 1 for p in pred[i]), default=0)
        layers[levels[i]].append(blocks[i])
    packed = []
    for level in sorted(layers):
        packed.extend(pack(graph, layers[level], cores * 2))
    quotient(graph, packed)
    return packed


def boundary_bytes(graph, blocks):
    """按原逻辑 tensor 和不同目的 Task 计数；不按每条消费边重复计费。"""
    owner = {v: i for i, block in enumerate(blocks) for v in block}
    reads, writes = Counter(), Counter()
    for tid, tensor in graph.tensors.items():
        p = {owner[v] for v in graph.producers[tid] if v in owner}
        c = {owner[v] for v in graph.consumers[tid] if v in owner}
        original_out = any(graph.all_ops[v]["op"] == "COPY_OUT" for v in graph.consumers[tid])
        for task in c - p:
            reads[task] += tensor["size"]
        for task in p:
            if original_out or not c or c - {task}:
                writes[task] += tensor["size"]
    return reads, writes
