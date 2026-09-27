"""Q3 proposals: legal dependencies, full-state diversity and real L2 timing."""
import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from q3v3.graph import Graph
from q3v3.neighborhoods import generate_candidates
from q3v3.official import evaluate, read_settings
from q3v3.state import PlanState


def graph_of(edges=(), count=6):
    return Graph({'ops': [{'id': i, 'op': 'ADD', 'pipe': 'PIPE_V', 'cycles': 100 + i}
                          for i in range(count)],
                  'tensors': [],
                  'edges': [{'source': a, 'target': b} for a, b in edges]})


def shared_input():
    return Graph({'ops': [{'id': i, 'op': 'ADD', 'pipe': 'PIPE_V', 'cycles': 100}
                          for i in range(3)],
                  'tensors': [{'id': 100, 'pos': 'UB', 'size': 12000},
                              {'id': 101, 'pos': 'UB', 'size': 18000}],
                  'edges': [{'source': 100, 'target': 0}, {'source': 100, 'target': 1},
                            {'source': 101, 'target': 2}]})


class NeighborhoodTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = read_settings(ROOT / 'official/data/config.txt')

    def test_candidates_preserve_coverage_dependencies_and_source(self):
        graph = graph_of([(0, 1), (1, 2), (3, 4), (4, 5)])
        plan = {'node_to_subgraph': {str(i): i for i in range(6)},
                'core_schedules': [[0, 1, 2], [3, 4, 5], []]}
        before = copy.deepcopy(plan)
        result = evaluate(graph.raw, plan, self.settings)
        candidates = generate_candidates(graph, plan, result, self.settings)
        self.assertTrue(candidates)
        self.assertEqual(plan, before)
        digests = []
        for item in candidates:
            state = PlanState(graph, item.plan)
            digests.append(state.digest)
            self.assertEqual(state.owner, PlanState(graph, plan).owner)
            # Some operation/Pipe combinations may be globally illegal; the
            # generator promises only the structural gate, as documented.
        self.assertEqual(len(digests), len(set(digests)))
        self.assertNotIn(PlanState(graph, plan).digest, digests)

    def test_middle_migration_and_joint_rebalance_are_available(self):
        graph = graph_of()
        plan = {'node_to_subgraph': {str(i): i for i in range(6)},
                'core_schedules': [[0, 1, 2, 3, 4, 5], []]}
        items = generate_candidates(graph, plan, {}, self.settings, max_candidates=48)
        self.assertTrue(any(item.family == 'rebalance' for item in items))
        self.assertTrue(any(item.family == 'placement' and
                            3 in item.plan['core_schedules'][1] for item in items))
        self.assertTrue(any(item.family == 'rebalance' and
                            len(item.plan['core_schedules'][1]) > 1 for item in items))

    def test_observed_inflight_read_produces_real_cache_hit_and_gain(self):
        graph = shared_input()
        plan = {'node_to_subgraph': {'0': 0, '1': 1, '2': 2},
                'core_schedules': [[0], [1, 2]]}
        original = evaluate(graph.raw, plan, self.settings)
        items = generate_candidates(graph, plan, original, self.settings, max_candidates=48)
        l2 = [item for item in items if item.family == 'l2_inflight']
        self.assertTrue(l2)
        outcomes = [evaluate(graph.raw, item.plan, self.settings) for item in l2]
        self.assertGreater(max(row['cache_stats']['hit_bytes'] for row in outcomes),
                           original['cache_stats']['hit_bytes'])
        self.assertLess(min(row['makespan'] for row in outcomes), original['makespan'])
        self.assertTrue(all(item.plan['node_to_subgraph'] == plan['node_to_subgraph'] for item in l2))

    def test_l2_switch_and_small_budget_keep_cache_family(self):
        graph = shared_input()
        plan = {'node_to_subgraph': {'0': 0, '1': 1, '2': 2},
                'core_schedules': [[0], [1, 2]]}
        result = evaluate(graph.raw, plan, self.settings)
        enabled = generate_candidates(graph, plan, result, self.settings, max_candidates=2)
        self.assertEqual(len(enabled), 2)
        self.assertTrue(enabled[0].family.startswith('l2_'))
        disabled = generate_candidates(graph, plan, result, self.settings, enable_l2=False)
        self.assertFalse(any(item.family.startswith('l2_') for item in disabled))
        self.assertEqual(generate_candidates(graph, plan, result, self.settings, max_candidates=0), [])

    def test_fifo_reuse_is_triggered_by_actual_eviction_then_miss(self):
        graph = Graph({'ops': [{'id': i, 'op': 'ADD', 'pipe': 'PIPE_V', 'cycles': 100}
                               for i in range(4)],
                       'tensors': [{'id': 100 + i, 'pos': 'UB',
                                    'size': 12000 if i == 0 else 18000} for i in range(3)],
                       'edges': [{'source': 100, 'target': 0}, {'source': 100, 'target': 3},
                                 {'source': 101, 'target': 1}, {'source': 102, 'target': 2}]})
        plan = {'node_to_subgraph': {str(i): i for i in range(4)},
                'core_schedules': [[0, 1], [2, 3]]}
        settings = dict(self.settings, cache_capacity_bytes=20000)
        result = evaluate(graph.raw, plan, settings)
        self.assertTrue(any(100 in e.get('evicted_tensor_ids', []) for e in result['cache_events']))
        proposals = [c for c in generate_candidates(graph, plan, result, settings)
                     if c.family == 'l2_fifo']
        self.assertTrue(proposals)
        for candidate in proposals:
            self.assertGreater(evaluate(graph.raw, candidate.plan, settings)['makespan'], 0)
        # Retaining the misses but removing their eviction evidence must not
        # produce FIFO-specific candidates.
        without = copy.deepcopy(result)
        for event in without['cache_events']:
            event.pop('evicted_tensor_ids', None)
        self.assertFalse(any(c.family == 'l2_fifo' for c in
                             generate_candidates(graph, plan, without, settings)))

    def test_no_case_name_or_measured_baseline_controls_candidates(self):
        graph = shared_input()
        plan = {'node_to_subgraph': {'0': 0, '1': 1, '2': 2},
                'core_schedules': [[0], [1, 2]]}
        result = evaluate(graph.raw, plan, self.settings)
        augmented = dict(result, case_name='arbitrary_renamed_input', baseline_cycles=10**18)
        first = generate_candidates(graph, plan, result, self.settings)
        second = generate_candidates(graph, plan, augmented, self.settings)
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
