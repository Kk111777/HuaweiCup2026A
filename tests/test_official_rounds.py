"""Certified output, complete-round admission and submission artifact policy."""
import tempfile
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

from test_q1_initialization import graph_of, SETTINGS
from q1 import official
from q1.model import Graph
from q1.plan import normalized, plan_digest
from q1.unified.api import default_parameters, solve_case
from q1.unified.engine import search
from q1.unified.service import SearchServices
from scheduling.configuration import intermediate_artifacts
from scheduling.io import read, write


class OfficialRoundTests(unittest.TestCase):
    def test_public_default_budget_is_eight_minutes(self):
        from scheduling.api import solve_case as common_case
        from q1.unified.api import solve_file
        from scheduling.budget import SearchBudget
        for entry in (common_case, solve_case, solve_file):
            self.assertEqual(inspect.signature(entry).parameters['seconds'].default, 480)
        budget = SearchBudget(480)
        self.assertEqual((budget.target, budget.extension, budget.hard), (480, 720, 960))

    def test_second_distinct_calibration_gets_one_grace_attempt_without_replacing_better_official(self):
        graph = Graph(graph_of(4, shared=False, cycles=[10000]*4, pipes=['PIPE_V']*4))
        seed = official.make_plan([graph.order], 2)
        split = official.make_plan([[v] for v in graph.order], 2)
        split['core_schedules'] = [[0, 2], [1, 3]]
        value = official.evaluate(graph.raw, seed, SETTINGS)
        worse = dict(official.evaluate(graph.raw, split, SETTINGS), makespan=value['makespan']*2)
        for minimum in (1, 2):
            clock = [0.]
            service = SearchServices(graph, SETTINGS)
            params = default_parameters(); params['max_rounds'] = 0
            params['evaluation']['minimum_official_checks'] = minimum
            params['evaluation']['late_official_gain_fraction'] = None
            def generate(*args):
                clock[0] = 8.
                return [('split', split, 1)]
            class Cost:
                def predict(self, complexity):
                    return .01 if complexity['tasks'] == 1 else 6.
                def observe(self, *args): pass
                def metadata(self): return {'source': 'fixture'}
            service.cost_model = Cost()
            with patch('time.perf_counter', side_effect=lambda: clock[0]), \
                 patch('q1.unified.engine.initial_candidates', side_effect=generate), \
                 patch.object(service, 'evaluate', return_value=(worse, dict(status='ok', seconds=.1))) as evaluate:
                plan, result, history = search(graph, 2, service.settings_dict, params,
                    10., service, seed=(seed, value), started=0.)
            self.assertEqual(evaluate.call_count, minimum-1)
            self.assertEqual((plan, result), (seed, value))
            self.assertEqual(history['completed_official_checks'], minimum)
            if minimum == 2:
                self.assertEqual(len(history['minimum_calibration_grace']), 1)
                self.assertTrue(history['records'][-1]['minimum_calibration_attempt'])

    def test_second_check_grace_includes_candidate_generation(self):
        graph = Graph(graph_of(4, shared=False, cycles=[10000]*4, pipes=['PIPE_V']*4))
        seed = official.make_plan([graph.order], 2)
        split = official.make_plan([[v] for v in graph.order], 2)
        split['core_schedules'] = [[0, 2], [1, 3]]
        value = official.evaluate(graph.raw, seed, SETTINGS)
        worse = dict(official.evaluate(graph.raw, split, SETTINGS), makespan=value['makespan']*2)
        for minimum in (1, 2):
            clock = [8.]
            service = SearchServices(graph, SETTINGS)
            params = default_parameters(); params['max_rounds'] = 0
            params['evaluation']['minimum_official_checks'] = minimum
            params['evaluation']['late_official_gain_fraction'] = None
            def generate(*args):
                clock[0] = 10.5
                return [('split', split, 1)]
            class Cost:
                def predict(self, complexity): return .01
                def observe(self, *args): pass
                def metadata(self): return {'source': 'fixture'}
            service.cost_model = Cost()
            with patch('time.perf_counter', side_effect=lambda: clock[0]), \
                 patch('q1.unified.engine.initial_candidates', side_effect=generate) as generation, \
                 patch.object(service, 'evaluate', return_value=(worse, dict(status='ok', seconds=.1))) as evaluate:
                plan, result, history = search(graph, 2, service.settings_dict, params,
                    10., service, seed=(seed, value), started=0.)
            self.assertEqual(generation.call_count, minimum-1)
            self.assertEqual(evaluate.call_count, minimum-1)
            self.assertEqual((plan, result), (seed, value))
            self.assertEqual(history['completed_official_checks'], minimum)
            self.assertEqual(len(history['minimum_round_grace']), minimum-1)


    def test_short_remaining_window_cannot_start_generation_without_closing_reserve(self):
        graph = Graph(graph_of(4, shared=False))
        plan = official.make_plan([graph.order], 2)
        value = official.evaluate(graph.raw, plan, SETTINGS)
        service = SearchServices(graph, SETTINGS)
        params = default_parameters()
        params['max_rounds'] = 0
        with patch('time.perf_counter', return_value=14.), \
             patch('q1.unified.engine.initial_candidates') as generate:
            output, result, history = search(graph, 2, service.settings_dict, params,
                10., service, seed=(plan, value), started=0.)
        generate.assert_not_called()
        self.assertEqual(output, plan)
        self.assertEqual(result, value)
        admission = history['round_admissions'][-1]
        self.assertFalse(admission['allowed'])
        self.assertGreater(admission['required_seconds'], admission['remaining_seconds'])

    def test_timed_out_worker_recovers_best_official_not_optimistic_checkpoint(self):
        raw = graph_of(4, shared=False)
        graph = Graph(raw)
        certified = official.make_plan([graph.order], 2)
        exact = official.evaluate(raw, certified, SETTINGS)
        optimistic = dict(exact, makespan=1, evaluation_source='approximate')
        def interrupted(target, args, deadline, telemetry=None):
            output = Path(args[1]); folder = output/'N2'
            write(folder/'official_checkpoint.json', dict(plan=certified, result=exact,
                wall_seconds=.1, cpu_seconds=.1))
            write(folder/'checkpoint.json', dict(plan=certified, result=optimistic,
                wall_seconds=.2, cpu_seconds=.2))
            write(output/'progress.json', dict(cores=2, phase='test_interruption'))
            return True
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); write(root/'graph.json', raw)
            with patch('scheduling.api.run_bounded', side_effect=interrupted):
                result = solve_case(root/'graph.json', official.REFERENCE/'data/config.txt',
                    [2], root/'out', seconds=10.)
            saved = read(root/'out/N2/evaluation.json')
            self.assertEqual(saved['makespan'], exact['makespan'])
            self.assertEqual(result['rows'][0]['score_source'], 'official')
            self.assertTrue(result['rows'][0]['hard_timeout'])

    def test_artifact_switch_does_not_change_answer_or_leave_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); write(root/'graph.json', graph_of(8, shared=False))
            answers = []
            for enabled in (True, False):
                params = default_parameters()
                params.update(max_rounds=0, enable_initial_candidates=False)
                params['artifacts'] = {'intermediate': enabled}
                folder = root/str(enabled)
                result = solve_case(root/'graph.json', official.REFERENCE/'data/config.txt',
                    [2, 3], folder, seconds=20., parameters=params)
                self.assertEqual(result['status'], 'ok', result)
                answers.append([(read(folder/f'N{n}/plan.json'),
                    normalized(read(folder/f'N{n}/evaluation.json'))) for n in (2, 3)])
                files = {f.relative_to(folder).as_posix() for f in folder.rglob('*') if f.is_file()}
                if enabled:
                    self.assertIn('N2/search.json', files)
                    self.assertIn('N2/official_checkpoint.json', files)
                else:
                    self.assertEqual(files, {'summary.json'} | {
                        f'N{n}/{name}.json' for n in (2, 3) for name in ('plan', 'evaluation', 'summary')})
            self.assertEqual(answers[0], answers[1])

    def test_artifact_configuration_rejects_truthy_strings(self):
        for value in ('false', 0, None):
            with self.assertRaises(TypeError):
                intermediate_artifacts({'artifacts': {'intermediate': value}})


if __name__ == '__main__':
    unittest.main()
