"""Architectural boundaries with a complete independent backend and real spawn."""
from collections import OrderedDict
from dataclasses import FrozenInstanceError, replace
import multiprocessing
from pathlib import Path
import pickle
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from scheduling.api import solve_case, source_signature
from scheduling.budget import CaseSliceBudget, SearchBudget
from scheduling.contracts import PlanSemantics
from scheduling.engine import search
from scheduling.io import read, write
from scheduling.registry import resolve_problem
from scheduling.service import SearchServices
import problem_interface_fixture as toy


class ProblemInterfaceTests(unittest.TestCase):
    def setUp(self):
        toy.CALLS.clear()

    def test_complete_non_q1_backend_drives_search_and_custom_state_signature(self):
        problem = toy.definition()
        model, settings = toy.ToyModel(toy.raw_input()), dict(multiplier=1)
        plan, _ = toy.seed(model, 2, settings)
        original = toy.evaluate(model.raw, plan, settings)
        services = SearchServices(model, settings, problem=problem)
        # No Q1 shape/partition data are supplied, and custom validation returns
        # an integer instead of the iterable expected by Q1 state signatures.
        with patch('time.perf_counter', return_value=0.):
            budget = CaseSliceBudget(SearchBudget(30., started=0.), 1)
            answer, result, history = search(model, 2, settings, toy.default_parameters(),
                budget.target, services, problem=problem, seed=(plan, original), budget=budget)
        self.assertEqual(result['makespan'], 13)
        self.assertEqual(answer['label'], 'balanced')
        self.assertGreaterEqual(len(model.signature_calls), 2)
        self.assertGreater(toy.CALLS['retain'], 0)
        self.assertEqual(history['problem_id'], 'toy_lanes')
        self.assertEqual(history['backend_version'], 'test-1')
        self.assertEqual(history['requested_params']['max_initial_evaluations_per_core'], 2)
        self.assertEqual(history['effective_params']['max_initial_evaluations_per_core'], 1)
        self.assertEqual([r['action'] for r in history['records']], ['toy_balance'])

    def test_custom_preanalysis_key_and_problem_version_isolate_shared_caches(self):
        base = toy.definition()
        model = toy.ToyModel(toy.raw_input())
        plan, _ = toy.seed(model, 2, {})
        cache, profiles = OrderedDict(), OrderedDict()
        definitions = (base, replace(base, version='test-2'), replace(base, id='other_toy'))
        for multiplier, problem in enumerate(definitions, 1):
            service = SearchServices(model, dict(multiplier=multiplier), problem=problem)
            service.cache, service.profiles = cache, profiles
            value, record = service.evaluate(plan, 1.)
            self.assertFalse(record['cache_hit'])
            self.assertEqual(value['makespan'], 14 * multiplier)
            _, repeated = service.evaluate(plan, 1.)
            self.assertTrue(repeated['cache_hit'])
            first = service.preanalyze(plan)
            # A changed order/label has a different plan key but intentionally
            # shares this backend's own job-profile identity.
            reordered = dict(plan, order=list(reversed(plan['order'])), label='different')
            self.assertNotEqual(problem.semantics.key(plan), problem.semantics.key(reordered))
            self.assertIs(service.preanalyze(reordered), first)
            self.assertEqual(first[0], 8 * multiplier)
        self.assertEqual(toy.CALLS['evaluate'], 3)
        self.assertEqual(toy.CALLS['preanalyze'], 3)
        self.assertEqual(toy.CALLS['preanalysis_key'], 6)
        self.assertEqual({key[:2] for key in cache}, {(d.id, d.version) for d in definitions})
        self.assertEqual({key[:2] for key in profiles}, {(d.id, d.version) for d in definitions})

    def test_unknown_or_incomplete_interfaces_are_rejected(self):
        for value in ('q3', 'os.system', '', None, object()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_problem(value)
        with self.assertRaises(TypeError):
            PlanSemantics(None, toy.key, toy.objective, toy.state_signature)
        with self.assertRaises(TypeError):
            replace(toy.definition(), execution=object())
        with self.assertRaises(TypeError):
            replace(toy.definition().execution, preanalysis_key=None)
        with self.assertRaises(FrozenInstanceError):
            toy.definition().id = 'mutated'
        supplied = toy.definition()
        self.assertIs(resolve_problem(supplied), supplied)

    def test_independent_backend_runs_real_case_api_in_spawn_without_q1_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write(folder/'graph.json', toy.raw_input(strict=True))
            write(folder/'settings.json', dict(multiplier=1))
            result = solve_case(folder/'graph.json', folder/'settings.json', [2, 7],
                folder/'answer', problem=toy.definition(), seconds=15)
            self.assertEqual(result['status'], 'ok', result)
            self.assertTrue(result['coverage_complete'])
            self.assertEqual(result['problem_id'], 'toy_lanes')
            self.assertEqual(result['baseline_seconds'], 0)
            self.assertFalse(result['parallel_solver'])
            self.assertEqual([row['makespan'] for row in result['rows']], [13, 8])
            self.assertTrue(all(row['officially_evaluated_final'] for row in result['rows']))
            for cores in (2, 7):
                self.assertEqual(read(folder/f'answer/N{cores}/evaluation.json')['oracle'], 'independent_toy')
                self.assertNotIn('node_to_subgraph', read(folder/f'answer/N{cores}/plan.json'))

    def test_empty_graph_uses_backend_result_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write(folder/'graph.json', dict(jobs=[], require_no_q1_imports=True))
            write(folder/'settings.json', dict(multiplier=1))
            result = solve_case(folder/'graph.json', folder/'settings.json', [7],
                folder/'answer', problem=toy.definition(), seconds=15)
            self.assertEqual(result['status'], 'ok', result)
            self.assertEqual(result['rows'][0]['makespan'], 0)
            self.assertEqual(read(folder/'answer/N7/evaluation.json')['oracle'], 'independent_toy')

    def test_q1_definition_is_serializable_in_real_spawn(self):
        definition = pickle.loads(pickle.dumps(resolve_problem('q1')))
        context = multiprocessing.get_context('spawn')
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=toy.spawn_probe, args=(definition, sender))
        try:
            process.start()
            sender.close()
            self.assertTrue(receiver.poll(15), 'Q1 definition did not start in spawned worker')
            report = receiver.recv()
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(report['id'], 'q1')
            self.assertEqual(report['result']['scene'], 'A')
            self.assertTrue(report['has_defaults'])
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            process.close()
            receiver.close()

    def test_source_fingerprint_covers_common_engine_and_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ('src/scheduling/engine.py', 'src/scheduling/contracts.py',
                     'src/q1/unified/backend.py', 'configs/unified.json', 'official/code/eval.py')
            for name in paths:
                path = root/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('original', encoding='utf-8')
            with patch('scheduling.api.ROOT', root):
                initial = source_signature()
                for name in paths:
                    with self.subTest(path=name):
                        path = root/name
                        path.write_text('changed', encoding='utf-8')
                        self.assertNotEqual(source_signature(), initial)
                        path.write_text('original', encoding='utf-8')
                        self.assertEqual(source_signature(), initial)

    def test_legacy_q1_solve_file_uses_real_backend_and_official_result(self):
        # Q1-specific helpers are restricted to this compatibility check; none
        # participate in the independent toy backend above.
        from test_q1_initialization import graph_of, SETTINGS
        from q1 import official
        from q1.plan import normalized
        from q1.unified.api import solve_file
        parameters = resolve_problem('q1').default_parameters()
        parameters.update(max_rounds=0, enable_initial_candidates=False)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            raw = graph_of(6, [(0, 2), (1, 3), (2, 4), (3, 5)])
            write(folder/'graph.json', raw)
            result = solve_file(folder/'graph.json', official.REFERENCE/'data/config.txt',
                                3, folder/'answer', seconds=15, parameters=parameters)
            self.assertEqual(result['status'], 'ok', result)
            self.assertEqual(result['baseline']['mode'], 'not_computed')
            self.assertEqual(result['cached_baseline_charge_seconds'], 0)
            plan = read(folder/'answer/plan.json')
            saved = read(folder/'answer/evaluation.json')
            self.assertEqual(normalized(saved), normalized(official.evaluate(raw, plan, SETTINGS)))


if __name__ == '__main__':
    unittest.main()
