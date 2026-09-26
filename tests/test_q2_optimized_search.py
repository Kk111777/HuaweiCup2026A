"""Search decisions, transactional proposals, exact evidence and public runtime."""
from collections import Counter, defaultdict
from dataclasses import replace, asdict
import json
from pathlib import Path
import random
import sys
import tempfile
import shutil
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from q2.official import Settings, read_json
SETTINGS = Settings.read()
from q2.graph import Graph
from q2.encoding import encode_atomic
from q2.state import build_state
from q2.contracts import EncodedCandidate
from q2.evaluation import ScoredState, exact_score
from q2.beta import PlateauBeta
from q2.construct import BudgetExpired
from q2.optimized.contracts import Proposal
from q2.optimized.proposals import (build_order, TrafficIndex, eft_rebuild, order_proposals,
                                    feedback_proposals, block_proposals)
from q2.optimized.service import EvaluationService
from q2.optimized.construction import Construction
from q2.optimized.pool import StatePool, select_batch
from q2.optimized.engine import SearchSession
from q2.optimized.control import parameters
from q2.optimized.runtime import recover
from q2.optimized.api import solve_case, replay, batch, frozen_output, reusable
from scheduling.budget import SearchBudget
from q2_optimized_fixture import make_graph


def example(seed=9, cores=3):
    raw, order = make_graph(seed)
    g = Graph(raw, SETTINGS)
    mapping = tuple(v % cores for v in range(len(g.ids)))
    plan = encode_atomic(g, cores, mapping, tuple(g.dense[o] for o in order))
    return g, build_state(g, plan)


def scan_order(g, core, batch=None, release_weight=0., continuation=0.):
    # Deliberately quadratic independent oracle: recompute every ready priority.
    done, order, remaining = set(), [], [len(t.consumers) for t in g.tensors]
    last, bands, groups = {}, {}, defaultdict(list)
    if batch is not None:
        for component in g.components: groups[min(core[v] for v in component)].append(component)
        for components in groups.values():
            for i, component in enumerate(components):
                for v in component: bands[v] = i//batch
    while len(done) < len(g.ids):
        ready = [v for v in range(len(g.ids)) if v not in done and set(g.preds[v]) <= done]
        def key(v):
            freed = sum(g.tensors[t].size for t in g.inputs[v] if remaining[t] == 1)
            new = sum(g.tensors[t].size for t in g.outputs[v] if g.tensors[t].consumers)
            follow = last.get(core[v]) in g.preds[v]
            priority = g.b[v] + release_weight*(freed-new)/g.settings.bandwidth + continuation*max(g.b)*follow
            return bands.get(v, 0), -priority, g.ids[v]
        v = min(ready, key=key); done.add(v); order.append(v); last[core[v]] = v
        for t in g.inputs[v]: remaining[t] -= 1
    return tuple(order)


class ProposalTests(unittest.TestCase):
    def test_sparse_ready_priority_matches_full_recalculation(self):
        for seed in range(12):
            g, state = example(seed)
            for options in ({}, {'batch': 2}, {'release_weight': .5, 'continuation': .1},
                            {'release_weight': 8}, {'continuation': .1}):
                self.assertEqual(build_order(g, state.core_of_op, **options), scan_order(g, state.core_of_op, **options))

    def test_incremental_traffic_matches_full_recomputation(self):
        for seed in range(12):
            g, state = example(seed)
            index = TrafficIndex(g, state.core_of_op, 3)
            before = (index.core, json.dumps(index.counts, sort_keys=True), json.dumps(index.loads))
            for row in state.compute_orders_by_core:
                for group in (row[:1], row[:4], row):
                    if not group: continue
                    for target in range(3):
                        new = list(state.core_of_op)
                        for v in group: new[v] = target
                        delta = index.delta(group, target)
                        other = TrafficIndex(g, new, 3)
                        self.assertEqual((delta['work'], delta['bytes'], delta['cross_pairs']),
                            (other.work-index.work, other.bytes-index.bytes, other.cuts-index.cuts))
                        self.assertEqual(delta['maximum_load'], max(map(max, other.loads)))
            self.assertEqual(before, (index.core, json.dumps(index.counts, sort_keys=True), json.dumps(index.loads)))

    def test_proposals_preserve_parent_and_frozen_migration_order(self):
        g, parent = example()
        before = asdict(parent)
        with tempfile.TemporaryDirectory() as temporary:
            service = EvaluationService(g, 'unused.json', temporary)
            _, trace = service.refined(parent)
            moves = list(feedback_proposals(g, parent, trace))
            self.assertLessEqual(len(moves), 24)
            self.assertTrue(moves)
            for proposal in moves:
                child = service.state(proposal, parent)
                service.refined(child)
                for v in proposal.region_ops:
                    self.assertEqual(child.core_of_op[v], proposal.parameters['target_core'])
            rebuilt = eft_rebuild(g, 3, build_order(g, parent.core_of_op), 1)
            self.assertEqual(len(rebuilt), len(g.ids))
        self.assertEqual(asdict(parent), before)

    def test_multisg_block_actions_preserve_whole_blocks_and_parent(self):
        g, state = example(4)
        groups = [[tuple(row[i:i+2]) for i in range(0,len(row),2)] for row in state.plan['core_schedules']]
        parent = build_state(g,state.plan,blocks_by_core=groups)
        with tempfile.TemporaryDirectory() as temporary:
            service=EvaluationService(g,'unused',temporary)
            _,trace=service.refined(parent)
            proposals=list(block_proposals(g,parent,trace,service.preparation))
            self.assertLessEqual(len(proposals),24)
            self.assertTrue(proposals)
            for proposal in proposals:
                proposed=proposal.parameters['blocks_by_core']
                for before,after in zip(groups,proposed):
                    self.assertEqual(sorted(map(tuple,before)),sorted(map(tuple,after)))
        self.assertEqual(parent.plan,state.plan)


