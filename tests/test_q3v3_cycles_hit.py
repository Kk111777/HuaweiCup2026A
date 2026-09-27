"""Requested lexicographic objective and real official cache scheduling cases."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from q3v3.beam import retain
from q3v3.cache_search import cache_candidates, cache_opportunities
from q3v3.graph import Graph
from q3v3.neighborhoods import Candidate
from q3v3.objective import key, improvement, record
from q3v3.official import evaluate, read_settings, write_json
from q3v3.search import solve_one
from q3v3.state import PlanState


def plateau():
    raw = dict(ops=[dict(id=i, op='ADD', pipe='PIPE_V', cycles=100 if i < 3 else 10000) for i in range(4)],
               tensors=[dict(id=100, pos='UB', size=12000), dict(id=101, pos='UB', size=18000)],
               edges=[dict(source=100,target=0),dict(source=100,target=1),dict(source=101,target=2)])
    plan = dict(node_to_subgraph={str(i):i for i in range(4)}, core_schedules=[[0],[1,2],[3]])
    return raw, plan


def result(cycles=100, hit=10, miss=90, added=20):
    return dict(makespan=cycles, cache_stats=dict(hit_bytes=hit, miss_bytes=miss),
                data_movement_bytes=dict(scheduled_copy_bytes=hit+miss+10, added_copy_bytes=added))


class CyclesHitTests(unittest.TestCase):
    def test_one_cycle_always_dominates_hit_rate(self):
        self.assertLess(key(result(100,0,100)), key(result(101,100,0)))

    def test_equal_cycles_use_rate_not_hit_count_or_copy_bytes(self):
        a, b = result(hit=20,miss=180,added=0), result(hit=15,miss=5,added=999)
        self.assertLess(key(b), key(a))
        self.assertEqual(improvement(a,b),'byte_hit_rate_at_equal_cycles')
        self.assertLess(key(a,'cycles-copy'),key(b,'cycles-copy'))
        json.dumps(record(b))

    def test_exact_fraction_and_empty_read_convention(self):
        n = 10**20
        self.assertLess(key(result(hit=n,miss=n-1)),key(result(hit=n,miss=n)))
        self.assertEqual(key(result(hit=0,miss=0))[1],0)
        with self.assertRaises(ValueError):
            key(result(), 'weighted')

    def test_beam_retains_same_placement_with_distinct_cache_order(self):
        items = [dict(digest=str(i),result=result(100+i),placement=p,cache_signature=c)
                 for i,(p,c) in enumerate((('A','X'),('A','X'),('B','X'),('C','X'),('A','Y')))]
        kept = retain(items,'cycles-hit')
        self.assertEqual([i['digest'] for i in kept],['0','1','2','4'])


class RealCacheSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = ROOT/'official/data/config.txt'
        cls.settings = read_settings(cls.config)

    def test_joint_cache_moves_improve_hit_on_true_cycle_plateau(self):
        raw, plan = plateau()
        graph = Graph(raw)
        original = evaluate(raw, plan, self.settings)
        before = copy.deepcopy(plan)
        candidates = list(cache_candidates(graph, plan, original, self.settings))
        outcomes = [evaluate(raw, p.plan, self.settings) for p in candidates]
        self.assertTrue(any(r['makespan']==original['makespan'] and key(r)<key(original) for r in outcomes))
        self.assertTrue(any(c.family.startswith('l2_joint') for c in candidates))
        self.assertEqual(plan,before)
        for c in candidates:
            state = PlanState(graph,c.plan)
            self.assertEqual(set(state.owner),set(graph.ops))

    def test_no_independent_read_does_not_invent_a_wait(self):
        raw, plan = plateau()
        raw['edges'] = raw['edges'][:2]
        graph = Graph(raw)
        original = evaluate(raw,plan,self.settings)
        self.assertEqual(list(cache_candidates(graph,plan,original,self.settings)),[])

    def test_eviction_episode_clears_after_reinsertion(self):
        raw, plan = plateau()
        graph = Graph(raw)
        original = evaluate(raw,plan,self.settings)
        hints,_ = cache_opportunities(PlanState(graph,plan),original,self.settings)
        self.assertTrue(any(h['kind']=='inflight' for h in hints))
        e = next(e for e in original['cache_events'] if e['event']=='miss')
        synthetic = copy.deepcopy(original)
        synthetic['cache_events'] = [dict(e,event='insert',tensor_id=999,evicted_tensor_ids=[e['tensor_id']]),
            dict(e,event='insert',evicted_tensor_ids=[]),e]
        hints,_ = cache_opportunities(PlanState(graph,plan),synthetic,self.settings)
        self.assertFalse(any(h['kind']=='fifo' for h in hints))

    def test_warm_only_search_continues_at_compute_bound_without_q1_code(self):
        raw, plan = plateau()
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            write_json(root/'graph.json',raw)
            write_json(root/'warm.json',plan)
            with patch('q3v3.search.export_seeds',side_effect=AssertionError('Q1 must not execute')):
                summary,_ = solve_one(root/'graph.json',3,self.config,root/'run',root/'missing-q1',
                    warm_plan=root/'warm.json',seconds=8,max_evaluations=12,evaluation_timeout=20)
            self.assertEqual(summary['makespan_cycles'],10000)
            self.assertGreater(summary['cache_hit_rate_bytes'],summary['supplied_plan_metrics']['cache_hit_rate_bytes'])
            self.assertGreater(summary['cache_only_improvements'],0)
            self.assertIsNone(summary['q1_source_provenance'])
            self.assertFalse((root/'run/q1_seed').exists())
            self.assertEqual(summary['warm_start_mode'],'full_plan_only')
            self.assertEqual(summary['verification'],'independent official problem-3 CLI with trace and hashes')

    def test_cli_accepts_n1_complete_plan_without_q1_source_and_audits(self):
        raw, plan = plateau()
        plan['core_schedules']=[[0,1,2,3]]
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            write_json(root/'graph.json',raw)
            write_json(root/'warm.json',plan)
            run=subprocess.run([sys.executable,str(ROOT/'src/solve_q3v3.py'),str(root/'graph.json'),
                '--cores','1','--warm-start',str(root/'warm.json'),'--q1-source',str(root/'missing-q1'),
                '--output-dir',str(root/'run'),'--max-evaluations','3','--seconds','3','--workers','1'],
                capture_output=True,text=True,encoding='utf-8',timeout=30)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            from report_q3v3 import report
            audit=report(root/'run')
            self.assertEqual(audit['status'],'passed')
            self.assertEqual(audit['q1_source_versions'],[])

    def test_secondary_gain_does_not_discard_untried_cycle_neighborhood(self):
        raw, plan = plateau()
        # A mandatory output copy leaves a positive gap to the compute bound.
        raw['tensors'].append(dict(id=102,pos='UB',size=16))
        raw['edges'].append(dict(source=3,target=102))
        graph=Graph(raw)
        original=evaluate(raw,plan,self.settings)
        first=next(c for c in cache_candidates(graph,plan,original,self.settings)
                   if improvement(original,evaluate(raw,c.plan,self.settings))=='byte_hit_rate_at_equal_cycles')
        second=copy.deepcopy(plan)
        second['core_schedules'].reverse()
        candidates=[first,Candidate(second,'order','Untried continuation of the same cycle neighborhood')]
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            write_json(root/'graph.json',raw)
            write_json(root/'warm.json',plan)
            with patch('q3v3.search.construction_candidates',return_value=iter(())), \
                 patch('q3v3.neighborhoods.generate_candidates',return_value=candidates) as generate:
                summary,_=solve_one(root/'graph.json',3,self.config,root/'run',warm_plan=root/'warm.json',
                    seconds=8,max_evaluations=3,evaluation_timeout=20,near_bound_fraction=0,
                    enable_l2=False,enable_global=False,enable_joint=False)
            self.assertEqual(summary['official_calls'],3)
            self.assertGreater(summary['cache_only_improvements'],0)
            self.assertEqual(generate.call_count,1,'A secondary improvement must not restart the cycle neighborhood')


if __name__ == '__main__':
    unittest.main()
