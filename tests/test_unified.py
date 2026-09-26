"""Integration boundaries: arbitrary graph, total budget, serial guard and final replay."""
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from test_q1_initialization import graph_of,SETTINGS
from q1 import official
from q1.solver import initialize
from q1.plan import normalized
from q1.unified.api import solve_file,solve_case,default_parameters
from q1.unified.budget import SearchBudget,CaseSliceBudget,run_bounded
from q1.unified.io import read,write
from q1.model import Graph
from q1.unified.service import SearchServices
from q1.unified.engine import search


def busy_worker():
    until=time.perf_counter()+10
    while time.perf_counter()<until:pass


class UnifiedTests(unittest.TestCase):
    def test_slow_followup_construction_does_not_start_local_generation_after_window(self):
        raw=graph_of(3);model=Graph(raw);plan=official.make_plan([model.order],2)
        value=official.evaluate(raw,plan,SETTINGS);clock=[0.]
        class Constructor:
            calls=0
            def next(self):
                self.calls+=1
                if self.calls==2:clock[0]=901.
                return f'growth_{self.calls}',plan,1
            def observe(self,name,result):pass
        constructor=Constructor();params=default_parameters()
        params.update(enable_initial_candidates=False,max_rounds=2)
        service=SearchServices(model,SETTINGS)
        with patch('time.perf_counter',side_effect=lambda:clock[0]),patch.object(service,'evaluate',
                return_value=(value,dict(status='ok',seconds=.1))),patch('q1.unified.engine.local_candidates') as local:
            search(model,2,service.settings_dict,params,600,service,seed=(plan,value),
                   started=0,constructor=constructor)
        self.assertEqual(constructor.calls,2)
        local.assert_not_called()

    def test_started_construction_can_finish_verification_after_soft_target(self):
        raw=graph_of(3);model=Graph(raw)
        plan=official.make_plan([model.order],2)
        value=official.evaluate(raw,plan,SETTINGS)
        clock=[0.]
        class Constructor:
            def next(self):
                clock[0]=601.
                return 'growth_deliberately_slow',plan,1
            def observe(self,name,result):pass
        params=default_parameters()
        params.update(enable_initial_candidates=False,max_rounds=0)
        service=SearchServices(model,SETTINGS)
        with patch('time.perf_counter',side_effect=lambda:clock[0]), patch.object(service,'evaluate',
                return_value=(value,dict(status='ok',seconds=.1))) as evaluate:
            _,_,history=search(model,2,service.settings_dict,params,600,service,
                               seed=(plan,value),started=0,constructor=Constructor())
        self.assertEqual(evaluate.call_count,1)
        self.assertEqual(history['records'][0]['action'],'growth_deliberately_slow')

    def test_expensive_baseline_does_not_skip_first_multicore_attempt(self):
        raw=graph_of(3);model=Graph(raw);plan=official.make_plan([model.order],2)
        value=official.evaluate(raw,plan,SETTINGS)
        class Constructor:
            def next(self):return 'growth_after_slow_baseline',plan,1
            def observe(self,name,result):pass
        params=default_parameters();params.update(enable_initial_candidates=False,max_rounds=0)
        service=SearchServices(model,SETTINGS)
        with patch('time.perf_counter',return_value=700.),patch.object(service,'evaluate',
                return_value=(value,dict(status='ok',seconds=.1))) as evaluate:
            search(model,2,service.settings_dict,params,600,service,seed=(plan,value),
                   started=0,constructor=Constructor())
        self.assertEqual(evaluate.call_count,1)

    def test_extension_requires_recent_official_gain_and_keeps_absolute_start(self):
        with patch('q1.unified.budget.time.perf_counter',return_value=601):
            b=SearchBudget(600,started=0)
            self.assertFalse(b.can_start())
            b.observe({'makespan':100},{'makespan':99},{'seconds':2})
            self.assertTrue(b.can_start())
            self.assertTrue(b.extended)
        with patch('q1.unified.budget.time.perf_counter',return_value=901):
            self.assertFalse(b.can_start())
        with patch('q1.unified.budget.time.perf_counter',return_value=1201):
            self.assertFalse(b.can_start())
            self.assertEqual(b.stop_reason,'hard_limit')

    def test_hard_supervisor_covers_synchronous_generation(self):
        telemetry={};start=time.perf_counter()
        self.assertTrue(run_bounded(busy_worker,(),start+.6,telemetry=telemetry))
        self.assertLess(time.perf_counter()-start,4)
        if sys.platform=='win32':self.assertGreater(telemetry['cpu_seconds'],0)

    def test_serial_reference_guard(self):
        with self.assertRaisesRegex(ValueError,'parallelism'):
            initialize(graph_of(3),2,SETTINGS,candidate_jobs=2)

    def test_arbitrary_graph_without_baseline_and_independent_replay(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d); graph=d/'previously-unseen-dag.json'
            raw=graph_of(11,[(0,3),(1,3),(2,4),(3,7),(4,8),(7,10),(8,10)])
            write(graph,raw)
            parameters=default_parameters();parameters['max_rounds']=4
            result=solve_file(graph,official.REFERENCE/'data/config.txt',3,d/'answer',
                              seconds=15,parameters=parameters)
            self.assertEqual(result['status'],'ok',result)
            self.assertEqual(result['baseline']['mode'],'not_computed')
            self.assertFalse(result['local_test_cache'])
            self.assertFalse(result['parallel_solver'])
            self.assertEqual(result['timing']['singlecore_seconds'],0)
            self.assertEqual(result['wall_seconds'],result['accounted_wall_seconds'])
            plan=read(d/'answer/plan.json');expected=read(d/'answer/evaluation.json')
            self.assertEqual(normalized(expected),normalized(official.evaluate(raw,plan,SETTINGS)))
            records=read(d/'answer/search.json')['records']
            self.assertTrue(any(r['action'].startswith('growth_') for r in records))
            self.assertTrue(any(not r['action'].startswith('growth_') for r in records))

    def test_all_core_counts_share_graph_and_never_compute_singlecore(self):
        def in_process(target,args,deadline,telemetry=None):
            target(*args)
            return False
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);graph=d/'new-dag.json'
            raw=graph_of(11,[(0,3),(1,3),(2,4),(3,7),(4,8),(7,10),(8,10)])
            write(graph,raw)
            params=default_parameters();params.update(max_rounds=0,enable_initial_candidates=False)
            with patch('scheduling.api.run_bounded',side_effect=in_process), \
                 patch('q1.unified.api.official.singlecore',side_effect=AssertionError('baseline called')), \
                 patch('q1.unified.backend.Graph',wraps=Graph) as build:
                result=solve_case(graph,official.REFERENCE/'data/config.txt',[2,3,4,5],d/'answer',
                                  seconds=20,parameters=params)
            self.assertEqual(result['status'],'ok',result)
            self.assertEqual(build.call_count,1)
            self.assertEqual(result['budget_scope'],'case_all_cores')
            self.assertEqual(result['baseline_seconds'],0)
            self.assertEqual(result['target_seconds'],20)
            self.assertEqual(len(result['rows']),4)
            self.assertLessEqual(sum(r['wall_seconds'] for r in result['rows']),result['wall_seconds'])
            for row in result['rows']:
                folder=d/'answer'/f'N{row["cores"]}'
                self.assertEqual(row['cached_baseline_charge_seconds'],0)
                self.assertEqual(row['wall_seconds'],row['accounted_wall_seconds'])
                self.assertEqual(read(folder/'request.json')['case_cores'],[2,3,4,5])
                plan=read(folder/'plan.json')
                self.assertGreater(official.evaluate(raw,plan,SETTINGS)['makespan'],0)
                self.assertTrue((folder/'structural_seed.json').exists())

    def test_case_slice_uses_remaining_case_time_and_preserves_total_start(self):
        with patch('q1.unified.budget.time.perf_counter',return_value=20.):
            parent=SearchBudget(600,started=0)
            first=CaseSliceBudget(parent,4)
            self.assertEqual(first.target,145.)
        with patch('q1.unified.budget.time.perf_counter',return_value=100.):
            second=CaseSliceBudget(parent,3)
            self.assertAlmostEqual(second.target,500/3)
            self.assertEqual(parent.started,0)
        with patch('q1.unified.budget.time.perf_counter',return_value=601.):
            self.assertFalse(second.can_start())
        with patch('q1.unified.budget.time.perf_counter',return_value=1201.):
            self.assertFalse(second.can_start())

    def test_later_core_seed_is_refreshed_after_shared_official_calibration(self):
        checked=[]
        def in_process(target,args,deadline,telemetry=None):
            target(*args)
            return False
        def search_probe(graph,cores,settings,params,seconds,service,*,seed,**kwargs):
            expected=service.approximation.result(seed[0])['makespan']
            self.assertEqual(seed[1]['makespan'],expected)
            if cores==2:
                measured=official.evaluate(graph.raw,seed[0],SETTINGS)
                measured['makespan']*=2
                service.approximation.observe(seed[0],measured)
            else:
                self.assertGreater(len(service.approximation.calibration),0)
            checked.append(cores)
            return *seed,dict(stop_reason='test')
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);graph=d/'graph.json';write(graph,graph_of(12,shared=False))
            with patch('scheduling.api.run_bounded',side_effect=in_process), \
                 patch('scheduling.api.search',side_effect=search_probe):
                result=solve_case(graph,official.REFERENCE/'data/config.txt',[2,3,4,5],d/'out',seconds=20)
            self.assertEqual(result['status'],'ok',result)
            self.assertEqual(checked,[2,3,4,5])

    def test_officially_rejected_seed_is_not_recovered_as_a_solution(self):
        def in_process(target,args,deadline,telemetry=None):
            target(*args)
            return False
        evaluate=SearchServices.evaluate
        def reject_two(service,plan,timeout):
            if len(plan['core_schedules'])==2:
                return None,dict(status='failed',seconds=.001,error='rejected by official evaluator')
            return evaluate(service,plan,timeout)
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);graph=d/'graph.json';write(graph,graph_of(8,shared=False))
            params=default_parameters();params.update(max_rounds=0,enable_initial_candidates=False)
            with patch('scheduling.api.run_bounded',side_effect=in_process), \
                 patch('scheduling.service.SearchServices.evaluate',new=reject_two):
                result=solve_case(graph,official.REFERENCE/'data/config.txt',[2,3],d/'out',seconds=20,parameters=params)
            self.assertFalse(result['coverage_complete'])
            self.assertEqual(result['rows'][0]['status'],'failed_no_solution')
            self.assertIsNone(result['rows'][0]['makespan'])
            self.assertFalse((d/'out/N2/checkpoint.json').exists())
            self.assertFalse((d/'out/N2/plan.json').exists())
            self.assertTrue(list((d/'out/N2/rejected_checkpoints').glob('*.json')))
            self.assertEqual(result['rows'][1]['status'],'ok')
            self.assertTrue((d/'out/N3/plan.json').exists())


if __name__=='__main__':unittest.main()
