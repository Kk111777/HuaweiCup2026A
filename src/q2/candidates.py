"""Shared Q2 proposal/evaluation services; official incumbents only.

The pipeline owns process supervision and publication. This module never launches
a second solver and never reads historical multi-core answers.
"""
from dataclasses import asdict, replace
import json
import time

from common.artifacts import write_json
from .official import digest, objective, read_json, Q2Error
from .contracts import BaselineResult, ConstructionDraft, EncodedCandidate
from .construct import construct_candidate, BudgetExpired
from .encoding import encode_candidate, encode_atomic, validate_fidelity, realized_blocks
from .affinity import component_candidates
from .beta import PlateauBeta
from .memory import profile
from .capacity import CapacityWindow, minimum_disruption, apply_window


class CandidateEvaluator:
    def __init__(self, graph, graph_path, directory, budget, calls, records, run_official, identity):
        self.g, self.graph_path, self.directory = graph, graph_path, directory
        self.budget, self.calls, self.records = budget, calls, records
        self.run_official, self.identity = run_official, identity
        self.cache = {}; self.best = None
        self.prefix_seconds = 0.; self.cache_hits = 0

    def evaluate(self, candidate, phase='B0', bundle=None):
        """Return an exact objective or a typed incomplete/invalid record.

        Cache contains compact summaries and disk paths, never full simulations.
        Fidelity is checked for every encoding, even on successful cache hits.
        """
        self.budget.check()
        g, plan = self.g, candidate.plan
        key = digest(dict(identity=self.identity, plan=plan, evaluation_version=2))
        row = dict(candidate_index=len(self.records), family=candidate.draft.family,
                   mode=candidate.draft.mode, beta=candidate.draft.beta, encoding=candidate.encoding,
                   plan_hash=digest(plan), encoder_version=candidate.encoder_version,
                   parent_version=candidate.parent_version, phase=phase)
        self.records.append(row)
        start = time.monotonic()
        from stub_multicore_cut_and_schedule import MulticoreCutError
        from multicore_cut_evaluate_problem_2 import SceneBEvaluationError
        try:
            bundle = g.settings.prefix(g.raw, plan) if bundle is None else bundle
            row['fidelity'] = validate_fidelity(g, bundle, candidate)
            core = candidate.draft.operation_core
            work, size = g.base_traffic(core)
            actual_work = sum(max(1, op['cycles']) for c in bundle['cores'].values()
                              for op in c['graph']['ops'] if op['op'] in ('COPY_IN', 'COPY_OUT'))
            if (work, size) != (actual_work, bundle['base_copy_bytes']):
                raise AssertionError('incremental/formula/official traffic mismatch')
            row.update(base_copy_bytes=size, cross_copy_pairs=len(bundle['cross_links']),
                       split_components=sum(len({core[v] for v in component}) > 1 for component in g.components),
                       subgraph_count=len(set(plan['node_to_subgraph'].values())),
                       search_block_count=len(candidate.draft.search_blocks),
                       proxy_J=candidate.draft.diagnostics.get('proxy_J'),
                       profile_by_core={k: profile(c['graph'], c['seq'], g.settings.capacity).record()
                                        for k, c in bundle['cores'].items()})
        except (MulticoreCutError, SceneBEvaluationError) as error:
            row.update(evaluation_status='INVALID', error=str(error))
            return None, row
        finally:
            self.prefix_seconds += time.monotonic()-start
        cached = self.cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            row.update(cached, evaluation_status='CACHED_OFFICIAL')
        else:
            self.budget.check()
            if self.best is not None and self.budget.remaining()-self.budget.reserve < self.budget.estimate:
                raise BudgetExpired('reserve final verification before next evaluation')
            path = self.directory/f'eval_{len(self.calls):03d}'
            result = self.run_official(self.graph_path, plan, g.settings, path,
                                      self.budget.remaining()-self.budget.reserve, self.calls, phase)
            self.budget.observe(self.calls[-1]['wall_seconds'])
            status = self.calls[-1]['status']
            row['evaluation_status'] = 'OFFICIAL' if result is not None else (
                'INVALID' if status == 'OFFICIAL_ERROR' else status)
            if result is None:
                return None, row
            value = objective(result)
            cached = dict(objective=value, result_path=f'{path.name}/result.json',
                          official_call_index=len(self.calls)-1,
                          spill_bytes=result['data_movement_bytes']['spill_added_copy_bytes'])
            self.cache[key] = cached
            row.update(cached)
        value = tuple(row['objective'])
        if self.best is None or value < self.best[0]:
            self.best = (value, candidate, dict(row))
            write_json(self.directory/'checkpoint_plan.json', plan)
            write_json(self.directory/'checkpoint_candidate.json', row)
        return value, row


