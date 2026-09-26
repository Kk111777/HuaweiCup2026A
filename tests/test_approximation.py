"""Compare the screening simulator against independent official small-graph events."""
import unittest
import random
from copy import deepcopy
from unittest.mock import patch
from test_q1_initialization import graph_of, SETTINGS
from q1 import official
from q1.model import Graph
from q1.improvement.incremental import IncrementalEvaluator
from q1.unified.features import recognize

PARAMS = dict(capacity=SETTINGS.capacity, bandwidth=SETTINGS.bandwidth,
              task_same_core_wait_cycles=SETTINGS.same_core_wait,
              task_cross_core_wait_cycles=SETTINGS.cross_core_wait)


class ApproximationTests(unittest.TestCase):
    def test_no_spill_random_events_and_cache(self):
        rng = random.Random(20260925)
        for case in range(24):
            n = rng.randrange(3, 10)
            links = [(i,j) for i in range(n) for j in range(i+1,n) if rng.random()<.2]
            raw = graph_of(n, links, cycles=[rng.randrange(1,100) for _ in range(n)],
                           pipes=[rng.choice(['PIPE_V','PIPE_M']) for _ in range(n)],
                           size=rng.randrange(1,300))
            graph = Graph(raw)
            cores = 2 + case % 3
            plan = official.make_plan([[i] for i in range(n)], cores)
            plan['core_schedules'] = [list(range(c,n,cores)) for c in range(cores)]
            model = IncrementalEvaluator(graph, PARAMS, {})
            measured = official.evaluate(raw, plan, SETTINGS)
            expected = measured['makespan']
            with self.subTest(case=case):
                actual, meta = model.estimate(plan)
                self.assertEqual(actual, expected)
                self.assertEqual(meta['pressure_risk_tasks'], 0)
                self.assertEqual(model.estimate(plan)[0], actual)
                self.assertGreater(model.stats['plan_cache_hits'], 0)
                prediction = model.result(plan)['data_movement_bytes']
                for name in ('original_graph_copy_bytes', 'scheduled_copy_bytes',
                             'added_copy_bytes', 'partition_added_copy_bytes'):
                    self.assertEqual(prediction[name], measured['data_movement_bytes'][name])
                model.observe(plan, measured)
                self.assertEqual(model.result(plan)['data_movement_bytes'], prediction)

    def test_copy_basis_exists_before_any_official_evaluation(self):
        graph = Graph(graph_of(3, [(0, 1), (1, 2)], size=60))
        plan = official.make_plan([graph.order], 2)
        with patch.object(official, 'singlecore', side_effect=AssertionError('singlecore called')), \
             patch.object(official, 'evaluate', side_effect=AssertionError('official called')):
            model = IncrementalEvaluator(graph, PARAMS, {})
            value = model.result(plan)
        self.assertEqual(model.original_copy_bytes, 120)
        self.assertEqual(value['data_movement_bytes']['added_copy_bytes'], 0)
        self.assertEqual(value['data_movement_bytes']['original_graph_copy_bytes'], 120)
        self.assertEqual(len(model.calibration), 0)

    def test_copy_features_count_the_official_side_of_each_copy(self):
        raw = graph_of(2, [(0, 1)], size=60)
        for tensor in raw['tensors']:
            if tensor['pos'] == 'DDR':
                tensor['size'] = 7
        graph = Graph(raw)
        from multicore_cut_evaluate_problem_1 import _copy_traffic_bytes
        expected = _copy_traffic_bytes(raw)
        self.assertEqual(expected, 120)
        self.assertEqual(recognize(graph, PARAMS)['original_copy_bytes'], expected)
        self.assertEqual(IncrementalEvaluator(graph, PARAMS, {}).original_copy_bytes, expected)

    def test_calibration_rejects_movement_from_a_different_graph(self):
        graph = Graph(graph_of(3))
        plan = official.make_plan([graph.order], 2)
        result = official.evaluate(graph.raw, plan, SETTINGS)
        model = IncrementalEvaluator(graph, PARAMS, {})
        wrong = deepcopy(result)
        wrong['data_movement_bytes']['original_graph_copy_bytes'] += 1
        with self.assertRaisesRegex(ValueError, 'does not match'):
            model.observe(plan, wrong)
        self.assertEqual(len(model.calibration), 0)

    def test_spill_calibration_does_not_penalize_pressure_free_plan(self):
        graph = Graph(graph_of(3))
        plan = official.make_plan([[0],[1],[2]], 2)
        model = IncrementalEvaluator(graph, PARAMS, {})
        before, _ = model.estimate(plan)
        model.observe(plan, {'makespan':before*3,
                            'data_movement_bytes':{'spill_added_copy_bytes':100}})
        after, detail = model.estimate(plan)
        self.assertEqual(before, after)
        self.assertEqual(detail['calibration_factor'], 1)

    def test_multi_operation_task_pipe_order(self):
        rng = random.Random(73196)
        for case in range(32):
            n = 12
            raw = graph_of(n,[(i,j) for i in range(n) for j in range(i+1,n) if rng.random()<.12],
                           cycles=[rng.randrange(1,500) for _ in range(n)],
                           pipes=[rng.choice(['PIPE_M','PIPE_V']) for _ in range(n)],
                           size=rng.randrange(1,900))
            blocks = [list(range(i,i+3)) for i in range(0,n,3)]
            plan = official.make_plan(blocks,2)
            plan['core_schedules'] = [[0,2],[1,3]]
            model = IncrementalEvaluator(Graph(raw),PARAMS,{})
            with self.subTest(case=case):
                self.assertEqual(model.estimate(plan)[0],official.evaluate(raw,plan,SETTINGS)['makespan'])


if __name__ == '__main__':
    unittest.main()
