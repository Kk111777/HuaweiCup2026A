"""User configuration must remain observable across automatic profiles."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from test_q1_initialization import graph_of, SETTINGS
from q1 import official
from q1.model import Graph
from q1.unified.api import default_parameters
from q1.unified.backend import get_problem_definition
from scheduling.budget import CaseSliceBudget, SearchBudget
from scheduling.configuration import apply_structure_overrides
from scheduling.engine import search
from scheduling.service import SearchServices


class ConfigurationTests(unittest.TestCase):
    def test_explicit_override_survives_automatic_profile_without_mutating_input(self):
        problem = get_problem_definition()
        graph = Graph(graph_of(5))
        plan = official.make_plan([graph.order], 2)
        result = official.evaluate(graph.raw, plan, SETTINGS)
        parameters = default_parameters()
        parameters.update(enable_initial_candidates=False, max_rounds=0)
        parameters['structure_overrides'] = {'max_proposals_per_round': 7, 'stage_widths': [3, 9]}
        before = deepcopy(parameters)
        service = SearchServices(graph, SETTINGS, problem=problem)
        with patch('time.perf_counter', return_value=0.):
            budget = CaseSliceBudget(SearchBudget(60., started=0.), 1)
            _, _, history = search(graph, 2, service.settings_dict, parameters, 60., service,
                                   problem=problem, seed=(plan, result), started=0., budget=budget)
        self.assertEqual(parameters, before)
        self.assertEqual(history['requested_params'], before)
        self.assertEqual(history['effective_params']['max_proposals_per_round'], 7)
        self.assertEqual(history['effective_params']['stage_widths'], [3, 9])
        self.assertNotEqual(history['structure_control']['max_proposals_per_round'], 7)

    def test_invalid_override_cannot_change_backend_hardware_or_uncontrolled_options(self):
        allowed = ('width',)
        for overrides, error in (({'problem': 'q3'}, ValueError), ({'bandwidth': 1}, ValueError),
                                 ({'width': True}, TypeError), ({'width': -1}, ValueError),
                                 ('invalid', TypeError)):
            with self.subTest(overrides=overrides), self.assertRaises(error):
                apply_structure_overrides({'width': 4, 'structure_overrides': overrides}, allowed)


if __name__ == '__main__':
    unittest.main()