def evaluate_candidate(service, candidate, **kwargs):
    return service.evaluate(candidate, **kwargs)


def capacity_repair_candidates(g, candidate, bundle, options, check, records):
    """All proposals share this frozen parent; no unverified changes accumulate."""
    plan = candidate.plan
    blocks_by_core = realized_blocks(g, plan, bundle, candidate.draft, options['block_operations'])
    seen = set()
    for k, c in bundle['cores'].items():
        check()
        p = profile(c['graph'], c['seq'], g.settings.capacity)
        if p.no_spill:
            continue
        blocks = blocks_by_core[k]
        if len(blocks) < 2:
            continue
        where = {sg: i for i, block in enumerate(blocks) for sg in block}
        centers = dict.fromkeys(where[c['op_subgraph'][op]] for i, op in enumerate(c['seq'])
                               if p.curve[0][i] > g.settings.capacity['L1'] or
                                  p.curve[1][i] > g.settings.capacity['UB'])
        for center in centers:
            width = min(options['window_subgraphs'], len(blocks))
            left = min(max(0, center-width//2), len(blocks)-width)
            if (k, left) in seen:
                continue
            if len(records) >= options['max_windows']:
                return
            seen.add((k, left)); check(); start = time.monotonic()
            w = CapacityWindow(bundle, k, blocks[left:left+width], g.settings.capacity)
            answer = minimum_disruption(w, check)
            row = {key: value for key, value in answer.items() if key != 'predicted_seq'}
            row.update(core=k, left_block=left, parent_plan_hash=digest(plan), accepted=False,
                       wall_seconds=time.monotonic()-start, completed=True)
            records.append(row)
            if answer['status'] == 'OPTIMAL_IN_FIXED_WINDOW' and answer['inversions'] > 0:
                new, fresh = apply_window(g, plan, bundle, w, answer, lambda graph, p: graph.settings.prefix(graph.raw, p))
                row['sequence_verified'] = True
                row['whole_core_profile'] = profile(fresh['cores'][k]['graph'], fresh['cores'][k]['seq'], g.settings.capacity).record()
                # The window has a new intended order; never claim the old commit order.
                proposed = replace(candidate, plan_json=json.dumps(new), encoding='capacity_min_disruption',
                                   intended_order=None, parent_version=digest(plan))
                yield proposed, fresh, row


def search_baseline(g, graph_path, cores, options, directory, budget, calls, records, windows,
                    run_official, identity, report):
    c, e = options['construction'], options['enhancement']
    modes = [m for m in c['scope_modes'] if m != 'component_grouped' or len(g.components) > 1]
    streams = [f'linked/{mode}/{enc}' for enc in c['encodings'] for mode in modes]
    beta = PlateauBeta(streams, c['betas'][-1], c['beta_search'], c['beta_continuation'])
    service = CandidateEvaluator(g, graph_path, directory, budget, calls, records, run_official, identity)
    drafts = {}; construction_seconds = 0.; stop = 'completed'; incomplete = False

    def checkpoint():
        write_json(directory/'beta_state.json', beta.state_dict())
        write_json(directory/'progress.json', dict(phase='repair_search', candidates=len(records),
                   windows=len(windows), stop_reason=stop, official_calls=calls,
                   elapsed_seconds=time.monotonic()-budget.start))
        print(f'[{graph_path.stem} N{cores}] Q2 repair: candidates={len(records)} windows={len(windows)} calls={len(calls)}', flush=True)

    try:
        if c['component_candidates']:
            for candidate in component_candidates(g, cores):
                value, row = service.evaluate(candidate)
                incomplete |= row['evaluation_status'] not in ('OFFICIAL', 'CACHED_OFFICIAL', 'INVALID')
                checkpoint()
        while (entry := beta.next()) is not None:
            stream, b = entry; _, mode, enc = stream.split('/')
            if (mode, b) not in drafts:
                stats = {}; start = time.monotonic()
                try:
                    budget.check_construction()
                    if options['runtime']['debug_trace']:
                        with (directory/f'construction_{mode}_{b}.jsonl').open('w', encoding='utf-8') as trace:
                            draft = construct_candidate(g, cores, mode, b, c['shortlist_k'], budget.check_construction,
                                lambda event: trace.write(json.dumps(event)+'\n'), stats=stats)
                    else:
                        draft = construct_candidate(g, cores, mode, b, c['shortlist_k'], budget.check_construction, stats=stats)
                except BudgetExpired:
                    report['interrupted_construction'] = dict(mode=mode, beta=b, **stats)
                    raise
                finally:
                    construction_seconds += time.monotonic()-start
                drafts[mode, b] = draft
                write_json(directory/f'draft_{mode}_{b}.json', asdict(draft))
            candidate = encode_candidate(g, cores, drafts[mode, b], enc)
            value, row = service.evaluate(candidate)
            decision = beta.observe(stream, b, value, row['evaluation_status'])
            row['search_decision'] = decision
            incomplete |= decision['stop_reason'] == 'INCOMPLETE_EVALUATION'
            checkpoint()
    except BudgetExpired:
        incomplete = True; stop = 'initialization_budget'; beta.stop(stop)
    if service.best is None:
        # Explicit independently evaluated all-on-one-core fallback, not an archived answer.
        draft = ConstructionDraft((0,)*len(g.ids), tuple(g.order), (tuple(g.order),), '{}', 'fallback', 'fallback', None, '{}')
        candidate = EncodedCandidate(json.dumps(encode_atomic(g, cores, draft.operation_core, draft.commit_order)), draft, 'commit', draft.commit_order)
        service.evaluate(candidate, 'fallback')
        if service.best is None:
            raise Q2Error('NO_VERIFIED_CANDIDATE', '没有完成有效 B0')
    base_value, base_candidate, base_row = service.best
    base_result = read_json(directory/base_row['result_path'])
    baseline_plan = base_candidate.plan
    write_json(directory/'baseline_plan.json', baseline_plan)
    write_json(directory/'baseline_result.json', base_result)
    report['selected_candidate'] = base_row
    report['stages'].update(construction_seconds=construction_seconds,
                            initialization_official_seconds=sum(x['wall_seconds'] for x in calls),
                            plan_serialization_seconds=0.)
    start = time.monotonic()
    report['enhancement_gate'] = 'DISABLED' if not e['enabled'] else 'NO_REAL_SPILL'
    if e['enabled'] and (not e['spill_gate'] or base_row['spill_bytes'] > 0):
        report['enhancement_gate'] = 'REAL_SPILL' if e['spill_gate'] else 'LEGACY_PEAK_ABLATION'
        try:
            budget.check(); bundle = g.settings.prefix(g.raw, baseline_plan)
            if e['objective'] == 'capacity_min_disruption':
                for candidate, fresh, row in capacity_repair_candidates(g, base_candidate, bundle, e, budget.check, windows):
                    value, ev = service.evaluate(candidate, 'B1', fresh)
                    row.update(official_objective=value, plan_hash=ev['plan_hash'], evaluation_status=ev['evaluation_status'],
                               accepted_against_parent=value is not None and value < base_value)
                    incomplete |= ev['evaluation_status'] not in ('OFFICIAL', 'CACHED_OFFICIAL', 'INVALID')
                    checkpoint()
            else:
                from .window import Window, choose_window, solve_window, apply_order
                plan = baseline_plan; processed = set()
                for _ in range(e['max_windows']):
                    budget.check(); bundle['_raw'] = g.raw
                    profiles = {k: profile(x['graph'], x['seq'], g.settings.capacity) for k, x in bundle['cores'].items()}
                    chosen = choose_window(bundle, profiles, e['window_subgraphs'], processed)
                    if chosen is None: break
                    k, left, right = chosen; processed.add(chosen)
                    w = Window(bundle, k, left, right, g.settings.capacity)
                    answer = solve_window(w, budget.check)
                    row = {key: value for key, value in answer.items() if key != 'predicted_seq'}
                    row.update(core=k, left=left, right=right, accepted=False); windows.append(row)
                    if not answer['completed']: raise BudgetExpired('legacy window interrupted')
                    if answer['peak_after'] < answer['peak_before']:
                        plan, bundle = apply_order(plan, bundle, w, answer, g.settings)
                        processed.clear(); row['accepted'] = True
                if plan != baseline_plan:
                    candidate = replace(base_candidate, plan_json=json.dumps(plan), intended_order=None,
                                        encoding='legacy_peak', parent_version=digest(baseline_plan))
                    _, row = service.evaluate(candidate, 'B1', bundle)
                    incomplete |= row['evaluation_status'] not in ('OFFICIAL', 'CACHED_OFFICIAL', 'INVALID')
        except BudgetExpired:
            incomplete = True; stop = 'enhancement_budget'
    best_value, selected_candidate, selected_row = service.best
    selected = 'B1' if best_value < base_value else 'B0'
    result = read_json(directory/selected_row['result_path'])
    for row in windows:
        if e['objective'] == 'capacity_min_disruption':
            row['accepted'] = selected == 'B1' and row.get('plan_hash') == selected_row['plan_hash']
    report['stages'].update(enhancement_seconds=time.monotonic()-start, step1_profile_seconds=service.prefix_seconds)
    if incomplete and stop == 'completed': stop = 'incomplete_evaluation'
    report['search'] = dict(beta.state_dict(), policy=c['beta_search'], complete=not incomplete,
                            official_evaluations=sum(x.get('launched', False) for x in calls if x['phase'] == 'B0'),
                            evaluated_candidates=len(records), cache_hits=service.cache_hits)
    report['search_complete'] = not incomplete
    report['selected_candidate'] = selected_row
    report['evaluated_candidates'] = len(records)
    checkpoint()
    return dict(baseline_plan=baseline_plan, baseline_result=base_result,
                enhanced=result if selected == 'B1' else None, selected=selected,
                final_plan=selected_candidate.plan, final_result=result,
                candidate=selected_candidate, stop=stop,
                accepted=sum(bool(w.get('accepted')) for w in windows))


def export_baseline_result(g, candidate, verified, records, beta_state):
    plan = candidate.plan
    bundle = g.settings.prefix(g.raw, plan)
    blocks = realized_blocks(g, plan, bundle, candidate.draft)
    execution = dict(source='official_scene_b', step1=dict(cores=bundle['cores'], cross_links=bundle['cross_links']),
                     step3_summary=verified['step3_by_core'],
                     final_timeline=verified['per_core_timeline'],
                     data_movement_bytes=verified['data_movement_bytes'],
                     plan_hash=digest(plan), encoder_version=candidate.encoder_version)
    return BaselineResult(json.dumps(plan), json.dumps(verified), candidate.draft,
                          tuple(block for k in sorted(blocks) for block in blocks[k]),
                          json.dumps(execution), json.dumps(records), json.dumps(beta_state))
