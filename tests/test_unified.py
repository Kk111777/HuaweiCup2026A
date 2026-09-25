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
from q1.unified.api import solve_file,default_parameters
from q1.unified.budget import SearchBudget,run_bounded
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

    def test_arbitrary_graph_fresh_baseline_and_independent_replay(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d); graph=d/'previously-unseen-dag.json'
            raw=graph_of(11,[(0,3),(1,3),(2,4),(3,7),(4,8),(7,10),(8,10)])
            write(graph,raw)
            parameters=default_parameters();parameters['max_rounds']=4
            result=solve_file(graph,official.REFERENCE/'data/config.txt',3,d/'answer',
                              seconds=15,parameters=parameters)
            self.assertEqual(result['status'],'ok',result)
            self.assertEqual(result['baseline']['mode'],'fresh')
            self.assertFalse(result['local_test_cache'])
            self.assertFalse(result['parallel_solver'])
            self.assertGreater(result['timing']['singlecore_seconds'],0)
            self.assertEqual(result['wall_seconds'],result['accounted_wall_seconds'])
            plan=read(d/'answer/plan.json');expected=read(d/'answer/evaluation.json')
            self.assertEqual(normalized(expected),normalized(official.evaluate(raw,plan,SETTINGS)))
            records=read(d/'answer/search.json')['records']
            self.assertTrue(any(r['action'].startswith('growth_') for r in records))
            self.assertTrue(any(not r['action'].startswith('growth_') for r in records))


if __name__=='__main__':unittest.main()
