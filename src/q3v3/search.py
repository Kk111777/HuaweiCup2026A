"""Budgeted full-state search with a freshly evaluated, nonregressing incumbent."""
from __future__ import annotations

from collections import deque
import json
import math
from pathlib import Path
import subprocess
import time

from .construction import construction_candidates
from .beam import cache_signature, retain
from .cache_search import cache_candidates, diagnose_cache
from . import ALGORITHM_NAME, ALGORITHM_REVISION
from .global_construction import RegionLibrary, global_candidates
from .joint_search import joint_candidates
from .l2_structure import Feedback, structural_candidates
from .q1_structure import StructureLibrary
from .official import evaluate_file, metrics, read_settings, replay, sha256, write_json
from .objective import DEFAULT_POLICY, describe, improvement, key, record
from .q1_compat import export_seeds
from .state import Graph, PlanState, compute_bounds


def find_warm_plan(root, case, cores):
    if root is None:
        return None
    root = Path(root)
    if root.is_file():
        return root
    names = [root / case / f'N{cores}' / 'plan.json',
             root / case / f'N{cores}' / f'{case}_multicore_res.json',
             root / f'N{cores}' / 'plan.json', root / 'plan.json']
    return next((p for p in names if p.is_file()), None)


def solve_one(graph_path, cores, config, output, q1_source=None, *, warm_plan=None,
              previous_plan=None, seconds=120, max_evaluations=64, enable_l2=True,
              near_bound_fraction=.02, evaluation_timeout=120,
              enable_global=True, enable_joint=True, enable_structural=True, objective_policy=DEFAULT_POLICY,
              seed_policy='auto', q2_warm_plan=None, reference_plan=None, prepared_seeds=None,
              cycle_tolerance=.01, traffic_weight=.05, added_share=.25):
    if objective_policy == 'cycles-traffic':
        from .traffic_search import solve_traffic
        return solve_traffic(graph_path, cores, config, output, q1_source, warm_plan=warm_plan,
            q2_warm_plan=q2_warm_plan, reference_plan=reference_plan, previous_plan=previous_plan,
            prepared_seeds=prepared_seeds, seconds=seconds, max_evaluations=max_evaluations,
            evaluation_timeout=evaluation_timeout, cycle_tolerance=cycle_tolerance,
            traffic_weight=traffic_weight, added_share=added_share, enable_l2=enable_l2,
            enable_global=enable_global, enable_joint=enable_joint, enable_structural=enable_structural,
            seed_policy=seed_policy)
    if q2_warm_plan is not None or reference_plan is not None or prepared_seeds is not None:
        raise ValueError('multiple full solution seeds require objective=cycles-traffic')
    from .neighborhoods import generate_candidates
    if cores not in (1,2,3,4,5) or type(cores) is not int:
        raise ValueError('supported core counts are 1 through 5')
    describe(objective_policy)
    if seed_policy not in ('auto', 'portfolio'):
        raise ValueError('seed policy must be auto or portfolio')
    use_bridge = warm_plan is None or seed_policy == 'portfolio'
    if cores == 1 and use_bridge:
        raise ValueError('N1 requires a complete warm plan with seed-policy auto; Q1 graph bridge supports N2-N5')
    if use_bridge and q1_source is None:
        raise ValueError('cold/portfolio seed mode requires a Q1 source; a full warm plan does not')
    if (type(max_evaluations) is not int or max_evaluations < 3 or not math.isfinite(seconds)
            or seconds <= 0 or not math.isfinite(evaluation_timeout) or evaluation_timeout <= 0
            or not 0 <= near_bound_fraction < 1):
        raise ValueError('invalid search budget or lower-bound threshold')
    graph_path, config, output = Path(graph_path), Path(config), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    raw = json.loads(graph_path.read_text(encoding='utf-8-sig'))
    graph, settings = Graph(raw), read_settings(config)
    bounds = compute_bounds(graph, cores)
    bridge = (export_seeds(graph_path, config, cores, Path(q1_source), output / 'q1_seed',
                          timeout=evaluation_timeout) if use_bridge else None)
    features = (bridge['features'] if bridge else dict(source='Q3 native graph index',
        operation_count=len(graph.ops), tensor_count=len(graph.tensors),
        component_count=len(graph.components), chain_count=len(graph.chains),
        cores=cores, compute_bounds=bounds, singlecore_score_used=False))
    write_json(output / 'features.json', features)
    def objective(result):
        return key(result, objective_policy)
    rows, seen, best, baseline, beam, expanded = [], set(), None, None, [], set()
    cycle_anchor = None
    last_cycle_call = 0
    supplied_result = None
    source_hashes = dict(graph=sha256(graph_path), config=sha256(config))

    def consider(name, plan, family, reason, timeout, parent_digest=None):
        nonlocal best, baseline, beam, supplied_result, cycle_anchor, last_cycle_call
        try:
            if len(plan['core_schedules']) != cores:
                raise ValueError('warm start has a different number of cores')
            state = PlanState(graph, plan)
        except (ValueError, KeyError, TypeError) as exc:
            rows.append(dict(name=name, family=family, status='structurally_invalid', error=str(exc)))
            return None
        if state.digest in seen:
            return None
        seen.add(state.digest)
        index = sum('evaluation_directory' in r for r in rows)
        folder = output / 'evaluations' / f'{index:04d}'
        row = dict(name=name, family=family, reason=reason, state_digest=state.digest,
                   parent_digest=parent_digest, evaluation_directory=str(folder.relative_to(output)))
        t = time.perf_counter()
        try:
            result = evaluate_file(graph_path, state.plan, config, folder, timeout=timeout)
            row.update(status='ok', **metrics(result))
            # Placement diversity is separate from complete input identity;
            # beam retention also preserves distinct observed cache order.
            placement = tuple(sorted(tuple(sorted(v for s in queue for v in state.blocks[s]))
                                     for queue in state.queues))
            item = dict(plan=state.plan, result=result, digest=state.digest, name=name,
                        placement=placement, cache_signature=cache_signature(result))
            # A secondary gain updates delivery/cache exploration but must not
            # repeatedly throw away untried cycle-reduction neighborhoods.
            if cycle_anchor is None or result['makespan'] < cycle_anchor['result']['makespan']:
                cycle_anchor = item
                last_cycle_call = index + 1
            if baseline is None:
                baseline = item
            if name == 'provided_q1_plan':
                supplied_result = result
            if best is None or objective(result) < objective(best['result']):
                row['improvement'] = improvement(best['result'], result, objective_policy) if best else 'initial'
                best = item
                row['accepted'] = True
                write_json(output / 'plan.json', state.plan)
                write_json(output / 'checkpoint.json', dict(name=name, state_digest=state.digest,
                           objective=record(result, objective_policy), plan=state.plan))
            else:
                row['accepted'] = False
            # Equal-objective but distinct order/placement remains explorable.
            beam = retain(beam + [item], objective_policy)
        except (ValueError, RuntimeError, OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
            row.update(status='official_rejected', error=str(exc))
            result = None
        row['seconds'] = time.perf_counter() - t
        rows.append(row)
        write_json(output / 'history.json', rows)
        return result

    initial = []
    if warm_plan is not None:
        warm_path = Path(warm_plan)
        initial.append(('provided_q1_plan', json.loads(warm_path.read_text(encoding='utf-8-sig'))))
        source_hashes['warm_plan'] = sha256(warm_path)
    if previous_plan is not None:
        queues = previous_plan['core_schedules']
        if len(queues) <= cores:
            initial.append(('previous_core_count', dict(previous_plan,
                core_schedules=[list(q) for q in queues] + [[] for _ in range(cores-len(queues))])))
    initial += [(v['name'], v['plan']) for v in bridge['plans']] if bridge else []
    for name, plan in initial:
        if sum('evaluation_directory' in r for r in rows) >= max_evaluations:
            break
        consider(name, plan, 'seed', 'Fresh official Q3 evaluation; no archived score reused.', evaluation_timeout)
    if best is None:
        raise RuntimeError(f'No officially valid seed; see {output / "history.json"}')
    if warm_plan is not None and supplied_result is None:
        raise RuntimeError('The explicitly supplied Q1 plan failed official Q3 validation')
    seed_best = best
    cycle_anchor = best
    search_started = time.perf_counter()
    preparation_seconds = search_started - started
    construction = iter(construction_candidates(graph, seed_best['plan'], settings))
    library = RegionLibrary(graph, cores, settings)
    structure_library = StructureLibrary(graph, cores) if enable_structural else None
    def check_budget():
        if time.perf_counter() - search_started >= seconds:
            raise TimeoutError('proposal construction exhausted the search budget')
    streams = {'inherited': construction}
    if enable_global:
        streams['global'] = iter(global_candidates(graph, cores, settings, library, check_budget))
    local_queue = deque()
    local_source = None
    joint_streams, joint_expanded = {}, set()
    cache_streams, cache_expanded = {}, set()
    structural_streams, structural_expanded, structural_turn = {}, set(), 0
    structure_feedback = []
    expansions = []
    slots = (('inherited', 'local', 'global', 'joint', 'cache',
              'inherited', 'global', 'local', 'joint', 'cache')
             if warm_plan is not None else
             ('inherited', 'local', 'global', 'joint', 'cache', 'inherited', 'global', 'local', 'joint'))
    if enable_structural:
        slots = ('inherited', 'local', 'global', 'structural', 'cache', 'inherited',
                 'global', 'local', 'structural', 'joint', 'structural', 'cache')
    cursor, stop = 0, 'candidate_exhausted'
    phase, phases = 'cycles', []
    near_bound_call_start = None
    polished_digest = None

    def next_local():
        nonlocal local_source, local_queue
        # Only a new minimum cycle count restarts the active cycle neighborhood.
        # Equal-cycle cache improvements still replace best and get cache work.
        if local_source != cycle_anchor['digest'] and cycle_anchor['digest'] not in expanded:
            local_queue.clear()
        while not local_queue:
            source = next((v for v in [cycle_anchor, best] + beam if v['digest'] not in expanded), None)
            if source is None:
                return None
            expanded.add(source['digest'])
            local_source = source['digest']
            expansions.append(dict(kind='local', parent_digest=source['digest'],
                                   official_calls=sum('evaluation_directory' in r for r in rows)))
            for candidate in generate_candidates(graph, source['plan'], source['result'], settings,
                                                   max_candidates=32, enable_l2=enable_l2):
                local_queue.append((candidate, source['digest']))
        return local_queue.popleft() if local_queue else None

    def next_joint():
        if not enable_joint:
            return None
        source = next((v for v in [cycle_anchor, best] + beam if v['digest'] not in joint_expanded), None)
        while source is not None:
            digest = source['digest']
            if digest not in joint_streams:
                joint_streams[digest] = iter(joint_candidates(graph, source['plan'], source['result'],
                    settings, library, check_budget))
                expansions.append(dict(kind='joint', parent_digest=digest,
                                       official_calls=sum('evaluation_directory' in r for r in rows)))
            try:
                return next(joint_streams[digest]), digest
            except StopIteration:
                joint_expanded.add(digest)
                del joint_streams[digest]
                source = next((v for v in [cycle_anchor, best] + beam if v['digest'] not in joint_expanded), None)
        return None

    def next_cache():
        if not enable_l2:
            return None
        source = next((v for v in [best] + beam if v['digest'] not in cache_expanded), None)
        while source is not None:
            digest = source['digest']
            if digest not in cache_streams:
                cache_streams[digest] = iter(cache_candidates(graph, source['plan'], source['result'], settings))
                expansions.append(dict(kind='cache', parent_digest=digest,
                    official_calls=sum('evaluation_directory' in r for r in rows)))
            try:
                return next(cache_streams[digest]), digest
            except StopIteration:
                cache_expanded.add(digest)
                del cache_streams[digest]
                source = next((v for v in [best] + beam if v['digest'] not in cache_expanded), None)
        return None

    def next_structural():
        nonlocal structural_turn
        if not enable_structural:
            return None
        calls = sum('evaluation_directory' in r for r in rows)
        # The primary objective controls radius. Improving only hit rate does
        # not conceal a cycles plateau or reset the structural exploration.
        scale = min(2, (calls - last_cycle_call) // 8)
        origins, used = [], set()
        for source in [cycle_anchor, best] + beam:
            if (source['digest'] not in used and
                    source['result']['makespan'] <= 1.1 * cycle_anchor['result']['makespan']):
                origins.append(source)
                used.add(source['digest'])
        start = (structural_turn // 2) % len(origins)
        structural_turn += 1
        for source in origins[start:] + origins[:start]:
            token = (source['digest'], scale)
            if token in structural_expanded:
                continue
            if token not in structural_streams:
                structural_streams[token] = iter(structural_candidates(graph, source['plan'], source['result'],
                    settings, structure_library, scale, check_budget, enable_l2=enable_l2))
                expansions.append(dict(kind='structural', parent_digest=source['digest'],
                                       scale=scale, official_calls=calls))
                feedback = Feedback(graph, PlanState(graph, source['plan']), source['result'],
                                    settings, enable_l2=enable_l2).summary()
                structure_feedback.append(dict(scale=scale, official_calls=calls, **feedback))
            try:
                return next(structural_streams[token]), source['digest']
            except StopIteration:
                structural_expanded.add(token)
                del structural_streams[token]
        return None
    # Search time excludes input, seed validation and final replay, all reported.
    while True:
        elapsed = time.perf_counter() - search_started
        official_calls = sum('evaluation_directory' in row for row in rows)
        if elapsed >= seconds:
            stop = 'search_time_budget'
            break
        if official_calls >= max_evaluations:
            stop = 'official_call_budget'
            break
        remaining_fraction = 1 - bounds['lower_bound'] / max(1, best['result']['makespan'])
        near_bound = remaining_fraction <= near_bound_fraction
        if near_bound:
            if objective_policy != DEFAULT_POLICY or not enable_l2:
                stop = 'compute_lower_bound_gap'
                break
            if near_bound_call_start is None:
                near_bound_call_start = official_calls
            if official_calls - near_bound_call_start >= 8:
                stop = 'near_bound_cache_budget'
                break
        polish = (objective_policy == DEFAULT_POLICY and enable_l2 and polished_digest != best['digest'] and
                  (near_bound or official_calls >= max_evaluations - min(8, max_evaluations // 4)
                   or elapsed >= .8 * seconds))
        next_phase = 'cache_polish' if polish else 'cycles'
        if next_phase != phase or not phases:
            phase = next_phase
            phases.append(dict(phase=phase, official_calls=official_calls, seconds=elapsed,
                               trigger='near_compute_bound' if near_bound else 'remaining_budget'))
        proposal = None
        try:
            # A full rotation tries every active type before concluding that
            # candidates are exhausted. Proxy ties never suppress an entire
            # global constructor or a new incumbent's joint neighborhood.
            active_slots = ('cache',) if polish else slots
            for _ in range(len(active_slots)):
                kind = active_slots[cursor % len(active_slots)]
                cursor += 1
                if kind in streams:
                    try:
                        value = next(streams[kind])
                    except StopIteration:
                        del streams[kind]
                        continue
                    if kind == 'inherited':
                        name, plan = value
                        proposal = (name, plan, 'construction', 'Inherited partition refinement/placement.', seed_best['digest'])
                    else:
                        proposal = (value.family, value.plan, value.family, value.reason, None)
                elif kind in ('local', 'joint', 'cache', 'structural'):
                    value = {'local': next_local, 'joint': next_joint, 'cache': next_cache,
                             'structural': next_structural}[kind]()
                    if value is not None:
                        candidate, parent = value
                        proposal = candidate.family, candidate.plan, candidate.family, candidate.reason, parent
                if proposal is not None:
                    break
        except TimeoutError:
            stop = 'search_time_budget'
            break
        if proposal is None:
            if polish:
                if not near_bound:
                    polished_digest = best['digest']
                    continue
                stop = 'cache_candidates_exhausted_near_bound'
            break
        name, plan, family, reason, parent = proposal
        remaining = seconds - (time.perf_counter() - search_started)
        if remaining < .1:
            stop = 'search_time_budget'
            break
        consider(name, plan, family, reason, min(evaluation_timeout, remaining), parent)
    search_seconds = time.perf_counter() - search_started
    # Independent official CLI replay of the exact delivered file.
    write_json(output / 'plan.json', best['plan'])
    replay_started = time.perf_counter()
    verified = replay(graph_path, output / 'plan.json', config, output / 'verified', timeout=evaluation_timeout)
    replay_seconds = time.perf_counter() - replay_started
    if objective(verified) != objective(best['result']):
        raise RuntimeError('Final CLI replay objective does not match search evaluation')
    if objective(verified) > objective(seed_best['result']):
        raise RuntimeError('Incumbent regression')
    if metrics(verified) != metrics(best['result']):
        raise RuntimeError('Final CLI replay metrics do not match search evaluation')
    if sha256(graph_path) != source_hashes['graph'] or sha256(config) != source_hashes['config']:
        raise RuntimeError('Input/config changed during optimization')
    if warm_plan is not None and sha256(warm_plan) != source_hashes['warm_plan']:
        raise RuntimeError('Warm plan changed during optimization')
    write_json(output / 'bounds.json', compute_bounds(graph, cores, best['plan']))
    write_json(output / 'expansions.json', expansions)
    write_json(output / 'search_phases.json', phases)
    write_json(output / 'structure_feedback.json', structure_feedback)
    write_json(output / 'cache_analysis.json', dict(
        initial=diagnose_cache(graph, seed_best['plan'], seed_best['result'], settings),
        final=diagnose_cache(graph, best['plan'], verified, settings)))
    summary = dict(algorithm=ALGORITHM_NAME, algorithm_revision=ALGORITHM_REVISION,
        case=graph_path.stem, status='ok', **metrics(verified),
        seed_makespan=seed_best['result']['makespan'],
        supplied_plan_makespan=supplied_result['makespan'] if supplied_result else None,
        supplied_plan_metrics=metrics(supplied_result) if supplied_result else None,
        seed_metrics=metrics(seed_best['result']), objective=record(verified, objective_policy),
        objective_policy=objective_policy, seed_policy=seed_policy,
        warm_start_mode='full_plan_only' if not use_bridge else 'graph_seed_portfolio',
        seed_name=seed_best['name'], winner=best['name'],
        reduction_from_seed_percent=100*(1-verified['makespan']/max(1,seed_best['result']['makespan'])),
        official_calls=sum('evaluation_directory' in r for r in rows),
        accepted_moves=sum(bool(r.get('accepted')) and r['family'] != 'seed' for r in rows),
        rejected_calls=sum(r['status'] == 'official_rejected' for r in rows),
        preparation_seconds=preparation_seconds, search_seconds=search_seconds,
        final_replay_seconds=replay_seconds, total_seconds=time.perf_counter()-started,
        stop_reason=stop, enable_l2=enable_l2, source_hashes=source_hashes,
        enable_global=enable_global, enable_joint=enable_joint,
        enable_structural=enable_structural,
        structural_calls=sum('evaluation_directory' in r and r['family'].startswith('structural_') for r in rows),
        structural_improvements=sum(bool(r.get('accepted')) and r['family'].startswith('structural_') for r in rows),
        structural_scales=sorted({v['scale'] for v in structure_feedback}),
        cache_only_improvements=sum(r.get('improvement') == 'byte_hit_rate_at_equal_cycles' for r in rows),
        cycle_search_anchor_digest=cycle_anchor['digest'],
        expanded_cache_states=len({v['parent_digest'] for v in expansions if v['kind']=='cache'}),
        expanded_local_states=len(expanded), expanded_joint_states=len({v['parent_digest'] for v in expansions if v['kind']=='joint'}),
        lower_bound=bounds['lower_bound'], plan_sha256=sha256(output/'plan.json'),
        q1_source_provenance=bridge['provenance'] if bridge else None, singlecore_score_used=False,
        verification='independent official problem-3 CLI with trace and hashes')
    write_json(output / 'summary.json', summary)
    return summary, best['plan']
