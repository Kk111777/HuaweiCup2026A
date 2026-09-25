import unittest
from unittest.mock import patch
from copy import deepcopy
from test_q1_initialization import graph_of, SETTINGS
from q1 import official
from q1.model import Graph
from q1.unified.features import recognize, plan_complexity, OfficialCostModel
from q1.unified.api import default_parameters
from q1.unified.engine import search
from q1.unified.service import SearchServices


class FeatureTests(unittest.TestCase):
    def test_structure_cost_is_independent_of_hardware_cycles(self):
        raw = graph_of(20)
        graph = Graph(raw)
        big = official.make_plan([graph.order],2)
        split = official.make_plan([[v] for v in graph.order],2)
        a,b = plan_complexity(graph,big),plan_complexity(graph,split)
        self.assertGreater(a['units'], b['units'])
        changed = deepcopy(raw)
        for op in changed['ops']:op['cycles'] *= 1000
        self.assertEqual(a, plan_complexity(Graph(changed),big))
        model = OfficialCostModel(graph,big,100)
        self.assertLess(model.predict(b),model.predict(a))
        model.observe(a,dict(status='ok',seconds=20))
        self.assertEqual(model.metadata()['samples'],1)
        model.observe(b,dict(status='ok',seconds=.001,cache_hit=True))
        self.assertEqual(model.metadata()['samples'],1)

    def test_large_graph_bootstrap_screens_before_official_call(self):
        raw = graph_of(4,shared=False,cycles=[10000]*4,pipes=['PIPE_V']*4)
        graph = Graph(raw)
        service = SearchServices(graph,SETTINGS)
        features = recognize(graph,service.settings_dict)
        features['operations'] = 2000  # Exercise large-input policy on a tiny fixture.
        big = official.make_plan([graph.order],2)
        split = official.make_plan([[v] for v in graph.order],2)
        split['core_schedules'] = [[0,2],[1,3]]
        value = official.evaluate(raw,big,SETTINGS)
        class Constructor:
            def next(self):return 'growth_test',big,1
            def observe(self,name,result):pass
        params = default_parameters()
        params.update(max_rounds=0,max_initial_evaluations_per_core=0)
        with patch('q1.unified.engine.initial_candidates',return_value=[('split_test',split,1)]), \
             patch.object(service,'evaluate',wraps=service.evaluate) as evaluate:
            search(graph,2,service.settings_dict,params,600,service,
                   seed=(big,value),constructor=Constructor())
        self.assertEqual(evaluate.call_args_list[0].args[0],split)
        measured=[r for r in service.history['records'] if r.get('evaluation_source')=='official']
        self.assertEqual(measured[0]['action'],'split_test')


if __name__ == '__main__':unittest.main()
