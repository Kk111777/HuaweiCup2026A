"""Shared search coordinator; problem semantics and algorithms are injected.

The search order and thresholds are inherited unchanged from the Q1 controller.
Execution, proposal generation, plan identity and prediction belong to explicit
backend bindings; this module imports no problem implementation.
"""
from collections import defaultdict
from copy import deepcopy
import time
from .budget import SearchBudget
from .contracts import ProblemDefinition
from .configuration import apply_structure_overrides

def search(graph, cores, settings, params, seconds, services, *, problem, seed=None, started=None, constructor=None, budget=None):
    if not isinstance(problem, ProblemDefinition):
        raise TypeError('search requires a complete ProblemDefinition')
    service_problem = getattr(services, 'problem', problem)
    if service_problem.metadata() != problem.metadata():
        raise ValueError('search and evaluation services must use the same problem and backend version')
    semantics, prediction, proposals = problem.semantics, problem.prediction, problem.proposals
    validate_plan, plan_digest = semantics.validate, semantics.key
    objective, state_signature = semantics.objective, semantics.state_signature
    create_estimator, create_policy = prediction.create_estimator, prediction.create_policy
    create_cost_model = prediction.create_cost_model
    recognize, plan_complexity = prediction.recognize, prediction.complexity
    cheap_plan_proxy = prediction.proxy
    CHEAP_PROXY_MODE, CHEAP_PROXY_NOTE = prediction.proxy_mode, prediction.proxy_note
    initial_candidates, local_candidates = proposals.initial, proposals.local
    rebuild_regions, schedule = proposals.rebuild, proposals.schedule
    _retain_states, control_profile = proposals.retain, proposals.control_profile
    requested_params = deepcopy(params)
    started = time.perf_counter() if started is None else started
    budget = SearchBudget(seconds, started) if budget is None else budget
    shared_case = getattr(budget, 'shared_case', False)
    services.shared_case = shared_case
    records, seen, accepted, audit = [], set(), [], []
    verified_scores, parents = {}, {}
    invalid_digests, invalid_records = set(), {}
    best_plan = best_result = None
    states = []
    options = params.get("evaluation", {})
    adaptive = options.get("mode") == "adaptive"
    policy = create_policy(seconds, options) if adaptive else None
    model = getattr(services, 'approximation', None)
    calibration_seed = None
    staged = None
    screened_since_check = 0
    final_check = {"status": "not_needed"}
    phase_seconds = defaultdict(float)
    best_official_state = None
    in_flight = None
    last_official_at = budget.elapsed()
    calibration_interval_seconds = min(60., seconds*.15)
    features = getattr(graph, 'structural_features', None) or recognize(graph, settings)
    seed_is_official = seed is not None and seed[1].get('evaluation_source') != 'approximate'
    cost_model = getattr(services, 'cost_model', None)
    if cost_model is None:
        cost_model = create_cost_model(graph, seed[0] if seed_is_official else None,
                                       getattr(services,'baseline_seconds',None))
        services.cost_model = cost_model
    controls = control_profile(features, cores, seconds) if shared_case else None
    if shared_case:
        params = dict(params)
        for name in proposals.controlled_parameters:
            params[name] = controls[name]
        calibration_interval_seconds = controls['calibration_interval_seconds']
    if 'structure_overrides' in params:
        params = apply_structure_overrides(params, problem.proposals.controlled_parameters)
    generation_costs = getattr(services, 'generation_costs', defaultdict(list))

    def window_remaining():
        return max(0., (budget.extension if budget.extended else budget.target)-budget.elapsed())

    def can_generate(phase):
        if not shared_case:
            return can_explore()
        observed = generation_costs.get(phase, [])
        estimate = (max(observed[-3:])*1.2 if observed else
                    controls['generation_seconds_prior'][phase])
        # Retain a screening/calibration window after candidate generation.
        return (services.can_continue() and budget.can_start(estimate+1.) and
                estimate < window_remaining()*.6)

    def generation_observed(phase, elapsed):
        phase_seconds[phase] += elapsed
        if elapsed > 0:
            generation_costs[phase].append(elapsed)

    def can_construct():
        # The shared constructor checks its own cooperative deadline. It can
        # start without first completing an unbounded graph-size estimate.
        return (can_explore() and window_remaining() > 3.) if shared_case else can_explore()

    def construct(fraction=.6):
        phase_started = time.perf_counter()
        proposal = (constructor.next(seconds=max(0.,window_remaining()*fraction))
                    if shared_case else constructor.next())
        generation_observed('construction',time.perf_counter()-phase_started)
        return proposal
    complexities = {}
    def cost_of(plan):
        key = plan_digest(plan)
        if key not in complexities:
            complexities[key] = plan_complexity(graph, plan)
        return cost_model.predict(complexities[key])

    def can_explore():
        # Approximation remains useful when a previous expensive official call
        # would not fit. Admission is checked against each proposed partition.
        return services.can_continue() and budget.can_start(.01)

    if seed is not None:
        best_plan, best_result = seed
        states = [{"plan": best_plan, "result": best_result, "digest": plan_digest(best_plan),
                   "objective": objective(best_result),
                   "signature": state_signature(graph, best_plan, cores)}]
        if seed_is_official:
            best_official_state = states[0]
    best_generated = best_initial = None
    def save_search():
        counts = defaultdict(lambda: {"generated": 0, "shortlist": 0, "evaluated": 0, "approximated": 0, "failed": 0})
        for entry in audit:
            counts[entry["action"]][entry["stage"]] += 1
        for entry in records:
            counts[entry["action"]]["approximated" if entry.get('evaluation_source')=='approximate' else "evaluated"] += 1
            counts[entry["action"]]["failed"] += entry["status"] == "failed"
        services.save({"records": records, "accepted": accepted,
                   **problem.metadata(), "requested_params": requested_params,
                   "effective_params": deepcopy(params),
                   "candidate_audit": audit, "action_counts": dict(counts),
                   "invalid_plans": dict(invalid_records),
                   "search_seconds": time.perf_counter() - started, "budget_seconds": seconds,
                   **budget.metadata(),
                   "evaluation_mode": "adaptive" if adaptive else "official",
                   "evaluation_policy": policy.metadata() if policy else None,
                   "approximate_stats": dict(model.stats) if model else {},
                   "graph_features": features, "official_cost_model": cost_model.metadata(),
                   "structure_control": controls,
                   "generation_seconds_observed": dict(generation_costs),
                   "incumbent_source": best_result.get('evaluation_source','official') if best_result else None,
                   "in_flight_evaluation": in_flight,
                   "calibration_interval_seconds": calibration_interval_seconds,
                   "phase_seconds": dict(phase_seconds), "final_candidate_check": final_check,
                   "staged_candidate": ({"action": staged[0], "estimated_cycles": staged[3],
                                         "plan_digest": plan_digest(staged[1])} if staged else None),
                   "proxy_mode": CHEAP_PROXY_MODE,
                   "search_states": [{"digest": s["digest"], "objective": list(s["objective"])}
                                     for s in states],
                   "best_generated_objective": best_generated,
                   "best_initial_objective": best_initial,
                   "granularity_weight": 0, "weight_note": "structural alternatives and family quotas; no size reward"})

    def state_of(plan, value, action):
        return dict(plan=plan,result=value,digest=plan_digest(plan),objective=objective(value),
                    signature=state_signature(graph,plan,cores),action=action)

    def accept_approximate(name, plan, estimated, detail):
        """Approximate and official objectives may both replace the incumbent."""
        nonlocal best_plan,best_result,states
        digest = plan_digest(plan)
        if digest in invalid_digests:
            return False
        record = dict(index=len(records),action=name,status='approximate',evaluation_source='approximate',
                      plan_digest=digest,parent_plan_digest=parents.get(digest),accepted=False,
                      estimated_makespan_cycles=estimated,seconds=detail['seconds'],approximation=detail)
        # Never replace a known exact score for this same plan with an estimate.
        measured = digest in verified_scores or (best_official_state and best_official_state['digest']==digest)
        if not measured and (best_result is None or estimated <= best_result['makespan']*1.1):
            value = model.result(plan,estimated,detail)
            states = _retain_states([s for s in states if s['digest']!=digest],
                                    state_of(plan,value,name),params.get('search_states',6))
            if best_result is None or objective(value)<objective(best_result):
                budget.observe(best_result,value,record)
                best_plan,best_result = plan,value
                record['accepted']=True
                accepted.append(dict(index=record['index'],action=name,objective=list(objective(value)),
                                     evaluation_source='approximate'))
                services.checkpoint(plan,value)
        records.append(record)
        return record['accepted']

    def reconcile(digest, value, name, plan):
        """Official correction can worsen a score: reprice estimates and rerank."""
        nonlocal states,best_plan,best_result,best_official_state
        candidates = [s for s in states if s['digest'] not in invalid_digests and
                      (value is None or s['digest']!=digest)]
        if best_official_state is not None and best_official_state['digest'] in invalid_digests:
            best_official_state = None
        if value is not None:
            exact = state_of(plan,value,name)
            candidates.append(exact)
            if best_official_state is None or exact['objective']<best_official_state['objective']:
                best_official_state=exact
        if best_official_state is not None:
            candidates=[s for s in candidates if s['digest']!=best_official_state['digest']]+[best_official_state]
        refreshed=[]
        for state in candidates:
            if model is not None and state['result'].get('evaluation_source')=='approximate':
                state=state_of(state['plan'],model.result(state['plan']),state.get('action','recalibrated'))
            refreshed.append(state)
        states=[]
        for state in sorted(refreshed,key=lambda s:(s['objective'],s['digest'])):
            states=_retain_states(states,state,params.get('search_states',6))
        if states:
            winner=states[0]
            best_plan,best_result=winner['plan'],winner['result']
            # Write corrections even when the corrected score is worse.
            services.checkpoint(best_plan,best_result)
        else:
            best_plan = best_result = None

    def verify(name, plan, proxy, *, estimate_cycles=None, reason="direct"):
        nonlocal best_plan, best_result, states, best_generated, best_initial, calibration_seed, in_flight, last_official_at, staged
        digest = plan_digest(plan)
        if digest in invalid_digests:
            return False
        estimate = max(budget.costs[-3:], default=0)
        if not services.can_continue():
            return False
        initial_probe = policy is not None and policy.calls == 0
        predicted_cost = cost_of(plan)
        if policy:
            remaining = budget.hard - budget.elapsed() - .05
            reserve = 1.
            soft_remaining = (budget.extension if budget.extended else budget.target) - budget.elapsed()
            available = remaining if initial_probe and reason=='bootstrap_completion' and not shared_case else soft_remaining
            if remaining <= 0 or available < predicted_cost + reserve:
                return False
            if shared_case and not budget.can_start(predicted_cost+reserve):
                return False
        elif not budget.can_start(estimate):
            return False
        validate_plan(graph, plan, cores)
        index = len(records)
        cap = budget.candidate_cap()
        if policy:
            # 首次划分尚无多核实测成本：允许在保护截止前完成，不能用虚构的短超时探测。
            cap = remaining if initial_probe and not shared_case else min(remaining, max(5., 2 * predicted_cost))
            if shared_case or (not initial_probe and reason != "final"):
                cap = min(cap, max(.001, soft_remaining - reserve))
        previous_result = best_result
        in_flight = dict(action=name,plan_digest=digest,estimated_cycles=estimate_cycles,
                        predicted_official_seconds=predicted_cost,reason=reason)
        save_search()
        value, record = services.evaluate(plan, cap)
        in_flight = None
        last_official_at = budget.elapsed()
        complexity = complexities[digest]
        record.update(predicted_official_seconds=predicted_cost, evaluator_complexity=complexity,
                      cost_prediction_source=cost_model.metadata()['source'])
        cost_model.observe(complexity, record)
        if policy:
            policy.observe(record, estimate_cycles, value["makespan"] if value else None)
        phase_seconds["official_evaluation"] += record.get("seconds", 0)
        budget.observe(previous_result, value, record)
        record.update(validation_reason="initial_probe" if initial_probe else reason,
                      requested_reason=reason,
                      estimated_makespan_cycles=estimate_cycles)
        if estimate_cycles is not None and value:
            record["estimation_error_fraction"] = estimate_cycles / value["makespan"] - 1
        record.update(index=index, action=name, proxy_cycles=proxy,
                      evaluation_source='official',
                      proxy_note=CHEAP_PROXY_NOTE, proxy_mode=CHEAP_PROXY_MODE, plan_digest=digest,
                      parent_plan_digest=parents.get(digest))
        # A completed official scheduling failure is conclusive. A timeout is
        # only missing evidence and must not blacklist an otherwise legal plan.
        rejected = value is None and record['status']=='failed' and not record.get('timed_out',False)
        record['plan_rejected'] = rejected
        if rejected:
            invalid_digests.add(digest)
            invalid_records[digest] = dict(index=index,action=name,error=record.get('error'),
                                            status=record['status'],seconds=record.get('seconds',0))
            verified_scores.pop(digest,None)
            services.reject(plan)
            if staged is not None and plan_digest(staged[1])==digest:
                staged = None
        if value is not None and model is not None:
            calibration_started = time.perf_counter()
            cached = model.plans.get(digest)
            if cached is not None:
                record["uncalibrated_makespan_cycles"] = cached["raw_cycles"]
                record["uncalibrated_error_fraction"] = cached["raw_cycles"] / value["makespan"] - 1
            model.observe(plan, value)
            phase_seconds["approximate_evaluation"] += time.perf_counter() - calibration_started
        elif value is not None and policy:
            calibration_seed = (plan, value)
        improved = value is not None and (previous_result is None or objective(value) < objective(previous_result))
        record["accepted"] = improved
        records.append(record)
        if value is not None:
            verified_scores[digest] = {"makespan": value["makespan"]}
            candidate_objective = list(objective(value))
            if name not in ("same_core_incumbent", "fewer_core_fallback"):
                if best_generated is None or candidate_objective < best_generated:
                    best_generated = candidate_objective
                if name in initial_names and (best_initial is None or candidate_objective < best_initial):
                    best_initial = candidate_objective
        reconcile(digest,value,name,plan)
        record['accepted'] = improved and best_plan is not None and plan_digest(best_plan)==digest
        record['incumbent_after_correction'] = (dict(plan_digest=plan_digest(best_plan),
            makespan=best_result['makespan'],evaluation_source=best_result.get('evaluation_source','official'))
            if best_plan is not None else None)
        if record['accepted']:
            accepted.append({"index": index, "action": name, "objective": list(objective(value)),
                             "evaluation_source":"official"})
        if constructor is not None:
            constructor.observe(name, value)
        save_search()
        services.emit({"event": "candidate", "action": name, "status": record["status"],
                       "makespan": value["makespan"] if value else None, "improved": improved})
        return improved

    def flush_staged(reason):
        nonlocal staged, screened_since_check, final_check
        if best_result is not None and best_result.get('evaluation_source')=='approximate':
            staged=('calibration_incumbent',best_plan,cheap_plan_proxy(graph,best_plan,settings),best_result['makespan'])
        if staged is not None and plan_digest(staged[1]) in invalid_digests:
            staged = None
        if staged is None:
            return False
        name, plan, proxy, estimated = staged
        before = len(records)
        improved = verify(name, plan, proxy, estimate_cycles=estimated, reason=reason)
        if len(records) > before:
            screened_since_check = 0
            staged = None
            if reason == "final":
                final_check = {"status": records[-1]["status"], "index": records[-1]["index"],
                               "accepted": improved, "seconds": records[-1].get("seconds", 0)}
        elif reason == "final":
            final_check = {"status": "insufficient_time", "retained_source": best_result.get('evaluation_source','official') if best_result else None}
        save_search()
        return improved

    def attempt(name, plan, proxy):
        nonlocal model, staged, screened_since_check, calibration_seed
        digest = plan_digest(plan)
        if digest in invalid_digests:
            return False
        if digest in seen:
            if constructor is not None and digest in verified_scores:
                constructor.observe(name, verified_scores[digest])
            return False
        if not can_explore():
            return False
        validate_plan(graph, plan, cores)
        seen.add(digest)
        if not policy:
            return verify(name,plan,proxy)
        if model is None:
            model = create_estimator(graph, settings, options)
            services.approximation = model
            if calibration_seed is not None:
                calibration_started = time.perf_counter()
                model.observe(*calibration_seed)
                phase_seconds["approximate_evaluation"] += time.perf_counter() - calibration_started
                calibration_seed = None
        reference = plan_digest(best_plan) if best_plan is not None else None
        if reference is not None and reference not in model.plans:
            model.estimate(best_plan)
        estimated, detail = model.estimate(plan, reference)
        phase_seconds["approximate_evaluation"] += detail["seconds"]
        improved = accept_approximate(name,plan,estimated,detail)
        if cost_of(plan) <= policy.threshold and staged is None:
            improved = verify(name,plan,proxy,estimate_cycles=estimated) or improved
            save_search()
            return improved
        # Alternate point ranking with optimistic ranking: an uncertain candidate
        # must not be permanently rejected just because its point estimate is high.
        def rank(p, cycles):
            entry = model.plans.get(plan_digest(p), {})
            margin = .45 if entry.get('pressure_risk') else .08
            optimistic = policy.calls % 2 == 0
            cost = cost_of(p)
            remaining = (budget.extension if budget.extended else budget.target)-budget.elapsed()
            return (cost>remaining, cycles*(1-margin if optimistic else 1), cost)
        if staged is None or rank(plan, estimated) < rank(staged[1], staged[3]):
            staged = (name, plan, proxy, estimated)
        screened_since_check += 1
        save_search()
        if screened_since_check >= policy.interval or budget.elapsed()-last_official_at >= calibration_interval_seconds:
            return flush_staged("periodic") or improved
        return improved

    # A structural seed may be approximate: it is usable but must never train
    # its own residual model or masquerade as a certified fallback.
    initial_names = set()
    if policy:
        if model is None:
            model = create_estimator(graph, settings, options)
            services.approximation = model
        if seed_is_official:
            calibration_started = time.perf_counter()
            model.observe(*seed)
            phase_seconds['approximate_evaluation'] += time.perf_counter()-calibration_started
        if constructor is not None:
            constructor.approximation = model
    if shared_case and seed is not None and not seed_is_official:
        # Start with the actual multi-core proposal, not a costly single-core
        # run. The first official call obeys this core's fair budget slice.
        staged = ('structural_seed', best_plan, cheap_plan_proxy(graph,best_plan,settings),best_result['makespan'])
        if cost_of(best_plan) < window_remaining()*.4:
            flush_staged('structural_seed')
    shared_construction_started = False
    if shared_case and constructor is not None and can_construct():
        # Growth remains the first constructive proposal family, after the
        # cheap structural checkpoint. Bound it so other cores and refinements
        # do not wait behind a single unbounded growth traversal.
        shared_construction_started = True
        candidate = construct(.4)
        if candidate is not None:
            initial_names.add(candidate[0])
            attempt(*candidate)
    # Large graphs are screened before committing to a possibly expensive first
    # official evaluation. Every proposal still enters this one controller.
    bootstrap_candidates = []
    if not shared_case and policy and features['operations'] >= 2000 and params.get('enable_initial_candidates',True) and budget.elapsed()<budget.hard:
        phase_started = time.perf_counter()
        bootstrap_candidates = initial_candidates(graph, cores, settings, params, audit)
        initial_names.update(c[0] for c in bootstrap_candidates)
        phase_seconds['initial_generation'] += time.perf_counter()-phase_started
    # One bootstrap proposal enters the SAME state pool, then all operators refine it.
    # Baseline + first constructive proposal form the bootstrap job started at
    # solver entry. A costly baseline may consume the soft target; still finish
    # this first useful multicore attempt within the absolute protection limit.
    if not shared_case and constructor is not None and services.can_continue() and budget.elapsed() < budget.hard:
        phase_started = time.perf_counter()
        candidate = constructor.next()
        phase_seconds["construction"] += time.perf_counter() - phase_started
        if candidate is not None:
            bootstrap_estimate = None
            initial_names.add(candidate[0])
            if bootstrap_candidates:
                choices = [candidate] + bootstrap_candidates
                ranked = []
                for proposal in choices:
                    estimate_cycles, detail = model.estimate(proposal[1])
                    phase_seconds['approximate_evaluation'] += detail['seconds']
                    accept_approximate(proposal[0],proposal[1],estimate_cycles,detail)
                    cost = cost_of(proposal[1])
                    # Risk-adjusted score protects against unmodelled Spill. The
                    # factor is a preference, not a claimed cycle upper bound.
                    score = estimate_cycles*(1.35 if detail['pressure_risk_tasks'] else 1)
                    ranked.append((score,cost,proposal,estimate_cycles))
                affordable = [r for r in ranked if r[1] < budget.hard-budget.elapsed()]
                pool = affordable or ranked
                best_score = min(r[0] for r in pool)
                shortlist = [r for r in pool if r[0] <= best_score*1.02]
                chosen = min(shortlist,key=lambda r:(r[1],r[0],plan_digest(r[2][1])))
                candidate = chosen[2]
                bootstrap_estimate = chosen[3]
                bootstrap_candidates = [p for p in choices if plan_digest(p[1]) != plan_digest(candidate[1])]
                audit.append(dict(action=candidate[0],stage='shortlist',reason='structure_aware_bootstrap',
                                  estimated_cycles=chosen[3],predicted_official_seconds=chosen[1]))
            elif model is not None:
                bootstrap_estimate, detail = model.estimate(candidate[1])
                accept_approximate(candidate[0],candidate[1],bootstrap_estimate,detail)
            # Construction and its first official verification are one started job.
            # A slow construction may cross the soft target; finishing it is allowed
            # by the existing hard protection limit. Do not discard its useful work.
            seen.add(plan_digest(candidate[1]))
            verify(*candidate, estimate_cycles=bootstrap_estimate, reason="bootstrap_completion")

    phase_started = time.perf_counter()
    services.emit({"event":"phase", "phase":"initial_generation"})
    generate_initial = not bootstrap_candidates and can_generate('initial_generation') and params.get("enable_initial_candidates", True)
    candidates = bootstrap_candidates or (initial_candidates(graph, cores, settings, params, audit) if generate_initial else [])
    if generate_initial:
        generation_observed('initial_generation', time.perf_counter() - phase_started)
    initial_names.update(c[0] for c in candidates)
    pending = list(candidates)
    initial_started = time.perf_counter()
    for index, candidate in enumerate(candidates):
        if best_result is not None and (index >= params["max_initial_evaluations_per_core"]
                                        or (time.perf_counter() - initial_started > seconds * .45
                                            and (not adaptive or index >= 4))):
            break
        attempt(*candidate)
        pending.pop(0)
    stale = 0
    exhausted = set()
    for round_no in range(params["max_rounds"]):
        if not can_explore():
            break
        if best_result is None:
            # A rejected seed does not end construction: another partition can
            # restore a valid state, without consulting the rejected score.
            rescue = pending.pop(0) if pending else (
                construct() if constructor is not None and can_construct() else None)
            if rescue is not None:
                attempt(*rescue)
            if best_result is None:
                continue
        # 停滞触发换起点、扩大区域；不因单一起点连续不改善而结束整个搜索。
        restart = stale // max(1, params["no_improvement_rounds"])
        state = states[(restart + round_no // 3) % len(states)] if restart else states[0]
        scale = min(2, restart)
        context = (state["digest"], scale)
        proposals = []
        if (constructor is not None and can_construct() and
                ((round_no == 0 and not shared_construction_started) or (stale > 0 and stale % 3 == 0))):
            proposal = construct()
            if proposal is not None:
                pending.insert(0, proposal)
        # A construction already in flight may finish after the exploration
        # window. Do not start an expensive local-generation phase after it.
        if not can_explore():
            break
        phase_started = time.perf_counter()
        generate_local = context not in exhausted and can_generate('local_generation')
        if generate_local:
            services.emit({"event":"phase", "phase":"local_generation", "round":round_no})
            proposals, _ = (local_candidates(graph, state["plan"], state["result"], settings, params)
                            if params.get("enable_local", True) else ([], 0))
            rebuilt = []
            regions = (rebuild_regions(graph, validate_plan(graph, state["plan"]),
                                       state["result"], cores, scale) if can_explore() and params.get("enable_region_rebuild", True) else [])
            for blocks in regions:
                if not can_explore() or len(rebuilt) >= 12:
                    break
                new_plan, proxy = schedule(graph, blocks, cores, settings)
                rebuilt.append(("region_rebuild", new_plan, proxy))
            # 同一重构动作保留不同粗细候选，其后按官方评估次数轮换。
            proposals = rebuilt + proposals
            for _, proposal, _ in proposals:
                parents.setdefault(plan_digest(proposal), state["digest"])
        if generate_local:
            generation_observed('local_generation', time.perf_counter() - phase_started)
        while pending and plan_digest(pending[0][1]) in seen:
            pending.pop(0)
        if pending:
            proposals.append(pending[0])
        for name, _, proxy in proposals:
            audit.append({"action": name, "stage": "shortlist", "round": round_no,
                          "proxy_cycles": proxy, "proxy_mode": CHEAP_PROXY_MODE})
        improved, evaluated = False, 0
        # 每轮按累计实际评估次数选择家族，不以shortlist保留冒充实际评估配额。
        eligible = [p for p in proposals if plan_digest(p[1]) not in seen]
        if not eligible:
            exhausted.add(context)
        by_kind = defaultdict(list)
        for proposal in eligible:
            by_kind[proposal[0]].append(proposal)
        attempts_by_kind = {k: sum(r["action"] == k for r in records) for k in by_kind}
        ordered = []
        while by_kind:
            kind = min(by_kind, key=lambda k: (attempts_by_kind[k], by_kind[k][0][2], k))
            ordered.append(by_kind[kind].pop(0))
            attempts_by_kind[kind] += 1
            if not by_kind[kind]:
                del by_kind[kind]
        for name, plan, proxy in ordered:
            improved |= attempt(name, plan, proxy)
            evaluated += 1
            if evaluated >= params["official_per_round"] or not can_explore():
                break
        stale = 0 if improved else stale + 1
        if not evaluated:
            if not pending and all((s["digest"], k) in exhausted for s in states for k in range(3)):
                break
    if adaptive:
        flush_staged("final")
    if best_result is None:
        final_check = dict(status='no_valid_plan',invalid_plans=len(invalid_digests))
        save_search()
        raise ValueError(f"no valid plan within {seconds:.1f}s; officially rejected {len(invalid_digests)} plans")
    search_seconds = time.perf_counter() - started
    budget.can_start()
    save_search()
    return best_plan, best_result, services.history
