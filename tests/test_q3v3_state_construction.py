"""Q3 state identity, execution-boundary and proposal invariants."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from q3v3.graph import Graph
from q3v3.construction import construction_candidates, place_blocks, refine_blocks
from q3v3.official import evaluate, read_settings
from multicore_cut_evaluate_problem_1 import evaluate_scene_a
from q3v3.state import PlanState, compute_bounds, make_plan


SETTINGS = read_settings(ROOT / "official/data/config.txt")


def graph_of(operations, dependencies=()):
    """Small real tensor graph; every dependency has one 16-byte tensor."""
    return Graph({
        "ops": [dict(id=i, op="ADD", pipe=pipe, cycles=cycles)
                for i, (pipe, cycles) in enumerate(operations)],
        "tensors": [dict(id=1000 + i, pos="UB", size=16)
                    for i in range(len(dependencies))],
        "edges": [dict(source=a, target=b)
                  for i, (u, v) in enumerate(dependencies)
                  for a, b in ((u, 1000 + i), (1000 + i, v))],
    })


class CompleteStateTests(unittest.TestCase):
    def test_digest_preserves_full_submission_and_structural_signature_ignores_labels(self):
        graph = graph_of([("PIPE_V", 20)] * 3)
        original = make_plan([[0], [1], [2]], [[0, 1], [2]])
        renamed = {"node_to_subgraph": {"0": 17, "1": 9, "2": 65},
                   "core_schedules": [[17, 9], [65]]}
        reordered = make_plan([[0], [1], [2]], [[1, 0], [2]])
        moved = make_plan([[0], [1], [2]], [[0], [1, 2]])
        identity = PlanState(graph, original).digest
        self.assertNotEqual(identity, PlanState(graph, renamed).digest)
        self.assertEqual(PlanState(graph, original).structural_signature,
                         PlanState(graph, renamed).structural_signature)
        insertion = deepcopy(original)
        insertion['node_to_subgraph'] = dict(reversed(list(original['node_to_subgraph'].items())))
        self.assertNotEqual(identity, PlanState(graph, insertion).digest)
        self.assertNotEqual(identity, PlanState(graph, reordered).digest)
        self.assertNotEqual(identity, PlanState(graph, moved).digest)

    def test_q1_combined_task_cycle_is_not_a_q3_rejection(self):
        graph = graph_of([("PIPE_V" if i % 2 == 0 else "PIPE_M", 10)
                          for i in range(4)], [(0, 1), (2, 3)])
        plan = make_plan([[i] for i in range(4)], [[1, 2], [3, 0]])
        with self.assertRaises(ValueError):
            evaluate_scene_a(graph.raw, plan, bandwidth=SETTINGS['bandwidth'],
                             capacity=SETTINGS['capacity'], cross_core_wait=1000, same_core_wait=100)
        state = PlanState(graph, plan)
        result = evaluate(graph.raw, state.plan, SETTINGS)
        self.assertGreater(result["makespan"], 0)
        self.assertEqual(result["problem"], 3)

    def test_rejects_missing_nodes_duplicate_schedules_and_quotient_cycles(self):
        graph = graph_of([("PIPE_V", 10)] * 3, [(0, 1), (1, 2)])
        invalid = [
            make_plan([[0], [1]], [[0], [1]]),
            make_plan([[0], [1], [2]], [[0, 1], [1, 2]]),
            make_plan([[0, 2], [1]], [[0], [1]]),
            make_plan([[0], [1], [2]], [[1, 0, 2], []]),
        ]
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                PlanState(graph, plan)

    def test_rejects_input_coercion_and_extra_contract_fields(self):
        graph = graph_of([("PIPE_V", 10)])
        cases = [
            {"node_to_subgraph": {"0": 1.5}, "core_schedules": [[1], []]},
            {"node_to_subgraph": {"0": True}, "core_schedules": [[1], []]},
            {"node_to_subgraph": {"0": -1}, "core_schedules": [[-1], []]},
            {"node_to_subgraph": {"0": 0}, "core_schedules": [[False], []]},
            {"node_to_subgraph": {"0": 0, "00": 0}, "core_schedules": [[0], []]},
            {"node_to_subgraph": {"0": 0}, "core_schedules": [[0], []], "score": 1},
        ]
        for plan in cases:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                PlanState(graph, plan)

    def test_rebuild_preserves_members_and_repairs_projected_order(self):
        graph = graph_of([("PIPE_V", 10)] * 4, [(0, 2), (1, 3)])
        plan = make_plan([[i] for i in range(4)], [[0, 2], [1, 3]])
        state = PlanState(graph, plan)
        rebuilt = state.rebuild(assignments={0: 1, 1: 0, 2: 1, 3: 0},
                                priority={0: 9, 1: 8, 2: 0, 3: 1})
        self.assertEqual(rebuilt["node_to_subgraph"], plan["node_to_subgraph"])
        self.assertEqual(rebuilt["core_schedules"], [[1, 3], [0, 2]])
        PlanState(graph, rebuilt)


class ComputeBoundsTests(unittest.TestCase):
    def test_compute_path_resource_and_fixed_placement_bounds(self):
        graph = graph_of([("PIPE_M", 0), ("PIPE_M", 1000), ("PIPE_V", 300)], [(0, 2)])
        plan = make_plan([[0, 1, 2]], [[0], []])
        bounds = compute_bounds(graph, 2, plan)
        self.assertEqual(bounds["compute_critical_path"], 1000)
        self.assertEqual(bounds["resource_lower_bound"], 501)
        self.assertEqual(bounds["lower_bound"], 1000)
        self.assertEqual(bounds["placement_lower_bound"], 1001)
        result = evaluate(graph.raw, plan, SETTINGS)
        self.assertLessEqual(bounds["placement_lower_bound"], result["makespan"])

    def test_declared_copy_cycles_are_not_a_compute_lower_bound(self):
        raw = {
            "ops": [dict(id=0, op="COPY_IN", pipe="PIPE_MTE2", cycles=999999),
                    dict(id=1, op="ADD", pipe="PIPE_V", cycles=7),
                    dict(id=2, op="COPY_OUT", pipe="PIPE_MTE3", cycles=999999)],
            "tensors": [dict(id=i, pos=pos, size=60)
                        for i, pos in ((100, "DDR"), (101, "UB"), (102, "UB"), (103, "DDR"))],
            "edges": [dict(source=a, target=b) for a, b in
                      ((100, 0), (0, 101), (101, 1), (1, 102), (102, 2), (2, 103))],
        }
        graph = Graph(raw)
        plan = make_plan([[1]], [[0], []])
        bounds = compute_bounds(graph, 2, plan)
        self.assertEqual(bounds["lower_bound"], 7)
        self.assertLessEqual(bounds["lower_bound"], evaluate(raw, plan, SETTINGS)["makespan"])

    def test_rejects_invalid_core_count_or_mismatched_plan(self):
        graph = graph_of([("PIPE_V", 10)])
        for cores in (0, -1, True, 2.5):
            with self.subTest(cores=cores), self.assertRaises(ValueError):
                compute_bounds(graph, cores)
        with self.assertRaises(ValueError):
            compute_bounds(graph, 3, make_plan([[0]], [[0], []]))


class ConstructionTests(unittest.TestCase):
    def test_release_uses_producer_operation_not_unrelated_task_tail(self):
        # The producer finishes at 10 while later same-Pipe work occupies
        # core 0 until ~10010. The consumer can use core 1 before that tail ends.
        graph = graph_of([("PIPE_V", 10), ("PIPE_V", 10000), ("PIPE_V", 10000)],
                         [(0, 1), (0, 2)])
        inherited = make_plan([[0, 1], [2]], [[0, 1], []])
        original = deepcopy(inherited)
        placed = place_blocks(graph, inherited, SETTINGS)
        self.assertEqual(inherited, original)
        state = PlanState(graph, placed)
        self.assertNotEqual(state.core_by_block[state.owner[0]], state.core_by_block[state.owner[2]])
        self.assertEqual(set(placed), {"node_to_subgraph", "core_schedules"})
        result = evaluate(graph.raw, placed, SETTINGS)
        ops = {op["op_id"]: op for row in result["per_core_timeline"] for op in row["ops"]}
        # Original op IDs are preserved by official task construction.
        self.assertLess(ops[2]["start"], ops[1]["end"])

    def test_refinement_preserves_coverage_core_assignment_and_dependencies(self):
        graph = graph_of([("PIPE_V" if i % 2 else "PIPE_M", 20) for i in range(6)],
                         [(0, 1), (0, 2), (1, 3), (2, 3), (3, 4), (4, 5)])
        inherited = make_plan([[0, 1, 2, 3, 4, 5]], [[0], []])
        for mode in ("chains", "operations"):
            with self.subTest(mode=mode):
                refined = refine_blocks(graph, inherited, mode)
                state = PlanState(graph, refined)
                self.assertEqual(set(state.owner), set(graph.ops))
                self.assertTrue(all(state.core_by_block[state.owner[v]] == 0 for v in graph.ops))
                self.assertGreater(len(state.blocks), 1)
                self.assertGreater(evaluate(graph.raw, refined, SETTINGS)["makespan"], 0)

    def test_bounded_candidates_are_complete_unscored_and_deterministic(self):
        graph = graph_of([("PIPE_V", 10)] * 4, [(0, 1), (2, 3)])
        inherited = make_plan([[0, 1, 2, 3]], [[0], []])
        candidates = list(construction_candidates(graph, inherited, SETTINGS))
        self.assertEqual(candidates, list(construction_candidates(graph, inherited, SETTINGS)))
        self.assertLessEqual(len(candidates), 14)
        self.assertGreater(len(candidates), 0)
        for name, candidate in candidates:
            with self.subTest(name=name):
                self.assertEqual(set(candidate), {"node_to_subgraph", "core_schedules"})
                self.assertEqual(set(PlanState(graph, candidate).owner), set(graph.ops))


if __name__ == "__main__":
    unittest.main()