class ControlTests(unittest.TestCase):
    def test_family_quotas_prevent_cheap_family_starvation(self):
        entries = []
        for f in range(6):
            for i in range(8):
                entries.append(dict(proposal=SimpleNamespace(family=str(f)),
                    coarse=SimpleNamespace(score=SimpleNamespace(makespan=f*100+i))))
        selected = select_batch(entries, 12, 2, 3)
        self.assertEqual(Counter(e['proposal'].family for e in selected), {str(i): 2 for i in range(6)})

    def test_pool_calibration_can_replace_search_but_never_exact(self):
        g, state = example()
        with tempfile.TemporaryDirectory() as temporary:
            service = EvaluationService(g, 'unused.json', temporary)
            a, trace_a = service.refined(state)
            proposal = next(order_proposals(g, state))
            bstate = service.state(proposal, state)
            b, trace_b = service.refined(bstate)
            a = replace(a, score=replace(a.score, makespan=100))
            b = replace(b, score=exact_score(b.state, (200, 0)))
            pool = StatePool(2); pool.update([a, b], [(state.state_signature, trace_a), (bstate.state_signature, trace_b)])
            self.assertEqual(pool.incumbents.search.state, a.state)
            self.assertEqual(pool.incumbents.exact.state, b.state)
            class Corrected:
                def rescore(self, st, trace):
                    return replace(trace, score=replace(trace.score, makespan=300))
            pool.recalibrate(Corrected())
            self.assertEqual(pool.incumbents.search.score.makespan, 200)
            pool.update([replace(b, score=trace_b.score)])
            self.assertEqual(pool.incumbents.exact.score.makespan, 200)
            with self.assertRaises(ValueError): ScoredState(a.state, a.score, replace(a.legality, status='UNKNOWN'))

    def test_beta_pending_invalid_gap_plateau_and_single_construction(self):
        g, state = example()
        constructor = Construction(g, 3)
        proposals = []
        while (p := constructor.next()) is not None: proposals.append(p)
        pending = [p for p in proposals if p.beta is not None]
        self.assertEqual(constructor.constructed, len({p.parameters['mode'] for p in pending}))
        self.assertIsNone(constructor.next())
        self.assertTrue(constructor.beta.pending)
        with tempfile.TemporaryDirectory() as temporary:
            row, _ = EvaluationService(g, 'unused', temporary).refined(state)
        with self.assertRaises(ValueError): constructor.observe(pending[0], row.score)
        constructor.observe(pending[0], exact_score(state, (100, 0)))
        p2 = constructor.next(); self.assertEqual(p2.beta, 2)
        constructor.observe(p2, exact_score(state, (100, 1)))
        p3 = constructor.next(); self.assertEqual(p3.beta, 3)
        constructor.observe(p3, status='INVALID')
        p4 = constructor.next(); self.assertEqual(p4.beta, 4)
        constructor.observe(p4, exact_score(state, (110, 0)))
        self.assertEqual(constructor.beta.records[-1]['relation'], 'first')
        restored = PlateauBeta.from_state_dict(json.loads(json.dumps(constructor.record())))
        self.assertEqual(json.dumps(restored.state_dict(), sort_keys=True), json.dumps(constructor.beta.state_dict(), sort_keys=True))

    def test_equivalent_exact_calls_once_and_fidelity_rechecked(self):
        g, state = example()
        with tempfile.TemporaryDirectory() as temporary:
            graph = Path(temporary)/'input.json'; graph.write_text(json.dumps(g.raw))
            service = EvaluationService(g, graph, Path(temporary)/'search')
            first, _ = service.official(state, 15)
            plan = state.plan
            renamed = dict(node_to_subgraph={o: s+100 for o,s in plan['node_to_subgraph'].items()},
                           core_schedules=[[s+100 for s in row] for row in plan['core_schedules']])
            other = build_state(g, renamed, preparation=service.preparation)
            second, record = service.official(other, 15)
            self.assertEqual(len(service.calls), 1)
            self.assertTrue(record['cached']); self.assertEqual(second.score.source, 'equivalent_official')
            self.assertEqual(first.score.makespan, second.score.makespan)
            self.assertEqual(second.score.plan_digest, other.plan_digest)
            candidate = EncodedCandidate(json.dumps(renamed), None, 'commit', tuple(reversed(range(len(g.ids)))), None)
            with self.assertRaises(AssertionError): service.state(Proposal(candidate, 'order'))

    def test_unknown_official_leaves_beta_waiting(self):
        g, state = example()
        with tempfile.TemporaryDirectory() as temporary:
            service = EvaluationService(g, 'unused', temporary)
            session = SearchSession(service, 3, parameters(), SearchBudget(60))
            session.initialize()
            for proposal in session._baseline(): session._entry(proposal)
            pending = dict(session.constructor.beta.pending)
            with patch.object(service, 'official', return_value=(None, dict(status='EVAL_TIMEOUT', wall_seconds=1))):
                session.calibrate()
            self.assertEqual(session.constructor.beta.pending, pending)
            self.assertFalse(session.constructor.beta.stops)

    def test_interrupted_batch_keeps_completed_candidate_without_changing_parent(self):
        g, _ = example()
        events=[]
        with tempfile.TemporaryDirectory() as temporary:
            service=EvaluationService(g,'unused',temporary)
            session=SearchSession(service,3,parameters({'calibration':'off','baseline_guard':False}),
                                  SearchBudget(60),emit=events.append)
            session.initialize(); parent=session.pool.incumbents.search.state
            proposals=list(order_proposals(g,parent))[:2]
            self.assertEqual(len(proposals),2)
            real=session._refine; completed=[]
            def bounded(entry):
                if completed: raise BudgetExpired('controlled batch interruption')
                value=real(entry); completed.append(value[0]); return value
            empty=lambda *a,**k: iter(())
            with patch.object(session,'_baseline',empty), patch.object(session,'_refine',bounded), \
                 patch('q2.optimized.engine.order_proposals',return_value=iter(proposals)), \
                 patch('q2.optimized.engine.rebuild_proposals',empty), \
                 patch('q2.optimized.engine.feedback_proposals',empty), \
                 patch('q2.optimized.engine.block_proposals',empty), \
                 patch('q2.optimized.engine.capacity_proposals',empty):
                session.step_batch()
            self.assertIn(completed[0].state.state_signature,session.pool.rows)
            end=next(e for e in events if e['event']=='batch_end')
            self.assertFalse(end['complete']); self.assertEqual(end['parent_signature'],parent.state_signature)


