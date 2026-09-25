"""五类物理诊断：独立排序、子检测器轮转；评分不是剪枝条件。"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from .bounds import expanded
from .state import closure, digest

PRINCIPLES = ("locality", "pipeline", "boundary", "ddr", "balance")


def overlap(a, b):
    i = j = total = 0
    while i < len(a) and j < len(b):
        total += max(0, min(a[i][1], b[j][1]) - max(a[i][0], b[j][0]))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return total


def diagnose(state, result, prepared, options):
    ctx, idx = state.context, state.context.index
    tasks = {t["task_id"]: t for core in result["per_core_timeline"] for t in core["tasks"]}
    ops = {(o["task_id"], o["op_id"]): o for core in result["per_core_timeline"] for o in core["ops"]}
    queues = {p: defaultdict(list) for p in PRINCIPLES}
    prefiltered = defaultdict(int)
    pipe_intervals = defaultdict(lambda: defaultdict(list))
    for (j, _), op in ops.items():
        pipe_intervals[j][op["pipe"]].append((op["start"], op["end"]))
    kmax = options.partners_per_seed
    finish = [max((tasks[j]["end"] for j in order), default=0) for order in state.orders]
    work = [tuple(sum(state.measure[j]["work"][p] for j in order) for p in range(2)) for order in state.orders]
    use = [sum(tasks[j]["duration"] for j in order) / max(1, result["makespan"]) for order in state.orders]
    previous = {j: (order[n-1] if n else None) for order in state.orders for n, j in enumerate(order)}

    def add(principle, kind, related, score, *, tensor=None, operation=None, cuts=None, evidence=None):
        related = sorted(set(related))
        if not related:
            return
        identity = (principle, kind, related, tensor, operation, cuts or {})
        seed = dict(id=digest(identity), principle=principle, kind=kind, tasks=related,
                    score=list(score), tensor=tensor, operation=operation,
                    cuts=cuts or {}, evidence=evidence or {})
        queues[principle][kind].append(seed)

    def front(j, vs):
        return sorted(closure(set(vs), idx.preds, state.nodes[j]))

    def gain(i, j):
        a, b = state.measure[i], state.measure[j]
        value = sum(idx.tensors[t].copy_cycles for t in a["inputs"] & b["inputs"])
        for x, y in ((i, j), (j, i)):
            for t in state.measure[x]["outputs"] & state.measure[y]["inputs"]:
                value += idx.tensors[t].copy_cycles
                tensor = idx.tensors[t]
                if not tensor.final_output and all(state.mapping[v] in (i, j) for v in tensor.consumers):
                    value += tensor.copy_cycles
        return value

    # Large fanout tensors are ranked before constructing expensive closure cuts.
    shared = []
    for tid, t in idx.tensors.items():
        consumers = sorted({state.mapping[v] for v in t.consumers})
        if t.producer is None and len(consumers) > 1:
            shared.append(((len(consumers)-1)*t.copy_cycles, t.size, tid, consumers))
    prefiltered["shared_input_tensors"] = max(0, len(shared)-options.seeds_per_principle*kmax)
    for score, size, tid, js in sorted(shared, key=lambda x: (-x[0], -x[1], x[2]))[:options.seeds_per_principle*kmax]:
        prefiltered["shared_input_pairs"] += max(0, len(js)*(len(js)-1)//2-kmax)
        pairs = sorted(combinations(js, 2), key=lambda pair: (-gain(*pair),
                       abs(tasks[pair[0]]["start"]-tasks[pair[1]]["start"]), pair))[:kmax]
        for i, j in pairs:
            add("locality", "shared_input", (i, j), (score, gain(i, j), size), tensor=tid,
                evidence={"consumer_tasks": js, "boundary_saving_cycles": gain(i, j)})
    for i, j in combinations(state.nodes, 2):
        saving = gain(i, j)
        a, b = state.measure[i]["work"], state.measure[j]["work"]
        complement = max(a)+max(b)-max(a[0]+b[0], a[1]+b[1])
        if saving > 0:
            add("locality", "boundary_reuse", (i, j), (saving,), evidence={"boundary_saving_cycles": saving})
        if complement > 0:
            add("pipeline", "complement", (i, j), (complement, saving), evidence={"complement_cycles": complement})

    for j, record in prepared["diagnostics"].items():
        spills = record["step2"]["spill_records"]
        if spills:
            prefiltered["spill_cuts"] += max(0, len(spills)-kmax)
            q = sum(max(1, (s["size"]+ctx.settings.bandwidth-1)//ctx.settings.bandwidth)
                    * (1+int(s["spill_out_copies_data"])) for s in spills)
            positions = {v: n for n, v in enumerate(record["step1"])}
            cuts = []
            for s in sorted(spills, key=lambda s: (-s["size"], s["next_use_step"], s["tid"]))[:kmax]:
                at = positions[s["next_use_op"]]
                cut = front(j, [v for v in record["step1"][:at] if v in state.nodes[j]])
                if cut and len(cut) < len(state.nodes[j]):
                    extra = ctx.measure(cut)["q"] + ctx.measure(state.nodes[j]-set(cut))["q"]-state.measure[j]["q"]
                    cuts.append((extra, cut))
            for extra, cut in sorted(cuts, key=lambda x: (x[0], x[1])):
                add("locality", "spill", (j,), (q, -extra), cuts={str(j): [cut]},
                    evidence={"spill_cycles": q, "new_boundary_cycles": extra, "spill_count": len(spills)})

    # Join branches are inspected on the original dependency relation. Ancestor
    # branches are not described as independent; common predecessors stay unique.
    for v in idx.ids:
        ps = sorted(idx.preds[v])
        ms = [p for p in ps if idx.ops[p]["pipe"] == "PIPE_M"]
        vs = [p for p in ps if idx.ops[p]["pipe"] == "PIPE_V"]
        prefiltered["join_pairs"] += len(ms)*len(vs)-min(len(ms), kmax)*min(len(vs), kmax)
        for m in ms[:kmax]:
            for w in vs[:kmax]:
                jm, jw = state.mapping[m], state.mapping[w]
                if m in closure({w}, idx.preds) or w in closure({m}, idx.preds):
                    continue
                score = min(max(1, idx.cycles(m)), max(1, idx.cycles(w)))
                add("pipeline", "independent_join", (jm, jw, state.mapping[v]), (score,), operation=v,
                    evidence={"branch_ends": [m, w]})
    for j in state.nodes:
        intervals = {p: sorted(pipe_intervals[j][p]) for p in ("PIPE_M", "PIPE_V")}
        omega = overlap(intervals["PIPE_M"], intervals["PIPE_V"])
        loss = min(state.measure[j]["work"])-omega
        if loss > 0:
            add("pipeline", "overlap_gap", (j,), (loss,), evidence={"actual_overlap": omega})

    links = defaultdict(lambda: {"sources": set(), "consumers": set(), "tensors": set()})
    for v in idx.ids:
        i = state.mapping[v]
        for w in idx.succs[v]:
            j = state.mapping[w]
            if i != j:
                link = links[i, j]
                link["sources"].add(v)
                link["consumers"].add(w)
                link["tensors"].update(idx.outputs[v] & idx.inputs[w])
    boundary_out = {}
    for j, record in prepared["diagnostics"].items():
        graph = record["boundary_graph"]
        outs = {o["id"] for o in graph["ops"] if o["op"] == "COPY_OUT"}
        for edge in graph["edges"]:
            if edge["target"] in outs:
                boundary_out[j, edge["source"]] = ops[j, edge["target"]]["end"]
    releases = {}
    for (i, j), link in links.items():
        if state.core[i] == state.core[j]:
            continue
        completed = [boundary_out[i, t] for t in link["tensors"]]
        # Include direct/control dependency sources as well as tensor writes.
        completed.extend(ops[i, v]["end"] for v in link["sources"])
        c = max(completed, default=tasks[i]["end"])
        h = max(0, tasks[i]["end"]-c)
        releases[i, j] = h
        x = tasks[i]["end"] + ctx.settings.cross_core_wait
        prior = previous[j]
        other = [0 if prior is None else tasks[prior]["end"]+ctx.settings.same_core_wait]
        other.extend(tasks[p]["end"]+ctx.settings.cross_core_wait for p in state.preds[j]
                     if p != i and state.core[p] != state.core[j])
        score = min(h, max(0, x-max(other)))
        if score > 0:
            add("boundary", "release", (i, j), (score, h),
                cuts={str(i): [front(i, link["sources"])]}, evidence={"target": j, "source": i, "data_ready": c, "h": h})
    for j in state.nodes:
        arrivals = {p: tasks[p]["end"]+ctx.settings.cross_core_wait for p in state.preds[j]
                    if state.core[p] != state.core[j]}
        levels = sorted(set(arrivals.values()), reverse=True)
        if not levels:
            continue
        latest = [p for p in arrivals if arrivals[p] == levels[0]]
        if len(latest) > 1:
            h = min(releases.get((p, j), 0) for p in latest)
            prior = previous[j]
            other = max([0 if prior is None else tasks[prior]["end"]+ctx.settings.same_core_wait,
                         *[a for p, a in arrivals.items() if p not in latest]])
            score = min(h, max(0, levels[0]-other))
            if score > 0:
                add("boundary", "tied_release", [j, *latest], (score,),
                    cuts={str(p): [front(p, links[p, j]["sources"])] for p in latest},
                    evidence={"target": j, "sources": sorted(latest)})
        for count in range(1, min(2, len(levels))+1):
            late = [p for p, arrival in arrivals.items() if arrival >= levels[count-1]]
            affected = set().union(*(links[p, j]["consumers"] for p in late))
            down = closure(affected, idx.succs, state.nodes[j])
            early = state.nodes[j]-down
            if not early or not down:
                continue
            ps = {state.mapping[p] for v in early for p in idx.preds[v] if p not in early and state.mapping[p] != j}
            prior = previous[j]
            ready = max([0 if prior is None else tasks[prior]["end"]+ctx.settings.same_core_wait,
                         *[tasks[p]["end"]+ctx.settings.cross_core_wait for p in ps if state.core[p] != state.core[j]]])
            score = min(max(0, tasks[j]["start"]-ready), ctx.measure(early)["d0"])
            if score > 0:
                add("boundary", "late_input", [j, *late], (score,), cuts={str(j): [sorted(early)]},
                    evidence={"target": j, "late_predecessors": sorted(late), "reference_ready": ready})

    b1 = expanded(state, prepared)
    copies = []
    for key, q in b1["copies"].items():
        j, oid = key[1:]
        item = ops[j, oid]
        delay = item["end"]-item["start"]-q
        slack = result["makespan"]-item["end"]-b1["tail"][key]
        copies.append((j, oid, item["start"], item["end"], q, delay, slack))
    ranked = sorted((o for o in copies if o[5] > 0), key=lambda o: (-o[5], o[6], -o[4], o[0], o[1]))
    prefiltered["contended_copies"] = max(0, len(ranked)-options.seeds_per_principle*kmax)
    for j, oid, start, end, q, delay, slack in ranked[:options.seeds_per_principle*kmax]:
        competitors = defaultdict(int)
        for k, other, a, b, *_ in copies:
            if k != j:
                competitors[k] += max(0, min(end, b)-max(start, a))
        partners = sorted((k for k in competitors if competitors[k] > 0), key=lambda k: (-competitors[k], k))[:kmax]
        prefiltered["ddr_partners"] += max(0, sum(v > 0 for v in competitors.values())-kmax)
        for k in partners:
            add("ddr", "contention", (j, k), (delay, -slack, q, competitors[k]), operation=[j, oid],
                evidence={"slowdown": delay, "tail_slack": slack, "competitor": k,
                          "overlap_cycles": competitors[k]})
    for j in state.nodes:
        k = state.core[j]
        light = min(range(len(work)), key=lambda c: (max(work[c]), finish[c], c))
        if k != light or len(state.nodes[j]) > 1:
            add("balance", "load", (j,), (finish[k], max(work[k]), tasks[j]["duration"], max(idx.b[v] for v in state.nodes[j])),
                evidence={"source_core": k, "light_core": light, "task_utilization": use[k], "core_work": work[k]})

    selected_by_principle, counts = {}, {}
    for principle in PRINCIPLES:
        subqueues = queues[principle]
        for queue in subqueues.values():
            queue.sort(key=lambda s: (tuple(-x for x in s["score"]), s["tasks"], str(s["tensor"]), str(s["operation"]), s["id"]))
        counts[principle] = {kind: len(q) for kind, q in sorted(subqueues.items())}
        selected_by_principle[principle] = []
        taken = 0
        for position in range(options.seeds_per_principle):
            for kind in sorted(subqueues):
                if position < len(subqueues[kind]) and taken < options.seeds_per_principle:
                    selected_by_principle[principle].append(subqueues[kind][position])
                    taken += 1
    selected = [selected_by_principle[p][i] for i in range(options.seeds_per_principle)
                for p in PRINCIPLES if i < len(selected_by_principle[p])]
    # Materialize only selected cuts; score queues must not spend quadratic time
    # constructing large ancestor sets for seeds that will not be searched.
    for seed in selected:
        if seed["kind"] == "shared_input":
            tid = seed["tensor"]
            seed["cuts"] = {str(j): [front(j, idx.tensors[tid].consumers & state.nodes[j])] for j in seed["tasks"]}
        elif seed["kind"] == "independent_join":
            for v in seed["evidence"]["branch_ends"]:
                j = state.mapping[v]
                seed["cuts"].setdefault(str(j), []).append(front(j, {v}))
        elif seed["kind"] == "contention":
            j, oid = seed["operation"]
            target = ops[j, oid]
            competitor = seed["evidence"]["competitor"]
            task = prepared["tasks"][competitor]
            candidates = []
            for other, op in task["op_by_id"].items():
                if op["op"] != "COPY_IN":
                    continue
                event = ops[competitor, other]
                shared = max(0, min(target["end"], event["end"])-max(target["start"], event["start"]))
                if shared:
                    candidates.append((-shared, other))
            for _, other in sorted(candidates)[:kmax]:
                tensors = set(task["out_tids"][other])
                consumers = {edge["target"] for edge in task["graph"]["edges"]
                             if edge["source"] in tensors and edge["target"] in state.nodes[competitor]}
                if not consumers:
                    continue
                first_use = min(ops[competitor, v]["start"] for v in consumers)
                lead = first_use-ops[competitor, other]["end"]
                down = closure(consumers, idx.succs, state.nodes[competitor])
                early = state.nodes[competitor]-down
                if lead > 0 and early and down:
                    seed["cuts"].setdefault(str(competitor), []).append(sorted(early))
                    seed["evidence"].setdefault("early_reads", []).append(dict(
                        copy_id=other, first_use=first_use, lead_cycles=lead, consumers=sorted(consumers)))
    return dict(seeds=selected, seed_counts=counts, selected_count=len(selected),
                core_finish=finish, core_work=work, task_utilization=use,
                prefilter_unsearched=dict(prefiltered),
                not_selected=sum(sum(c.values()) for c in counts.values())-len(selected))
