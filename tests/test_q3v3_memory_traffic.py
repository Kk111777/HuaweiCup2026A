"""Semantic guards for the new objective, exact memory prefix and full seeds."""
from pathlib import Path
from fractions import Fraction
import json
import sys
import tempfile
import unittest
import subprocess
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from q3v3.traffic_objective import TrafficPolicy,select,pareto
from q3v3.official import evaluate,evaluate_file,replay,read_settings,write_json
from q3v3.graph import Graph
from q3v3.memory_search import MemoryService,capacity_candidates
from q3v3.capacity_window import CapacityWindow,minimum_disruption,apply_window
from q3v3.state import PlanState
from q3v3.regions import operation_cores
from q3v3.search import solve_one


def result(t=10000,d=100000,a=80000):
    return dict(makespan=t,cache_stats=dict(hit_bytes=0,miss_bytes=d),
        data_movement_bytes=dict(original_graph_copy_bytes=20000,scheduled_copy_bytes=d,
        added_copy_bytes=a,spill_added_copy_bytes=a//2,partition_added_copy_bytes=a-a//2))


def memory_graph():
    # Interleaving two 80 KB UB values overflows 128 KB; finish each first.
    raw=dict(ops=[dict(id=i,op='ADD',pipe='PIPE_V',cycles=10) for i in range(4)],
        tensors=[dict(id=100,pos='UB',size=80000),dict(id=101,pos='UB',size=80000)],
        edges=[dict(source=0,target=100),dict(source=100,target=2),
               dict(source=1,target=101),dict(source=101,target=3)])
    plan=dict(node_to_subgraph={str(i):i for i in range(4)},core_schedules=[[0,1,2,3]])
    return raw,plan


class PolicyTests(unittest.TestCase):
    def test_small_time_loss_can_buy_large_traffic_reduction(self):
        p=TrafficPolicy.from_result(result())
        self.assertLess(p.key(result(t=10002,d=30000,a=30000)),p.key(result()))
        self.assertTrue(p.eligible(result(t=10100),10000))
        self.assertFalse(p.eligible(result(t=10101,d=0,a=0),10000))

    def test_fastest_update_invalidates_prior_guard_even_when_score_is_good(self):
        p=TrafficPolicy.from_result(result())
        xs=[dict(digest=str(i),result=r) for i,r in enumerate([result(),result(10050,1000,0),result(9900)])]
        chosen,fast=select(xs,p)
        self.assertEqual(fast['result']['makespan'],9900)
        self.assertNotEqual(chosen['result']['makespan'],10050)
        self.assertEqual(p.reference_cycles,10000)

    def test_zero_added_and_invalid_parameters(self):
        p=TrafficPolicy.from_result(result(a=0))
        self.assertEqual(p.reference_added,20000)
        for value in (float('nan'),float('inf'),-1):
            with self.assertRaises(ValueError): TrafficPolicy.from_result(result(),tolerance=value)

    def test_pareto_keeps_time_traffic_tradeoffs(self):
        xs=[dict(digest=str(i),result=r) for i,r in enumerate([result(),result(10050,1000,0),result(11000,200000,90000)])]
        self.assertEqual(len(pareto(xs)),2)


class MemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config=ROOT/'official/data/config.txt';cls.settings=read_settings(cls.config)

    def test_capacity_window_frees_before_other_alloc_and_matches_official(self):
        raw,plan=memory_graph();g=Graph(raw)
        service=MemoryService(g,self.settings);bundle=service.get(plan)
        original=evaluate(raw,plan,self.settings)
        self.assertEqual(sum(c['spill_bytes'] for c in bundle['cores'].values()),
                         original['data_movement_bytes']['spill_added_copy_bytes'])
        w=CapacityWindow(bundle,0,[[0],[1],[2],[3]],self.settings['capacity'])
        answer=minimum_disruption(w)
        self.assertEqual(answer['status'],'OPTIMAL_IN_FIXED_WINDOW')
        new,fresh=apply_window(g,plan,bundle,w,answer,service.prefix)
        after=evaluate(raw,new,self.settings)
        self.assertLess(after['data_movement_bytes']['spill_added_copy_bytes'],original['data_movement_bytes']['spill_added_copy_bytes'])
        self.assertEqual(operation_cores(PlanState(g,new)),operation_cores(PlanState(g,plan)))

    def test_generated_capacity_candidate_has_real_spill_reduction(self):
        raw,plan=memory_graph();g=Graph(raw);before=evaluate(raw,plan,self.settings)
        cs=list(capacity_candidates(g,plan,before,self.settings,MemoryService(g,self.settings)))
        self.assertTrue(cs)
        self.assertTrue(any(evaluate(raw,c.plan,self.settings)['data_movement_bytes']['spill_added_copy_bytes'] <
                            before['data_movement_bytes']['spill_added_copy_bytes'] for c in cs))

    def test_complete_seed_portfolio_and_two_official_deliverables(self):
        raw,plan=memory_graph()
        better=dict(node_to_subgraph=dict(plan['node_to_subgraph']),core_schedules=[[0,2,1,3]])
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);write_json(p/'graph.json',raw);write_json(p/'q1.json',plan);write_json(p/'q2.json',better)
            summary,_=solve_one(p/'graph.json',1,self.config,p/'run',warm_plan=p/'q1.json',
                q2_warm_plan=p/'q2.json',objective_policy='cycles-traffic',seconds=2,max_evaluations=5,
                evaluation_timeout=30,enable_global=False,enable_joint=False,enable_structural=False)
            self.assertEqual(set(summary['seed_results']),{'q1','q2'})
            self.assertLessEqual(summary['slowdown_from_fastest_percent'],1.00001)
            self.assertTrue((p/'run/fastest/verified/trace.json').exists())
            self.assertEqual(summary['warm_start_mode'],'complete_solution_portfolio')

    def test_candidate_subprocess_timeout_keeps_verified_incumbent(self):
        raw,plan=memory_graph()
        calls=0
        def fail_after_seed(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls>1: raise subprocess.TimeoutExpired('candidate worker',.01)
            return evaluate_file(*args,**kwargs)
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);write_json(p/'graph.json',raw);write_json(p/'seed.json',plan)
            with patch('q3v3.traffic_search.evaluate_file',side_effect=fail_after_seed):
                summary,_=solve_one(p/'graph.json',1,self.config,p/'run',warm_plan=p/'seed.json',
                    objective_policy='cycles-traffic',seconds=2,max_evaluations=4,
                    evaluation_timeout=30,enable_global=False,enable_joint=False,enable_structural=False)
            self.assertGreater(summary['rejected_calls'],0)
            self.assertEqual(summary['status'],'ok')
            self.assertTrue((p/'run/verified/replay.json').exists())

    def test_cross_core_prefix_matches_official_copy_and_spill(self):
        raw,plan=memory_graph();plan['core_schedules']=[[0,1],[2,3]]
        g=Graph(raw);service=MemoryService(g,self.settings);bundle=service.get(plan)
        actual=evaluate(raw,plan,self.settings)['data_movement_bytes']
        self.assertTrue(bundle['cross_links'])
        predicted=service.record(plan,bundle)
        for key in ('partition_added_copy_bytes','spill_added_copy_bytes'):
            self.assertEqual(predicted[key],actual[key])

    def test_prepared_seed_rejects_changed_plan_hash(self):
        from q3v3.traffic_search import cached_seed
        raw,plan=memory_graph()
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);write_json(p/'graph.json',raw);write_json(p/'seed.json',plan)
            replay(p/'graph.json',p/'seed.json',self.config,p/'verified',timeout=30)
            plan['core_schedules']=[[0,2,1,3]];write_json(p/'seed.json',plan)
            with self.assertRaisesRegex(ValueError,'plan hash differs'):
                cached_seed(dict(result=str(p/'verified/result.json'),replay=str(p/'verified/replay.json')),
                    p/'graph.json',p/'seed.json',self.config,self.settings)


if __name__=='__main__': unittest.main()
