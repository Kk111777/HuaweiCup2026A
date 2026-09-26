"""Production wiring: cache feedback, published snapshots, budgets and isolation."""
from dataclasses import replace
from pathlib import Path
import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from q2 import pipeline
from q2.official import Settings, Q2Error, read_json
from q2.graph import Graph
from q2.beta import PlateauBeta
from q2.construct import construct_candidate, BudgetExpired
from q2.encoding import encode_candidate
from q2.candidates import CandidateEvaluator, capacity_repair_candidates
from test_q2_repairs import graph_fixture, prefix_for


class RepairPipelineTests(unittest.TestCase):
    def options(self):
        options = pipeline.load_options()
        options['runtime'].update(wall_budget_seconds=30, final_verification_reserve_seconds=5)
        return options

    def test_published_snapshot_cache_plateau_and_resume(self):
        options = self.options()
        graph = ROOT/'tests/fixtures/q2/minimal.json'
        with tempfile.TemporaryDirectory() as directory:
            result = pipeline.solve(graph, 2, directory, options)
            self.assertEqual(result['final_objective'], (6, 0))
            self.assertEqual(result['official_evaluations'], 2)
            self.assertTrue(result['search_complete'])
            self.assertEqual(result['enhancement_gate'], 'NO_REAL_SPILL')
            self.assertEqual(result['windows'], [])
            for stream in result['search']['stops']:
                self.assertEqual([r['beta'] for r in result['search']['records'] if r['stream'] == stream], [1,2,3,4])
            target = Path(directory)/'minimal/N2'
            snapshot = pipeline.load_baseline_result(target)
            self.assertEqual(snapshot.evaluation['makespan'], 6)
            self.assertTrue((target/result['selected_candidate']['result_path']).is_file())
            view = snapshot.plan; view['core_schedules'][0].clear()
            self.assertNotEqual(view, snapshot.plan)
            self.assertTrue(pipeline.solve(graph, 2, directory, options, resume=True)['resumed'])
            old = pipeline.load_options(ROOT/'configs/q2/legacy_1_1.json')
            with self.assertRaises(Q2Error): pipeline.solve(graph, 2, directory, old, resume=True)
            (target/'baseline_state.json').write_text('{}')
            with self.assertRaises(Q2Error): pipeline.load_baseline_result(target)

    def test_cache_hit_checks_new_fidelity_claim(self):
        g,_ = graph_fixture(0,2)
        draft = construct_candidate(g,2,'global',1)
        candidate = encode_candidate(g,2,draft,'commit')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d); graph = path/'graph.json'; graph.write_text(json.dumps(g.raw))
            service = CandidateEvaluator(g, graph, path, pipeline.Budget(self.options()['runtime']),
                                         [], [], pipeline.run_official, 'fixture')
            service.evaluate(candidate)
            bad = replace(candidate, intended_order=tuple(reversed(draft.commit_order)))
            with self.assertRaises(AssertionError): service.evaluate(bad)
            self.assertEqual(len(service.calls), 1)

    def test_interrupted_growth_preserves_verified_component(self):
        with tempfile.TemporaryDirectory() as d, patch('q2.candidates.construct_candidate', side_effect=BudgetExpired()):
            result = pipeline.solve(ROOT/'tests/fixtures/q2/minimal.json', 2, d, self.options())
            self.assertEqual(result['status'], 'SUCCESS_VERIFIED')
            self.assertFalse(result['search_complete'])
            self.assertEqual(result['selected_candidate']['family'], 'component')
            self.assertEqual(result['official_calls'][-1]['phase'], 'final_verification')

    def test_failed_final_verification_does_not_publish(self):
        original = pipeline.run_official
        def evaluate(graph, plan, settings, directory, timeout, calls, phase):
            if phase == 'final_verification':
                calls.append(dict(phase=phase,status='EVAL_TIMEOUT',launched=False,wall_seconds=0))
                return None
            return original(graph, plan, settings, directory, timeout, calls, phase)
        with tempfile.TemporaryDirectory() as d, patch.object(pipeline, 'run_official', side_effect=evaluate):
            result = pipeline.solve(ROOT/'tests/fixtures/q2/minimal.json', 2, d, self.options())
            self.assertEqual(result['status'], 'EVAL_TIMEOUT')
            self.assertFalse((Path(d)/'minimal/N2').exists())

    def test_capacity_batch_uses_frozen_parent_and_real_gate(self):
        settings = Settings.read()
        raw = read_json(ROOT/'tests/fixtures/q2_repair/memory_interleaved.json')
        plan = read_json(ROOT/'tests/fixtures/q2_repair/memory_interleaved_multicore_res.json')
        g = Graph(raw, settings)
        from q2.contracts import ConstructionDraft, EncodedCandidate
        from q2.encoding import plan_core_map
        core = plan_core_map(g, plan)
        draft = ConstructionDraft(core, tuple(g.order), (tuple(g.order),), json.dumps(plan), 'control','control',None,'{}')
        candidate = EncodedCandidate(json.dumps(plan), draft, 'legacy', None)
        options = self.options(); options['enhancement']['block_operations'] = 1
        with tempfile.TemporaryDirectory() as d, patch('q2.candidates.component_candidates', return_value=iter([candidate])), \
                patch('q2.candidates.construct_candidate', side_effect=BudgetExpired()):
            graph = Path(d)/'control.json'; graph.write_text(json.dumps(raw))
            result = pipeline.solve(graph, 2, Path(d)/'out', options)
            self.assertEqual(result['baseline_objective'][0], 3736)
            self.assertEqual(result['final_objective'][0], 2302)
            self.assertEqual(result['data_movement_bytes']['spill_added_copy_bytes'], 0)
            self.assertEqual(candidate.plan, plan)
            for row in result['windows']:
                self.assertEqual(row['parent_plan_hash'], result['baseline_plan_hash'])

    def test_default_config_and_fixed_grid_policies(self):
        options = self.options()
        self.assertEqual(options['algorithm_version'], 'Q2-Repair-2.1')
        self.assertEqual(options['construction']['beta_search'], 'adaptive')
        for policy, expected in [('fixed',[1]),('grid',[1,2,3,4])]:
            beta = PlateauBeta(['s'],4,policy); visited=[]
            while (entry := beta.next()):
                stream,b=entry;visited.append(b);beta.observe(stream,b,(100+b,0))
            self.assertEqual(visited,expected)

    def test_controller_roundtrip_preserves_pending_and_rejects_stale(self):
        beta=PlateauBeta(['a','b']);s,b=beta.next();beta.observe(s,b,(100,1))
        state=json.loads(json.dumps(beta.state_dict()));resumed=PlateauBeta.from_state_dict(state)
        self.assertEqual(beta.next(),resumed.next())
        before=resumed.state_dict()
        with self.assertRaises(ValueError):resumed.observe('b',2,(90,0))
        self.assertEqual(before,resumed.state_dict())
        resumed.observe('b',1,(200,0))
        self.assertEqual(resumed.next(),('a',2))
        resumed.observe('a',2,(99,0))
        self.assertEqual(resumed.best['a'],(99,0))

    def test_timeout_is_not_cached_and_marks_incomplete(self):
        real = pipeline.run_official
        def once(graph, plan, settings, directory, timeout, calls, phase):
            if not calls:
                calls.append(dict(phase=phase,status='EVAL_TIMEOUT',launched=False,wall_seconds=0))
                return None
            return real(graph, plan, settings, directory, timeout, calls, phase)
        with tempfile.TemporaryDirectory() as d, patch.object(pipeline,'run_official',side_effect=once):
            result = pipeline.solve(ROOT/'tests/fixtures/q2/minimal.json',2,d,self.options())
            self.assertEqual(result['status'],'SUCCESS_VERIFIED')
            self.assertFalse(result['search_complete'])
            self.assertEqual(result['candidates'][0]['evaluation_status'],'EVAL_TIMEOUT')
            self.assertEqual(result['candidates'][1]['evaluation_status'],'OFFICIAL')

    def test_new_pipeline_windows_spawn_and_failure_isolation(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d);valid=path/'valid.json';bad=path/'bad.json'
            valid.write_bytes((ROOT/'tests/fixtures/q2/minimal.json').read_bytes())
            bad.write_text('{"ops":[],"tensors":[]}')
            result=pipeline.batch([valid,bad],[2],path/'out',self.options(),ROOT/'official',None,workers=2)
            self.assertEqual(result['successful'],1)
            self.assertFalse(result['complete'])
            self.assertTrue((path/'out/valid/N2/baseline_state.json').is_file())

    def test_windows_do_not_call_official_inside_dp(self):
        g,plan=graph_fixture(0,3)
        draft=construct_candidate(g,2,'global',1)
        candidate=encode_candidate(g,2,draft,'commit')
        bundle=prefix_for(g,candidate.plan)
        with patch.object(Settings,'evaluate',side_effect=AssertionError('full evaluator inside DP')):
            list(capacity_repair_candidates(g,candidate,bundle,self.options()['enhancement'],lambda:None,[]))


if __name__ == '__main__': unittest.main()
