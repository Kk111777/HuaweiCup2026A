"""Compare the screening simulator against independent official small-graph events."""
import unittest
import random
from test_q1_initialization import graph_of, SETTINGS
from q1 import official
from q1.model import Graph
from q1.improvement.incremental import IncrementalEvaluator

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
            expected = official.evaluate(raw, plan, SETTINGS)['makespan']
            with self.subTest(case=case):
                actual, meta = model.estimate(plan)
                self.assertEqual(actual, expected)
                self.assertEqual(meta['pressure_risk_tasks'], 0)
                self.assertEqual(model.estimate(plan)[0], actual)
                self.assertGreater(model.stats['plan_cache_hits'], 0)

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
