"""Graph-only bootstrap invariants and shared-constructor evaluation boundaries."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from test_q1_initialization import graph_of, SETTINGS
from q1 import official
from q1.graph import GraphIndex
from q1.model import Graph
from q1.plan import validate_plan
from q1.improvement.incremental import IncrementalEvaluator
from q1.unified.construction import Construction
from q1.unified.seed import structural_plan
from q1.unified.service import SearchServices


class StructuralSeedTests(unittest.TestCase):
    def assert_legal(self, graph, cores):
        with patch.object(official, 'singlecore', side_effect=AssertionError('singlecore called')), \
                patch.object(official, 'evaluate', side_effect=AssertionError('evaluate called')), \
                patch.object(official, 'preanalyze', side_effect=AssertionError('preanalyze called')):
            plan, meta = structural_plan(graph, cores, SETTINGS)
        blocks = validate_plan(graph, plan, cores)
        self.assertEqual(sum(map(len, blocks)), len(graph.ops))
        self.assertEqual(meta['score_source'], 'unscored')
        self.assertEqual(structural_plan(graph, cores, SETTINGS), (plan, meta))
        return plan, meta, blocks

    def test_arbitrary_dependencies_and_core_counts(self):
        graph = Graph(graph_of(17, [(0, 4), (1, 4), (4, 7), (2, 5), (5, 8),
                                  (7, 10), (8, 10), (10, 15), (3, 12), (12, 16)]))
        for cores in (2, 3, 4, 5):
            with self.subTest(cores=cores):
                self.assert_legal(graph, cores)

    def test_many_independent_components_are_coarsened_and_balanced(self):
        graph = Graph(graph_of(400, shared=False, cycles=[100] * 400, pipes=['PIPE_M'] * 400))
        plan, meta, blocks = self.assert_legal(graph, 5)
        self.assertLessEqual(len(blocks), 10)
        self.assertEqual(meta['packed_components'], 400)
        loads = [sum(graph.cost[int(v)] for v, t in plan['node_to_subgraph'].items() if t in q)
                 for q in plan['core_schedules']]
        self.assertEqual(len(set(loads)), 1)
        self.assertLessEqual(meta['largest_task'], meta['maximum_task_operations'])

    def test_long_dependency_chain_keeps_bounded_coarse_chunks(self):
        graph = Graph(graph_of(900, [(v, v + 1) for v in range(899)],
                               cycles=[100] * 900, shared=False))
        _, meta, blocks = self.assert_legal(graph, 5)
        self.assertGreater(len(blocks), 1)
        self.assertLess(len(blocks), 20)
        self.assertLessEqual(meta['largest_task'], meta['maximum_task_operations'])

    def test_empty_graph_has_only_empty_queues(self):
        graph = Graph(graph_of(0))
        for cores in (2, 3, 4, 5):
            plan, meta, blocks = self.assert_legal(graph, cores)
            self.assertEqual(plan, {'node_to_subgraph': {}, 'core_schedules': [[] for _ in range(cores)]})
            self.assertEqual((meta['tasks'], meta['proxy_cycles'], blocks), (0, 0, []))

    def test_shared_constructor_reuses_index_and_approximate_preanalysis(self):
        graph = Graph(graph_of(12, [(v, v + 1) for v in range(11)]))
        service = SearchServices(graph, SETTINGS)
        service.graph_index = GraphIndex(graph, SETTINGS)
        service.shared_case = True
        with patch('q1.unified.construction.GraphIndex', side_effect=AssertionError('rebuilt index')):
            constructor = Construction(graph, 3, SETTINGS, service)
        self.assertIs(constructor.index, service.graph_index)
        constructor.approximation = IncrementalEvaluator(graph, service.settings_dict, {})
        with patch.object(service, 'preanalyze', side_effect=AssertionError('official preanalysis')):
            candidate = constructor.next()
        self.assertIsNotNone(candidate)
        validate_plan(graph, candidate[1], 3)
        self.assertEqual(constructor.records[0]['timing_source'], 'approximate_local_pipe')

    def test_constructor_window_aborts_at_progress_without_official_work(self):
        graph = Graph(graph_of(12))
        service = SearchServices(graph, SETTINGS)
        constructor = Construction(graph, 3, SETTINGS, service)
        clock = [0.]
        def slow_partition(*args, progress=None):
            clock[0] = 11.
            progress(dict(event='partition_progress', processed_ops=1000))
            self.fail('expired construction continued')
        with patch('q1.unified.construction.perf_counter', side_effect=lambda: clock[0]), \
                patch('q1.unified.construction.partition', side_effect=slow_partition), \
                patch.object(service, 'preanalyze', side_effect=AssertionError('official preanalysis')):
            self.assertIsNone(constructor.next(seconds=10.))
        self.assertEqual(constructor.records[-1]['status'], 'window_expired')


if __name__ == '__main__':
    unittest.main()
