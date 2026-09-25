"""Approximate checkpoint survival, official correction and operator compatibility."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from test_q1_initialization import graph_of,SETTINGS
from q1 import official
from q1.model import Graph
from q1.plan import plan_digest
from q1.improvement.incremental import IncrementalEvaluator
from q1.improvement.operators import local_candidates
from q1.unified.api import default_parameters
from q1.unified.engine import search
from q1.unified.service import SearchServices
from pathlib import Path
import sys
import tempfile
from q1.unified.io import write,read,digest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from audit_unified import replay_one


class ApproximateAcceptanceTests(unittest.TestCase):
    def fixture(self):
        raw=graph_of(4,shared=False,cycles=[10000]*4,pipes=['PIPE_V']*4)
        graph=Graph(raw)
        seed=official.make_plan([graph.order],2)
        split=official.make_plan([[v] for v in graph.order],2)
        split['core_schedules']=[[0,2],[1,3]]
        return raw,graph,seed,split,official.evaluate(raw,seed,SETTINGS)

    def test_no_time_for_official_keeps_approximate_checkpoint(self):
        raw,graph,seed,split,value=self.fixture();clock=[0.];saved=[]
        class Constructor:
            def next(self):clock[0]=1199.5;return 'growth_split',split,1
            def observe(self,name,result):pass
        service=SearchServices(graph,SETTINGS,checkpoint=lambda p,r:saved.append((p,r)))
        service.baseline_seconds=100
        params=default_parameters();params.update(enable_initial_candidates=False,max_rounds=0)
        with patch('time.perf_counter',side_effect=lambda:clock[0]),patch.object(service,'evaluate') as evaluate:
            plan,result,history=search(graph,2,service.settings_dict,params,600,service,
                seed=(seed,value),started=0,constructor=Constructor())
        evaluate.assert_not_called()
        self.assertEqual(plan,split)
        self.assertEqual(result['evaluation_source'],'approximate')
        self.assertEqual(saved[-1],(plan,result))
        self.assertEqual(history['final_candidate_check']['retained_source'],'approximate')
        self.assertLess(result['makespan'],value['makespan'])

    def test_official_correction_reranks_and_overwrites_optimistic_checkpoint(self):
        raw,graph,seed,split,value=self.fixture();saved=[]
        measured=official.evaluate(raw,split,SETTINGS)
        measured['makespan']=value['makespan']*2  # Simulate a large unmodelled penalty.
        class Constructor:
            def next(self):return 'growth_split',split,1
            def observe(self,name,result):pass
        service=SearchServices(graph,SETTINGS,checkpoint=lambda p,r:saved.append((p,r)))
        params=default_parameters();params.update(enable_initial_candidates=False,max_rounds=0)
        with patch.object(service,'evaluate',return_value=(measured,dict(status='ok',seconds=.1))):
            plan,result,history=search(graph,2,service.settings_dict,params,600,service,
                seed=(seed,value),constructor=Constructor())
        self.assertEqual(saved[0][1]['evaluation_source'],'approximate')
        self.assertEqual(plan,seed)
        self.assertEqual(result['makespan'],value['makespan'])
        self.assertNotEqual(saved[-1][1].get('evaluation_source'),'approximate')
        self.assertEqual(history['records'][-1]['incumbent_after_correction']['plan_digest'],plan_digest(seed))

    def test_approximate_timeline_supports_personal_operators_without_self_training(self):
        raw,graph,seed,split,value=self.fixture()
        service=SearchServices(graph,SETTINGS)
        model=IncrementalEvaluator(graph,service.settings_dict,{})
        model.observe(seed,value)
        result=model.result(split)
        self.assertEqual(result['makespan'],official.evaluate(raw,split,SETTINGS)['makespan'])
        proposals,_=local_candidates(graph,split,result,service.settings_dict,default_parameters())
        self.assertTrue(proposals)
        with self.assertRaisesRegex(ValueError,'must not train'):
            model.observe(split,result)
        self.assertIsNone(result['data_movement_bytes']['spill_added_copy_bytes'])

    def test_elapsed_time_triggers_official_calibration_before_batch_is_full(self):
        raw,graph,seed,split,value=self.fixture();clock=[0.]
        service=SearchServices(graph,SETTINGS);service.baseline_seconds=100
        measured=official.evaluate(raw,split,SETTINGS)
        def generate(*args):
            clock[0]=61.
            return [('split',split,1)]
        params=default_parameters();params.update(max_rounds=0)
        with patch('time.perf_counter',side_effect=lambda:clock[0]), \
             patch('q1.unified.engine.initial_candidates',side_effect=generate), \
             patch.object(service,'evaluate',return_value=(measured,dict(status='ok',seconds=10))) as evaluate:
            search(graph,2,service.settings_dict,params,600,service,seed=(seed,value),started=0)
        self.assertEqual(evaluate.call_count,1)
        exact=[r for r in service.history['records'] if r['evaluation_source']=='official']
        self.assertEqual(exact[0]['requested_reason'],'periodic')

    def test_offline_replay_preserves_prediction_and_records_realized_score(self):
        raw,graph,seed,split,value=self.fixture()
        service=SearchServices(graph,SETTINGS)
        model=IncrementalEvaluator(graph,service.settings_dict,{})
        prediction=model.result(split)
        prediction['makespan']+=123
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);config=official.REFERENCE/'data/config.txt'
            write(folder/'graph.json',raw);write(folder/'plan.json',split)
            write(folder/'evaluation.json',prediction)
            write(folder/'request.json',dict(graph=str(folder/'graph.json'),config=str(config),
                graph_sha256=digest(folder/'graph.json'),config_sha256=digest(config),source_signature='test'))
            before=digest(folder/'evaluation.json')
            replay_one(folder)
            self.assertEqual(digest(folder/'evaluation.json'),before)
            marker=read(folder/'verified.json')
            self.assertEqual(marker['solver_score_source'],'approximate')
            self.assertNotEqual(marker['solver_makespan'],marker['official_makespan'])
            self.assertFalse(marker['exact_result_match'])
            with patch('audit_unified.official.evaluate') as evaluate:
                replay_one(folder)
                evaluate.assert_not_called()


if __name__=='__main__':unittest.main()
