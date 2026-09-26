"""Operation-level differential tests against the unmodified official engine."""
from dataclasses import replace
import json
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from q2.official import Settings
SETTINGS = Settings.read()
from q2.graph import Graph
from q2.encoding import encode_atomic
from q2.state import build_state
from q2.optimized.preparation import PreparationService, PreparationCache
from q2.evaluation import exact_score
from q2.optimized.approximation import ModelEvaluator, Calibration
from q2.optimized.events import network, simulate, lower_bound
from q2_optimized_fixture import make_graph


class ModelDifferentialTests(unittest.TestCase):
    def compare(self, g, plan, evaluator=None):
        evaluator = evaluator or ModelEvaluator(g)
        state = build_state(g, plan, preparation=evaluator.preparation)
        result = g.settings.evaluate(g.raw, plan)
        trace = evaluator.evaluate(state)
        expected = {(row['core_id'], op['op_id']): (op['start'], op['end'])
                    for row in result['per_core_timeline'] for op in row['ops']}
        actual = {key: (start, end) for key, start, end in zip(trace.op_keys, trace.starts, trace.ends)}
        self.assertEqual(actual, expected)
        self.assertEqual(trace.raw_cycles, result['makespan'])
        self.assertEqual(trace.score.added_copy_bytes, result['data_movement_bytes']['added_copy_bytes'])
        self.assertEqual(trace.spill_bytes, result['data_movement_bytes']['spill_added_copy_bytes'])
        self.assertEqual(trace.score.source, 'approximate')
        self.assertLessEqual(trace.lower_bound, result['makespan'])
        return state, trace, result

    def test_40_random_graphs_120_plans_all_operation_times(self):
        operations = 0
        for seed in range(40):
            raw, order = make_graph(seed, n=5+seed%10)
            g = Graph(raw, SETTINGS); rng = random.Random(1000+seed)
            evaluator = ModelEvaluator(g)
            for cores in (2, 3, 5):
                with self.subTest(seed=seed, cores=cores):
                    mapping = tuple(rng.randrange(cores) for _ in g.ids)
                    plan = encode_atomic(g, cores, mapping, tuple(g.dense[o] for o in order))
                    _, trace, _ = self.compare(g, plan, evaluator)
                    operations += len(trace.op_keys)
        self.assertEqual(operations, 4478)

    def test_fixed_minimal_and_both_memory_fixtures(self):
        for name, cycles, spill in [('minimal', 6, 0), ('memory_interleaved', 3736, 80000),
                                    ('memory_grouped', 2302, 0)]:
            with self.subTest(fixture=name):
                folder = ROOT/'tests/fixtures/q2_repair'
                raw = json.loads((folder/f'{name}.json').read_text())
                plan = json.loads((folder/f'{name}_multicore_res.json').read_text())
                _, trace, _ = self.compare(Graph(raw, SETTINGS), plan)
                self.assertEqual((trace.raw_cycles, trace.spill_bytes), (cycles, spill))

    def test_preparation_and_model_reuse_and_no_mutable_references(self):
        raw, order = make_graph(9)
        g = Graph(raw, SETTINGS)
        plan = encode_atomic(g, 3, tuple(v%3 for v in range(len(g.ids))), tuple(g.dense[o] for o in order))
        evaluator = ModelEvaluator(g)
        state, trace, _ = self.compare(g, plan, evaluator)
        before = evaluator.preparation.cache.stats.copy()
        prepared = evaluator.preparation.prepare_execution(state)
        copy = prepared.tasks
        copy[0]['seq'].clear()
        self.assertTrue(prepared.tasks[0]['seq'])
        again = evaluator.evaluate(state)
        self.assertEqual((trace.raw_cycles, trace.ends), (again.raw_cycles, again.ends))
        self.assertEqual(before['core_prepares'], evaluator.preparation.cache.stats['core_prepares'])
        self.assertEqual(before['prefix_builds'], evaluator.preparation.cache.stats['prefix_builds'])
        self.assertGreater(evaluator.stats['refined_hits'], 0)
        renamed = dict(node_to_subgraph={o: s+100 for o, s in plan['node_to_subgraph'].items()},
                       core_schedules=[[s+100 for s in row] for row in plan['core_schedules']])
        other = build_state(g, renamed, preparation=evaluator.preparation)
        self.assertEqual(other.execution_digest, state.execution_digest)
        other_trace = evaluator.evaluate(other)
        self.assertEqual(other_trace.score.plan_digest, other.plan_digest)
        self.assertEqual(other_trace.raw_cycles, trace.raw_cycles)
        for task in evaluator.preparation.prepare_execution(other).tasks.values():
            self.assertNotIn('op_subgraph', task)
        with self.assertRaises(ValueError):
            evaluator.evaluate(replace(state, core_of_op=tuple(0 for _ in g.ids)))

    def test_byte_eviction_and_environment_invalidation(self):
        raw, order = make_graph(1)
        g = Graph(raw, SETTINGS)
        plan = encode_atomic(g, 2, tuple(0 for _ in g.ids), tuple(g.dense[o] for o in order))
        cache = PreparationCache(max_bytes=8000)
        service = PreparationService(g, cache)
        state = build_state(g, plan, preparation=service)
        service.prepare_execution(state)
        self.assertLessEqual(cache.bytes, 8000)
        g.settings = replace(g.settings, bandwidth=g.settings.bandwidth+1)
        with self.assertRaises(ValueError): service.prepare_execution(state)
        self.assertGreater(cache.stats['namespace_invalidations'], 0)

    def test_calibration_is_exact_only_bucketed_and_rebinds_scores(self):
        folder = ROOT/'tests/fixtures/q2_repair'
        g = Graph(json.loads((folder/'minimal.json').read_text()), SETTINGS)
        plan = json.loads((folder/'minimal_multicore_res.json').read_text())
        evaluator = ModelEvaluator(g)
        state, trace, _ = self.compare(g, plan, evaluator)
        with self.assertRaises(ValueError): evaluator.calibration.observe(trace, trace.score)
        measured = exact_score(state, (12, 0))
        self.assertTrue(evaluator.calibration.observe(trace, measured))
        self.assertFalse(evaluator.calibration.observe(trace, measured))
        self.assertGreater(evaluator.rescore(state, trace).score.makespan, trace.score.makespan)
        self.assertEqual(evaluator.calibration.correct(6, 'coarse', 0, None, 0), 6)
        self.assertEqual(evaluator.calibration.correct(6, 'refined', 2, 1, 0), 6)


if __name__ == '__main__':
    unittest.main()
