"""Q1v3 structural reuse with Q3 semantics and real official micrographs."""
from copy import deepcopy
from itertools import permutations
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from q3v3 import search
from q3v3.graph import Graph
from q3v3.joint_search import refine_assignment
from q3v3.l2_structure import Feedback, rebuild_index, structural_candidates
from q3v3.neighborhoods import Candidate
from q3v3.official import evaluate, read_settings, write_json
from q3v3.q1_structure import StructureLibrary, bounded_units, discover_regions, region_cover
from q3v3.regions import RegionIndex, encode_order, operation_cores
from q3v3.state import PlanState, make_plan

SETTINGS = read_settings(ROOT/'official/data/config.txt')


def graph_of(count, edges=()):
    return Graph(dict(ops=[dict(id=i,op='ADD',pipe='PIPE_V',cycles=100) for i in range(count)],
        tensors=[dict(id=1000+i,pos='UB',size=60) for i in range(count)],
        edges=[dict(source=i,target=1000+i) for i in range(count)] +
              [dict(source=1000+a,target=b) for a,b in edges]))


class StructuralTests(unittest.TestCase):
    def test_postdominator_excludes_real_early_output_exit(self):
        graph = graph_of(4, [(0,1),(0,2),(1,3),(2,3)])
        self.assertIn(frozenset(range(4)), discover_regions(graph))
        raw = deepcopy(graph.raw)
        raw['ops'].append(dict(id=99,op='COPY_OUT',pipe='PIPE_MTE3',cycles=0))
        raw['edges'].append(dict(source=1000,target=99))
        graph = Graph(raw)
        self.assertNotIn(frozenset(range(4)), discover_regions(graph))

    def test_open_cover_preserves_exact_operations_and_exposes_branches(self):
        graph = graph_of(4, [(0,1),(0,2),(1,3),(2,3)])
        regions = discover_regions(graph)
        closed = region_cover(graph, regions)
        opened = region_cover(graph, regions, opened=True)
        self.assertGreater(len(opened),len(closed))
        self.assertEqual(sorted(v for b in opened for v in b),list(graph.order))

    def test_core_count_changes_chain_boundaries_before_assignment(self):
        graph = graph_of(24, [(i,i+1) for i in range(23)])
        a = next(StructureLibrary(graph,2).layouts(0))[1]
        b = next(StructureLibrary(graph,5).layouts(0))[1]
        self.assertGreater(len(b),len(a))
        coarse = bounded_units(graph, [frozenset(graph.ops)], 2, 0)
        fine = bounded_units(graph, [frozenset(graph.ops)], 2, 2)
        self.assertGreater(len(fine),len(coarse))

    def test_actual_duplicate_raw_input_misses_update_affinity_and_cuts(self):
        graph = graph_of(4, [(0,1),(2,3)])
        raw = deepcopy(graph.raw)
        raw['tensors'].append(dict(id=2000,pos='UB',size=12000))
        raw['edges'] += [dict(source=2000,target=1),dict(source=2000,target=3)]
        graph = Graph(raw)
        plan = make_plan([[v] for v in graph.order],[[0,1],[2,3]])
        state = PlanState(graph,plan)
        result = evaluate(raw,plan,SETTINGS)
        feedback = Feedback(graph,state,result,SETTINGS)
        self.assertGreater(feedback.reuse_bytes[2000],0)
        self.assertGreater(feedback.cache_pressure[1],0)
        groups = bounded_units(graph,[frozenset([0,1]),frozenset([2,3])],1,0)
        hot = bounded_units(graph,[frozenset([0,1]),frozenset([2,3])],1,0,feedback.cache_pressure)
        self.assertGreater(len(hot),len(groups))
        index = RegionIndex(graph,[[0,1],[2,3]],SETTINGS)
        self.assertFalse(index.links[0])  # Shared raw input has no producer edge.
        feedback.links(index)
        self.assertGreater(index.links[0][1],0)
        disabled = Feedback(graph,state,result,SETTINGS,enable_l2=False)
        disabled.links(index)
        self.assertFalse(index.links[0])
        self.assertEqual(disabled.summary()['hot_consumers'],[])

    def test_local_rebuild_and_fm_preserve_all_outside_operation_cores(self):
        graph = graph_of(8)
        plan = encode_order(graph,{v:v%2 for v in graph.ops},graph.order,2)
        state = PlanState(graph,plan)
        feedback = Feedback(graph,state,evaluate(graph.raw,plan,SETTINGS),SETTINGS)
        index,movable = rebuild_index(graph,[frozenset([0,1]),frozenset(range(2,8))],{0},feedback,SETTINGS)
        assignment = refine_assignment(index,index.majority(feedback.core),2,movable=movable)
        after = index.cores(assignment)
        self.assertEqual({v:after[v] for v in range(2,8)}, {v:feedback.core[v] for v in range(2,8)})

    def test_joint_reconstruction_crosses_old_boundaries_and_officially_improves(self):
        graph = graph_of(8, [(i,i+1) for i in range(7)])
        plan = encode_order(graph,{v:v%2 for v in graph.ops},graph.order,2)
        source = deepcopy(plan)
        baseline = evaluate(graph.raw,plan,SETTINGS)
        candidates = list(structural_candidates(graph,plan,baseline,SETTINGS,scale=2))
        self.assertTrue(candidates)
        results = [evaluate(graph.raw,c.plan,SETTINGS) for c in candidates]
        self.assertLess(min(r['makespan'] for r in results),baseline['makespan'])
        inherited = operation_cores(PlanState(graph,plan))
        self.assertTrue(any(sum(operation_cores(PlanState(graph,c.plan))[v]!=inherited[v] for v in graph.ops)>1
                            for c in candidates))
        self.assertEqual(plan,source)
        for c in candidates:
            self.assertEqual(set(PlanState(graph,c.plan).owner),set(graph.ops))

    def test_single_core_and_cooperative_deadline(self):
        graph = graph_of(4, [(0,1),(0,2),(1,3),(2,3)])
        plan = encode_order(graph,{v:0 for v in graph.ops},graph.order,1)
        result = evaluate(graph.raw,plan,SETTINGS)
        for candidate in structural_candidates(graph,plan,result,SETTINGS):
            self.assertGreater(evaluate(graph.raw,candidate.plan,SETTINGS)['makespan'],0)
        def expired():
            raise TimeoutError('deadline')
        with self.assertRaises(TimeoutError):
            list(structural_candidates(graph,plan,result,SETTINGS,check=expired))

    def test_stagnation_expands_scope_without_discarding_official_incumbent(self):
        graph = graph_of(7)
        plan = make_plan([[v] for v in graph.order],[list(graph.order),[]])
        baseline = evaluate(graph.raw,plan,SETTINGS)
        proposals, radii = iter(permutations(graph.order)), []
        def structural(g,p,r,s,library,scale,*args,**kwargs):
            radii.append(scale)
            for queue in proposals:
                yield Candidate(make_plan([[v] for v in g.order],[list(queue),[]]),'structural_control','test plateau')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            write_json(root/'graph.json',graph.raw)
            write_json(root/'warm.json',plan)
            with patch('q3v3.search.structural_candidates',side_effect=structural), \
                 patch('q3v3.search.construction_candidates',return_value=iter(())), \
                 patch('q3v3.neighborhoods.generate_candidates',return_value=[]), \
                 patch('q3v3.search.evaluate_file',return_value=baseline), \
                 patch('q3v3.search.replay',return_value=baseline):
                row,final = search.solve_one(root/'graph.json',2,ROOT/'official/data/config.txt',root/'run',
                    warm_plan=root/'warm.json',seconds=5,max_evaluations=22,near_bound_fraction=0,
                    enable_global=False,enable_joint=False,enable_l2=False)
            self.assertEqual(set(radii),{0,1,2})
            self.assertEqual(final,plan)
            self.assertEqual(row['makespan_cycles'],baseline['makespan'])
            self.assertEqual(row['official_calls'],22)


if __name__ == '__main__':
    unittest.main()