class RuntimeTests(unittest.TestCase):
    def test_public_runtime_frozen_replay_resume_identity_and_missing_exact(self):
        graph = ROOT/'tests/fixtures/q2_repair/minimal.json'
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/'run'
            result = solve_case(graph, SETTINGS.config, [2,3,4,5], directory, seconds=15.,
                                options={'calibration':'off', 'baseline_guard':False, 'max_batches':2})
            self.assertEqual(result['status'], 'ok')
            self.assertEqual([r['makespan'] for r in result['rows']], [6]*4)
            self.assertTrue(all(r['status']=='SUCCESS_ESTIMATED' for r in result['rows']))
            request = read_json(directory/'request.json')
            self.assertEqual(request['baseline_policy'], 'backend_owned')
            logs = [[json.loads(x) for x in (directory/f'N{k}'/'candidate_log.jsonl').read_text().splitlines()]
                    for k in (2,3,4,5)]
            initial = [x['case_elapsed_seconds'] for log in logs for x in log if x['event']=='initial_checkpoint']
            batches = [x['case_elapsed_seconds'] for log in logs for x in log if x['event']=='batch_begin']
            self.assertLess(max(initial), min(batches))
            self.assertEqual(reusable(directory, request)['status'], 'ok')
            changed = dict(request, seconds=14.)
            with self.assertRaises(ValueError): reusable(directory, changed)
            snapshot = directory/'N2'
            record = replay(graph, SETTINGS.config, snapshot, Path(temporary)/'replay', timeout=15)
            self.assertEqual(record['official_makespan'], 6)
            with patch('q2.optimized.api.run_official', return_value=None):
                failed = Path(temporary)/'failed'
                self.assertEqual(replay(graph, SETTINGS.config, snapshot, failed)['status'], 'FAILED')
                self.assertFalse((failed/'verified.json').exists())
            (snapshot/'plan.json').write_text('{}')
            with self.assertRaises(ValueError): frozen_output(snapshot)
            missing = solve_case(graph, SETTINGS.config, [2], Path(temporary)/'missing', seconds=15,
                options={'calibration':'off', 'baseline_guard':False, 'max_batches':1, 'output_policy':'best-exact'})
            self.assertEqual(missing['rows'][0]['status'], 'NO_EXACT_STATE')

    def test_batch_complete_instance_resume(self):
        graph = ROOT/'tests/fixtures/q2_repair/minimal.json'
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/'batch'
            kwargs = dict(seconds=15., options={'calibration':'off','baseline_guard':False,'max_batches':1})
            first = batch([graph], SETTINGS.config, [2], directory, **kwargs)
            again = batch([graph], SETTINGS.config, [2], directory, resume=True, **kwargs)
            self.assertEqual(first, again)
            with self.assertRaises(ValueError):
                batch([graph], SETTINGS.config, [2], directory, resume=True, seconds=14., options=kwargs['options'])

    def test_baseline_guard_finishes_beta_and_publishes_no_worse_exact(self):
        graph = ROOT/'tests/fixtures/q2_repair/minimal.json'
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)/'guard'
            result = solve_case(graph, SETTINGS.config, [2,3], directory, seconds=15., options={'max_batches':1})
            for row in result['rows']:
                self.assertEqual(row['status'], 'SUCCESS_EXACT_UNREPLAYED')
                self.assertTrue(row['baseline_guard']['complete'])
                self.assertLessEqual(row['makespan'], row['baseline_guard']['baseline_makespan'])
                checkpoint = read_json(directory/f'N{row["cores"]}'/'checkpoint.json')
                self.assertFalse(checkpoint['beta']['pending'])
                self.assertEqual(set(checkpoint['beta']['stops'].values()), {'MAX_BETA'})

    def test_interrupted_runtime_recovers_valid_checkpoint_without_complete_marker(self):
        graph=ROOT/'tests/fixtures/q2_repair/minimal.json'
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)/'normal'
            solve_case(graph,SETTINGS.config,[2],output,seconds=15.,options={'max_batches':1})
            request=read_json(output/'request.json')
            interrupted=Path(temporary)/'interrupted'; (interrupted/'N2').mkdir(parents=True)
            for name in ('checkpoint.json','request.json'):
                shutil.copyfile(output/'N2'/name,interrupted/'N2'/name)
            progress=dict(cores=2,phase='search_batch',wall_seconds=1.,per_core={'2':{'wall_seconds':.5,'cpu_seconds':.1}})
            (interrupted/'progress.json').write_text(json.dumps(progress))
            result=recover(request,interrupted,perf_counter()-30,False,True,{})
            self.assertEqual(result['status'],'hard_timeout_with_checkpoint')
            self.assertEqual(result['rows'][0]['makespan'],6)
            self.assertGreaterEqual(result['rows'][0]['timing']['wall_seconds'],29.5)
            self.assertFalse((interrupted/'complete.json').exists())
            frozen_output(interrupted/'N2')

    def test_protected_publication_rejects_better_estimate_after_exact_regression(self):
        g, state = example()
        with tempfile.TemporaryDirectory() as temporary:
            service = EvaluationService(g, 'unused', temporary)
            session = SearchSession(service, 3, parameters(), SearchBudget(60))
            original, trace = service.refined(state)
            original = replace(original, score=exact_score(state, (100,0)))
            child = service.state(next(order_proposals(g, state)), state)
            proposal, childtrace = service.refined(child)
            proposal = replace(proposal, score=replace(proposal.score, makespan=90))
            session.pool.update([original, proposal])
            regression = replace(proposal, score=exact_score(child,(120,0)))
            with patch.object(service, 'official', return_value=(regression, {'status':'OK'})):
                session.protection.finish()
            self.assertEqual(session.pool.incumbents.search.score.makespan, 100)
            self.assertEqual(session.pool.incumbents.exact.state, state)


if __name__ == '__main__': unittest.main()
