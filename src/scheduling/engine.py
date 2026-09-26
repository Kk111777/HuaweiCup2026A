"""Shared search coordinator; problem semantics and algorithms are injected.

Search rounds reserve a closing official evaluation; only certified results are returned.
Execution, proposal generation, plan identity and prediction belong to explicit
backend bindings; this module imports no problem implementation.
"""
from collections import defaultdict
from copy import deepcopy
import time
from .budget import SearchBudget
from .contracts import ProblemDefinition
from .configuration import apply_structure_overrides, late_official_gain_fraction

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
    round_admissions = []
    exploration_closed = False
    minimum_official_checks = options.get('minimum_official_checks', 2)
    if type(minimum_official_checks) is not int or minimum_official_checks not in (1, 2):
        raise ValueError('evaluation.minimum_official_checks must be 1 or 2')
    minimum_completion_attempted = False
    minimum_grace = []
    minimum_round_grace = []
    late_gain_threshold = late_official_gain_fraction(params)
    late_official_checks = []
    measured_official_costs = {}

    def completed_official_checks():
        extra_seed = int(seed_is_official and seed is not None and plan_digest(seed[0]) not in verified_scores)
        return len(verified_scores) + extra_seed

    def generation_estimate(phase):
        observed = generation_costs.get(phase, [])
        return (max(observed[-3:])*1.2 if observed else
                controls['generation_seconds_prior'][phase] if controls else 1.)

    def screening_estimate():
        recent = [r['seconds'] for r in records if r.get('evaluation_source') == 'approximate'][-6:]
        return max(.05, max(recent, default=.05)) * (policy.interval if policy else 1)

    def closing_reserve():
        # Recompute for the CURRENT partition: a merge can make official work dearer.
        plan = best_plan if best_plan is not None else (staged[1] if staged else None)
        predicted = cost_of(plan) if plan is not None else 0.
        return 1.25 * max(predicted, budget.timeout_floor) + 1.

    def grant_minimum_round(required, phase):
        # Reserve generation AND a closing calibration, once, within the same
        # case extension. No fresh budget is created for this opportunity.
        if (minimum_round_grace or minimum_completion_attempted or
                not 0 < completed_official_checks() < minimum_official_checks):
            return False
        parent = getattr(budget, 'parent', None)
        available = min(budget.extension-budget.elapsed(), budget.hard-budget.elapsed()-.05)
        if parent is not None:
            available = min(available, parent.extension-parent.elapsed(),
                            parent.hard-parent.elapsed()-.05)
        if required >= available or not services.can_continue():
            return False
        budget.extended = True
        if parent is not None and parent.target-parent.elapsed() < required:
            parent.extended = True
        minimum_round_grace.append(dict(phase=phase, required_seconds=required,
            available_seconds=available, note='One complete generation-screening-calibration opportunity.'))
        return True

    def admit_round(phase, *, close_on_failure=True):
        nonlocal exploration_closed
        if exploration_closed or not services.can_continue():
            return False
        generation = generation_estimate(phase)
        screening, reserve = screening_estimate(), closing_reserve()
        required = generation + screening + reserve
        allowed = budget.can_start(required)
        if not allowed:
            allowed = grant_minimum_round(required, phase)
        round_admissions.append(dict(phase=phase, elapsed_seconds=budget.elapsed(),
            generation_seconds=generation, screening_seconds=screening,
            official_reserve_seconds=reserve, required_seconds=required,
            remaining_seconds=window_remaining(), allowed=allowed))
        if not allowed and close_on_failure:
            exploration_closed = True
            budget.stop_reason = 'insufficient_complete_round_time'
        return allowed

    def window_remaining():
        return max(0., (budget.extension if budget.extended else budget.target)-budget.elapsed())

    def can_generate(phase):
        return admit_round(phase)

    def generation_observed(phase, elapsed):
        phase_seconds[phase] += elapsed
        if elapsed > 0:
            generation_costs[phase].append(elapsed)

    def can_construct():
        # The shared constructor checks its own cooperative deadline. It can
        # start without first completing an unbounded graph-size estimate.
        return can_explore() and admit_round('construction', close_on_failure=False)

    def construct(fraction=.6):
        phase_started = time.perf_counter()
        proposal = (constructor.next(seconds=max(0., min(window_remaining()*fraction, window_remaining()-closing_reserve()-screening_estimate())))
                    if shared_case else constructor.next())
        generation_observed('construction',time.perf_counter()-phase_started)
        return proposal
    complexities = {}
    def cost_of(plan):
        key = plan_digest(plan)
        if key not in complexities:
            complexities[key] = plan_complexity(graph, plan)
        if key in measured_official_costs:
            return 1.35 * measured_official_costs[key]
        return cost_model.predict(complexities[key])

    def can_screen():
        # An already generated batch is cheap to screen. Do not let a changing
        # structural official-cost forecast terminate it after its first item.
        if exploration_closed or not services.can_continue():
            return False
        reserve = 1.25 * max(budget.costs[-3:], default=.05) + 1.
        remaining = min(window_remaining(), budget.hard-budget.elapsed()-.05)
        parent = getattr(budget, 'parent', None)
        if parent is not None:
            remaining = min(remaining, parent.hard-parent.elapsed()-.05)
        return remaining > screening_estimate() + reserve

    def can_explore():
        nonlocal exploration_closed
        if exploration_closed or not services.can_continue():
            return False
        # If no certified state exists, bootstrap/recovery still gets a chance.
        required = closing_reserve() + screening_estimate() if best_official_state else .01
        if not budget.can_start(required):
            exploration_closed = True
            budget.stop_reason = 'insufficient_calibration_time'
            return False
        return True

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
                   "round_admissions": round_admissions,
                   "output_policy": "best_official",
                   "minimum_official_checks": minimum_official_checks,
                   "completed_official_checks": completed_official_checks(),
                   "minimum_calibration_grace": minimum_grace,
                   "minimum_round_grace": minimum_round_grace,
                   "late_official_gain_fraction": late_gain_threshold,
                   "late_official_checks": late_official_checks,
                   "best_official": ({"plan_digest": best_official_state['digest'],
                       "objective": list(best_official_state['objective'])} if best_official_state else None),
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
        nonlocal best_plan, best_result, states, best_generated, best_initial, calibration_seed, in_flight, last_official_at, staged, minimum_completion_attempted, exploration_closed
        digest = plan_digest(plan)
        if digest in invalid_digests or late_official_checks:
            return False
        estimate = max(budget.costs[-3:], default=0)
        if not services.can_continue():
            return False
        initial_probe = policy is not None and policy.calls == 0
        predicted_cost = cost_of(plan)
        minimum_attempt = False
        late_entry = None
        if policy:
            remaining = budget.hard - budget.elapsed() - .05
            reserve = 1.
            if shared_case:
                budget.can_start(predicted_cost + reserve)
            soft_remaining = (budget.extension if budget.extended else budget.target) - budget.elapsed()
            available = remaining if initial_probe and reason=='bootstrap_completion' and not shared_case else soft_remaining
            normal_admission = (remaining > 0 and available >= predicted_cost + reserve
                                and (not shared_case or budget.can_start(predicted_cost+reserve)))
            # Give one distinct post-seed proposal a real calibration opportunity.
            # A structural wall-time forecast is advisory, not proof it cannot finish.
            known = digest in verified_scores or (best_official_state and best_official_state['digest']==digest)
            reference_cycles = best_official_state['result']['makespan'] if best_official_state else None
            gain = ((reference_cycles-estimate_cycles)/reference_cycles
                    if reference_cycles and estimate_cycles is not None else None)
            significant = (late_gain_threshold is not None and gain is not None
                           and gain >= late_gain_threshold
                           and not known and reason in {'initial_round_end', 'local_round_end', 'final'})
            if not normal_admission and significant:
                parent = getattr(budget, 'parent', None)
                floor = 1.25 * max(budget.costs[-3:], default=.05) + reserve
                grace_remaining = remaining
                if parent is not None:
                    grace_remaining = min(grace_remaining, parent.extension-parent.elapsed(),
                                          parent.hard-parent.elapsed()-.05)
                if grace_remaining >= floor:
                    late_entry = dict(plan_digest=digest, reason=reason,
                        official_reference_cycles=reference_cycles, estimated_cycles=estimate_cycles,
                        predicted_gain_fraction=gain, threshold=late_gain_threshold,
                        predicted_official_seconds=predicted_cost,
                        observed_cost_floor_seconds=floor, available_seconds=grace_remaining)
                    late_official_checks.append(late_entry)
                    exploration_closed = True
                    budget.extended = True
                    if parent is not None:
                        parent.extended = True
                    budget.stop_reason = 'significant_gain_final_verification'
                    available = soft_remaining = grace_remaining
            needs_second = (0 < completed_official_checks() < minimum_official_checks
                            and not known and not minimum_completion_attempted)
            if not normal_admission and late_entry is None and needs_second:
                parent = getattr(budget, 'parent', None)
                # A low-gain proposal does not get an unbounded cost override
                # just to reach two checks. Only the significant-gain final
                # opportunity above may override the structural forecast.
                floor = max(1.25 * max(budget.costs[-3:], default=.05), predicted_cost) + reserve
                grace_remaining = min(budget.extension-budget.elapsed(), remaining)
                if parent is not None:
                    grace_remaining = min(grace_remaining, parent.extension-parent.elapsed(),
                                          parent.hard-parent.elapsed()-.05)
                if grace_remaining >= floor and services.can_continue():
                    budget.extended = True
                    if parent is not None and parent.target-parent.elapsed() < floor:
                        parent.extended = True
                    available = soft_remaining = grace_remaining
                    minimum_attempt = True
                    minimum_completion_attempted = True
                    minimum_grace.append(dict(plan_digest=digest, reason=reason,
                        predicted_official_seconds=predicted_cost, available_seconds=available,
                        observed_cost_floor_seconds=floor,
                        note='Minimum-check opportunity requires the predicted official cost to fit the extension.'))
            if not normal_admission and not minimum_attempt and late_entry is None:
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
        if value is not None and not record.get('cache_hit'):
            measured_official_costs[digest] = record['seconds']
        if late_entry is not None:
            late_entry.update(status=record['status'], seconds=record.get('seconds', 0),
                official_cycles=value['makespan'] if value else None,
                improved_official=bool(value and objective(value) < best_official_state['objective']))
        if policy:
            policy.observe(record, estimate_cycles, value["makespan"] if value else None)
        phase_seconds["official_evaluation"] += record.get("seconds", 0)
        budget.observe(previous_result, value, record)
        record.update(minimum_calibration_attempt=minimum_attempt,
                      significant_gain_final_attempt=late_entry is not None,
                      validation_reason="initial_probe" if initial_probe else reason,
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
            record['official_makespan_cycles'] = value['makespan']
            record['official_objective'] = list(objective(value))
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
        if plan_digest(plan) in verified_scores:
            staged = None
            screened_since_check = 0
            if reason == 'final':
                final_check = dict(status='already_verified')
            return False
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

    def attempt(name, plan, proxy, *, batch=False):
        nonlocal model, staged, screened_since_check, calibration_seed
        digest = plan_digest(plan)
        if digest in invalid_digests:
            return False
        if digest in seen:
            if constructor is not None and digest in verified_scores:
                constructor.observe(name, verified_scores[digest])
            return False
        if not (can_screen() if batch else can_explore()):
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
        if not batch and cost_of(plan) <= policy.threshold and staged is None:
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
        if not batch and (screened_since_check >= policy.interval or budget.elapsed()-last_official_at >= calibration_interval_seconds):
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
        if budget.can_start(cost_of(best_plan) + 1.):
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
            if adaptive:
                flush_staged("construction_round_end")
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
        batch = adaptive and best_official_state is not None
        if batch and not can_screen():
            break
        attempt(*candidate, batch=batch)
        pending.pop(0)
    if adaptive:
        flush_staged("initial_round_end")
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
            batch = adaptive and best_official_state is not None
            if batch and not can_screen():
                break
            improved |= attempt(name, plan, proxy, batch=batch)
            evaluated += 1
            if evaluated >= params["official_per_round"] or not (can_screen() if batch else can_explore()):
                break
        if adaptive:
            improved |= flush_staged("local_round_end")
        stale = 0 if improved else stale + 1
        if not evaluated:
            if not pending and all((s["digest"], k) in exhausted for s in states for k in range(3)):
                break
    if adaptive:
        flush_staged("final")
    # Exploration estimates are saved, but may never become the submitted answer.
    if best_official_state is not None:
        best_plan, best_result = best_official_state['plan'], best_official_state['result']
        services.checkpoint(best_plan, best_result)
    else:
        best_plan = best_result = None
    if best_result is None:
        final_check = dict(status='no_valid_plan',invalid_plans=len(invalid_digests))
        save_search()
        raise ValueError(f"no valid plan with official evaluation within {seconds:.1f}s; officially rejected {len(invalid_digests)} plans")
    search_seconds = time.perf_counter() - started
    budget.can_start()
    save_search()
    return best_plan, best_result, services.history
