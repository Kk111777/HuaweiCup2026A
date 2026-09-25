"""分级候选评价、限定域分支定界和接受后重新诊断的三层主循环。"""
from __future__ import annotations

from collections import Counter
import json
from time import perf_counter

from .. import official
from . import bounds
from .diagnose import diagnose
from .evaluate import Evaluator
from .neighborhoods import build_plan, canonical_groups, domains, layer1
from .state import (Context, CycleError, OptimizationResult, PlanState, SearchOptions,
                    closure, digest, objective, topological)


class SearchStopped(Exception):
    pass


class Search:
    def __init__(self, context, options, progress=None):
        self.context, self.options = context, options
        self.progress = progress or (lambda event: None)
        self.evaluator = Evaluator(context, self.progress)
        self.records = []
        self.counts = Counter()

    def record(self, entry):
        self.records.append(entry)
        self.counts[entry.get("status", entry.get("event", "record"))] += 1
        if entry.get("pruning"):
            self.counts["pruned_"+entry["pruning"]] += 1
        self.progress({"event": "search_record", **entry})

    def candidate(self, plan, threshold, deadline, metadata):
        if perf_counter() >= deadline:
            raise SearchStopped("time_budget")
        started = perf_counter()
        record = dict(scope="complete_plan", **metadata, threshold=list(threshold))
        result = state = None
        try:
            try:
                state = PlanState(self.context, plan)
            except CycleError as error:
                record.update(status="INVALID", reason=str(error))
                return None
            record["plan_hash"] = state.signature
            result = self.evaluator.cached(state)
            if result is not None:
                record.update(status="EXACT", cache_hit=True, objective=list(objective(result)))
                return state, result
            b0 = bounds.basic(state)
            record["b0"] = {k: b0[k] for k in ("time", "movement")}
            if "b0" in self.options.pruning and bounds.cannot_improve(b0["time"], b0["movement"], threshold):
                record.update(status="PRUNED", pruning="b0")
                return None
            if perf_counter() >= deadline:
                raise SearchStopped("time_budget_before_preparation")
            before = perf_counter()
            self.progress({"event": "candidate_phase", "phase": "preparation", "plan_hash": state.signature})
            prepared = self.evaluator.prepare(state)
            record["preparation_seconds"] = perf_counter()-before
            before = perf_counter()
            b1 = bounds.expanded(state, prepared, b0)
            record["b1"] = {k: b1[k] for k in ("time", "movement")}
            record["bounds_seconds"] = perf_counter()-before
            if "b1" in self.options.pruning and bounds.cannot_improve(b1["time"], b1["movement"], threshold):
                record.update(status="PRUNED", pruning="b1")
                return None
            violation = bounds.ddr_violation(b1, threshold[0], self.options.ddr_endpoints) if "ddr" in self.options.pruning else None
            if violation:
                record.update(status="PRUNED", pruning="ddr", certificate=violation)
                return None
            if perf_counter() >= deadline:
                raise SearchStopped("time_budget_before_evaluation")
            before = perf_counter()
            self.progress({"event": "candidate_phase", "phase": "official_simulation", "plan_hash": state.signature})
            result = self.evaluator.evaluate(state, prepared)
            record.update(status="EXACT", objective=list(objective(result)), evaluation_seconds=perf_counter()-before)
            return state, result
        except official.SCHEDULING_ERRORS as error:
            # An iteration guard is a resource limit, not an infeasibility proof.
            resource = any(s in str(error).lower() for s in ("max_iter", "iteration limit", "iteration budget"))
            record.update(status="INCOMPLETE" if resource else "INVALID", reason=str(error), error_type=type(error).__name__)
            if resource:
                raise SearchStopped("official_iteration_budget") from error
            return None
        except SearchStopped as error:
            record.update(status="INCOMPLETE", reason=str(error))
            raise
        finally:
            if "status" in record:
                record["elapsed_seconds"] = perf_counter()-started
                self.record(record)

    def domain(self, base, current_result, domain, threshold, deadline):
        started = perf_counter()
        deadline = min(deadline, started+self.options.region_seconds)
        initial_calls = self.evaluator.statistics["evaluations"]
        initial_budget_calls = self.evaluator.statistics["budget_evaluations"]
        old = set(domain["old_task_ids"])
        fragments = [set(p) for p in domain["fragments"]]
        outside_map = {v: ("outside", j) for v, j in base.mapping.items() if j not in old}
        outside_cores = {("outside", j): k for j, k in base.core.items() if j not in old}
        best = None
        incumbent_in_domain = (all(any(p <= base.nodes[j] for j in old) for p in fragments)
                               if domain["partition_mode"] == "repartition" else
                               {frozenset(p) for p in fragments} == {frozenset(base.nodes[j]) for j in old})
        if incumbent_in_domain:
            best = (base, current_result)
        nodes = leaves = 0
        complete = True
        stop_reason = None
        self.record({"event": "domain_start", "domain": domain})

        def limit():
            if perf_counter() >= deadline:
                raise SearchStopped("region_or_layer_time_budget")
            if self.evaluator.statistics["budget_evaluations"]-initial_budget_calls >= self.options.region_evaluations:
                raise SearchStopped("region_evaluation_budget")

        def cutoff():
            return min(threshold, objective(best[1])) if best else threshold

        def preds_for(mapping):
            preds = {j: set() for j in mapping.values()}
            for v, j in mapping.items():
                for p in self.context.index.preds[v]:
                    if p in mapping and mapping[p] != j:
                        preds[j].add(mapping[p])
            return preds

        def partial_ok(mapping, cores, branch):
            nonlocal nodes
            limit()
            nodes += 1
            preds = preds_for(mapping)
            # Outside-core order persists even if some unknown fragments later
            # join existing labels. Unknown fragments are never temporary Tasks.
            for order in base.orders:
                fixed = [("outside", j) for j in order if j not in old]
                for a, b in zip(fixed, fixed[1:]):
                    if a in preds and b in preds:
                        preds[b].add(a)
            try:
                topological(preds)
            except CycleError:
                self.record(dict(status="PRUNED", scope="branch_completions", pruning="cycle",
                                 domain_id=domain["id"], branch=branch))
                return False
            if "partial" in self.options.pruning:
                bound = bounds.partial(self.context, mapping, cores, len(base.orders))
                if bounds.cannot_improve(bound["time"], bound["movement"], cutoff()):
                    self.record(dict(status="PRUNED", scope="branch_completions", pruning="partial",
                                     domain_id=domain["id"], branch=branch, bound=bound, threshold=list(cutoff())))
                    return False
            return True

        def schedule(groups, labels):
            nonlocal best, leaves
            outside, new = canonical_groups(base, domain, groups)
            mapping = {v: j for j, ns in {**outside, **new}.items() for v in ns}
            data_preds = preds_for(mapping)
            try:
                topological(data_preds)
            except CycleError:
                self.record(dict(status="PRUNED", scope="branch_completions", pruning="cycle",
                                 domain_id=domain["id"], branch={"labels": labels}))
                return
            new_ids = sorted(new)
            assigned = {j: base.core[j] for j in outside}

            def allocate(position):
                limit()
                if position != len(new_ids):
                    j = new_ids[position]
                    for k in domain["allowed_cores"]:
                        assigned[j] = k
                        # Full partition is irrevocable here; only assigned cores count.
                        if "partial" in self.options.pruning:
                            bound = bounds.partial(self.context, mapping, assigned, len(base.orders))
                            if bounds.cannot_improve(bound["time"], bound["movement"], cutoff()):
                                self.record(dict(status="PRUNED", scope="branch_completions", pruning="partial",
                                                 domain_id=domain["id"], branch={"labels": labels, "cores": dict(assigned)},
                                                 bound=bound, threshold=list(cutoff())))
                                continue
                        allocate(position+1)
                    assigned.pop(j, None)
                    return
                buckets = {k: [j for j in new_ids if assigned[j] == k] for k in domain["allowed_cores"]}
                preds = {j: set(ps) for j, ps in data_preds.items()}
                for k, order in enumerate(base.orders):
                    fixed = [j for j in order if j not in old]
                    for a, b in zip(fixed, fixed[1:]):
                        preds[b].add(a)
                for k, items in buckets.items():
                    left, right = domain["window_anchors"][str(k)]
                    for j in items:
                        if left is not None:
                            preds[j].add(left)
                        if right is not None:
                            preds[right].add(j)
                try:
                    topological(preds)
                except CycleError:
                    self.record(dict(status="PRUNED", scope="branch_completions", pruning="cycle",
                                     domain_id=domain["id"], branch={"labels": labels, "cores": dict(assigned)}))
                    return
                permutations = {}
                cores = sorted(buckets)

                def arrange(core_index, active_preds):
                    nonlocal best, leaves
                    limit()
                    if core_index == len(cores):
                        leaves += 1
                        plan = build_plan(base, domain, groups, assigned, permutations)
                        answer = self.candidate(plan, cutoff(), deadline, {
                            "domain_id": domain["id"], "labels": labels,
                            "cores": dict(assigned), "window_orders": {k: list(v) for k, v in permutations.items()}})
                        if answer and (best is None or objective(answer[1]) < objective(best[1])):
                            best = answer
                        return
                    k = cores[core_index]

                    def permute(prefix, remaining, pp):
                        limit()
                        if not remaining:
                            permutations[k] = prefix
                            arrange(core_index+1, pp)
                            return
                        for j in sorted(remaining):
                            if (closure({j}, pp)-{j}) & remaining:
                                continue
                            updated = {v: set(ps) for v, ps in pp.items()}
                            if prefix:
                                updated[j].add(prefix[-1])
                            try:
                                topological(updated)
                            except CycleError:
                                continue
                            permute([*prefix, j], remaining-{j}, updated)
                    permute([], set(buckets[k]), active_preds)
                arrange(0, preds)
            allocate(0)

        def partition(position, groups, labels, mapping):
            limit()
            if position == len(fragments):
                schedule(groups, labels)
                return
            for label in range(len(groups)+1):
                new_groups = [set(g) for g in groups]
                if label == len(groups):
                    new_groups.append(set())
                new_groups[label].update(fragments[position])
                new_mapping = dict(mapping)
                new_mapping.update({v: ("inside", label) for v in fragments[position]})
                branch = {"labels": [*labels, label]}
                if partial_ok(new_mapping, outside_cores, branch):
                    partition(position+1, new_groups, branch["labels"], new_mapping)

        try:
            if domain["partition_mode"] == "fixed":
                schedule(fragments, list(range(len(fragments))))
            else:
                partition(0, [], [], outside_map)
        except SearchStopped as error:
            complete, stop_reason = False, str(error)
        status = ("INCOMPLETE" if not complete else "OPTIMAL_IN_DOMAIN"
                  if best and objective(best[1]) <= threshold else "NO_IMPROVEMENT_IN_DOMAIN")
        report = dict(event="domain_end", domain_id=domain["id"], status=status,
                      stop_reason=stop_reason, nodes=nodes, leaves=leaves,
                      evaluations=self.evaluator.statistics["evaluations"]-initial_calls,
                      budget_evaluations=self.evaluator.statistics["budget_evaluations"]-initial_budget_calls,
                      elapsed_seconds=perf_counter()-started,
                      best_objective=list(objective(best[1])) if best else None,
                      incumbent_in_domain=incumbent_in_domain)
        self.record(report)
        return best, report


