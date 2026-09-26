"""A bounded final official check for significant approximate batch improvements."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from test_q1_initialization import graph_of, SETTINGS
from q1 import official
from q1.model import Graph
from q1.plan import plan_digest
from q1.unified.api import default_parameters
from q1.unified.engine import search
from q1.unified.service import SearchServices
from scheduling.budget import SearchBudget, CaseSliceBudget
from scheduling.configuration import late_official_gain_fraction


class LateOfficialTests(unittest.TestCase):
    def run_fixture(self, threshold=.03, outcome='better', expired=False, candidate_cost=100., minimum=1):
        graph = Graph(graph_of(4, shared=False, cycles=[10000]*4, pipes=['PIPE_V']*4))
        seed = official.make_plan([graph.order], 2)
        split = official.make_plan([[v] for v in graph.order], 2)
        split['core_schedules'] = [[0, 2], [1, 3]]
        serial = deepcopy(split); serial['core_schedules'] = [[0, 1, 2, 3], []]
        value = official.evaluate(graph.raw, seed, SETTINGS)
        exact = official.evaluate(graph.raw, split, SETTINGS)
        if threshold == 'boundary':
            threshold = (value['makespan']-exact['makespan'])/value['makespan']
        clock = [0.]
        service = SearchServices(graph, SETTINGS)
        params = default_parameters(); params.update(max_rounds=3, enable_region_rebuild=False)
        params['evaluation'].update(minimum_official_checks=minimum, late_official_gain_fraction=threshold)
        class Cost:
            def predict(self, complexity): return .01 if complexity['tasks'] == 1 else candidate_cost
            def observe(self, *args): pass
            def metadata(self): return {'source': 'fixture'}
        service.cost_model = Cost()
        def generate(*args):
            clock[0] = 8.
            return [('serial', serial, 1), ('parallel', split, 2)]
        calls = []
        def evaluate(plan, timeout):
            calls.append(plan_digest(plan))
            if outcome == 'failed':
                return None, dict(status='failed', seconds=.1, timed_out=False)
            actual = dict(exact, makespan=value['makespan']*2) if outcome == 'worse' else exact
            return actual, dict(status='ok', seconds=.1)
        # A deliberately older parent clock tests that the case limit overrides
        # the current core's otherwise sufficient extension window.
        with patch('time.perf_counter', side_effect=lambda: clock[0]):
            parent = SearchBudget(10., started=-6.)
            budget = CaseSliceBudget(parent, 1) if expired else None
            if budget is not None:
                budget.target = 10.; budget.extension = 15.; budget.hard = 20.
            with patch('q1.unified.engine.initial_candidates', side_effect=generate), \
                 patch('q1.unified.engine.local_candidates', return_value=([], 0)) as local, \
                 patch.object(service, 'evaluate', side_effect=evaluate):
                plan, result, history = search(graph, 2, service.settings_dict, params,
                    10., service, seed=(seed, value), started=0., budget=budget)
        return plan, result, history, calls, seed, split, value, exact, local.call_count

    def test_batch_best_gets_one_late_check_and_stops_new_work(self):
        plan, result, h, calls, seed, split, value, exact, local_calls = self.run_fixture()
        self.assertEqual(calls, [plan_digest(split)])
        self.assertEqual((plan, result), (split, exact))
        self.assertEqual(local_calls, 0)
        self.assertEqual(len(h['late_official_checks']), 1)
        self.assertEqual(h['late_official_checks'][0]['official_reference_cycles'], value['makespan'])
        self.assertTrue(h['late_official_checks'][0]['improved_official'])
        self.assertEqual(len([r for r in h['records'] if r['evaluation_source']=='approximate']), 2)

    def test_false_positive_restores_best_official(self):
        plan, result, h, calls, seed, split, value, exact, _ = self.run_fixture(outcome='worse')
        self.assertEqual((plan, result), (seed, value))
        self.assertEqual(len(calls), 1)
        self.assertFalse(h['late_official_checks'][0]['improved_official'])

    def test_failed_official_keeps_certified_fallback(self):
        plan, result, h, calls, seed, split, value, exact, _ = self.run_fixture(outcome='failed')
        self.assertEqual((plan, result), (seed, value))
        self.assertEqual(len(calls), 1)
        self.assertEqual(h['late_official_checks'][0]['status'], 'failed')

    def test_below_threshold_and_disabled_do_not_force_verification(self):
        for threshold in (.99, None):
            plan, result, h, calls, seed, split, value, exact, _ = self.run_fixture(threshold)
            self.assertEqual(calls, [])
            self.assertEqual((plan, result), (seed, value))
            self.assertEqual(h['late_official_checks'], [])

    def test_exact_threshold_is_inclusive(self):
        plan, result, h, calls, seed, split, value, exact, _ = self.run_fixture('boundary')
        self.assertEqual(calls, [plan_digest(split)])
        self.assertEqual((plan, result), (split, exact))

    def test_case_extension_limit_blocks_extra_call(self):
        plan, result, h, calls, seed, split, value, exact, _ = self.run_fixture(expired=True)
        self.assertEqual(calls, [])
        self.assertEqual((plan, result), (seed, value))
        self.assertEqual(h['late_official_checks'], [])

    def test_normal_calibration_does_not_require_significant_gain(self):
        plan, result, h, calls, seed, split, value, exact, _ = self.run_fixture(.99, candidate_cost=.01)
        self.assertEqual(calls, [plan_digest(split)])
        self.assertEqual((plan, result), (split, exact))
        self.assertEqual(h['late_official_checks'], [])

    def test_minimum_two_does_not_override_unaffordable_cost_below_gain_threshold(self):
        plan, result, h, calls, seed, split, value, exact, _ = self.run_fixture(.99, minimum=2)
        self.assertEqual(calls, [])
        self.assertEqual((plan, result), (seed, value))
        self.assertEqual(h['minimum_calibration_grace'], [])

    def test_configuration_rejects_invalid_thresholds(self):
        self.assertEqual(late_official_gain_fraction({}), .03)
        for value in (True, '0.03', 0, 1, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                late_official_gain_fraction({'evaluation': {'late_official_gain_fraction': value}})


if __name__ == '__main__':
    unittest.main()