def improve(graph, initial_plan, settings, *, options=None, progress=None, expected_initial=None):
    """Return an officially evaluated plan; never writes files or reruns stage 1."""
    options = options or SearchOptions()
    progress = progress or (lambda event: None)
    started = perf_counter()
    context = Context(graph, settings)
    search = Search(context, options, progress)
    current = PlanState(context, initial_plan)
    progress({"event": "initial_evaluation"})
    result = search.evaluator.evaluate(current)
    if expected_initial is not None:
        def clean(value):
            return json.loads(json.dumps({k: v for k, v in value.items() if k not in ("input_graph", "input_plan")}))
        if clean(result) != clean(expected_initial):
            raise ValueError("initial result differs from archived official result")
    initial_result = result
    initial_seconds = perf_counter()-started
    search_started = perf_counter()
    deadline = search_started+options.search_seconds
    history = []
    version = 0
    status = "INCOMPLETE"
    while perf_counter() < deadline:
        prepared = search.evaluator.prepare(current)
        before = perf_counter()
        diagnosis = diagnose(current, result, prepared, options)
        search.record(dict(event="diagnosis", version=version, base_hash=current.signature,
                           diagnosis=diagnosis, elapsed_seconds=perf_counter()-before))
        round_started = perf_counter()
        remaining = max(0, deadline-round_started)
        accepted = False
        any_incomplete = False
        for layer in (1, 2, 3):
            layer_deadline = min(deadline, round_started+remaining*sum(options.layer_shares[:layer]))
            layer_started = perf_counter()
            progress({"event": "layer_start", "version": version, "layer": layer})
            best = (current, result)
            exhausted = True
            deferred = []
            try:
                if layer == 1:
                    for plan, description in layer1(current, diagnosis, result):
                        answer = search.candidate(plan, objective(best[1]), layer_deadline,
                                                  {"layer": layer, "version": version, **description})
                        if answer and objective(answer[1]) < objective(best[1]):
                            best = answer
                else:
                    for domain in domains(current, diagnosis, result, prepared, options, layer, deferred):
                        if perf_counter() >= layer_deadline:
                            raise SearchStopped("layer_time_budget")
                        answer, report = search.domain(current, result, domain, objective(best[1]), layer_deadline)
                        if report["status"] == "INCOMPLETE":
                            exhausted = False
                        if answer and objective(answer[1]) < objective(best[1]):
                            best = answer
                            if layer == 3:
                                break
            except SearchStopped:
                exhausted = False
            any_incomplete |= not exhausted
            search.record(dict(event="layer_end", version=version, layer=layer,
                               status=("LAYER_IMPROVED" if objective(best[1]) < objective(result) else
                                       "LAYER_EXHAUSTED" if exhausted else "INCOMPLETE"),
                               candidate_set_exhausted=exhausted if layer != 3 or objective(best[1]) >= objective(result) else False,
                               deferred=deferred, elapsed_seconds=perf_counter()-layer_started,
                               best_objective=list(objective(best[1]))))
            if objective(best[1]) < objective(result):
                old = objective(result)
                current, result = best
                entry = dict(version=version+1, layer=layer, before=list(old), after=list(objective(result)),
                             delta_cycles=old[0]-objective(result)[0],
                             delta_added_bytes=old[1]-objective(result)[1], plan=current.plan,
                             plan_hash=current.signature, elapsed_seconds=perf_counter()-search_started)
                history.append(entry)
                progress({"event": "accepted", **entry})
                version += 1
                accepted = True
                break
        if not accepted:
            status = "INCOMPLETE" if any_incomplete else "NEIGHBORHOOD_EXHAUSTED"
            break
    if objective(result) > objective(initial_result):
        raise RuntimeError("stage 2 violated the initial-solution performance guarantee")
    statistics = dict(search.counts)
    statistics.update(search.evaluator.statistics, accepted=len(history),
                      initial_evaluation_seconds=initial_seconds,
                      search_seconds=perf_counter()-search_started,
                      elapsed_seconds=perf_counter()-started)
    return OptimizationResult(current.plan, result, initial_result, status, history, search.records, statistics)
