"""Transaction, COPY reuse, non-convex region and coordinated-move checks."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from q3v3.global_construction import global_candidates, RegionLibrary
from q3v3.growth import Model, GrowthState, try_place, commit, grow
from q3v3.joint_search import joint_candidates, refine_assignment
from q3v3.neighborhoods import Candidate
from q3v3.regions import RegionIndex, coarsen_regions, encode_order, operation_cores, topological_order
from q3v3.official import evaluate, read_settings, write_json
from q3v3.state import Graph, PlanState, make_plan
from q3v3 import search
from q3v3.q1_compat import DEFAULT_Q1_SOURCE

SETTINGS = read_settings(ROOT / 'official/data/config.txt')


def graph_of(costs, edges=()):
    return Graph(dict(ops=[dict(id=i, op='ADD', pipe='PIPE_V', cycles=t) for i,t in enumerate(costs)],
        tensors=[dict(id=1000+i, pos='UB', size=60) for i in range(len(costs))],
        edges=[dict(source=i, target=1000+i) for i in range(len(costs))] +
              [dict(source=1000+a, target=b) for a,b in edges]))


class GrowthTests(unittest.TestCase):
    def test_trial_is_pure_and_rejects_stale_commit(self):
        graph = graph_of([10, 20, 30], [(0,1), (0,2)])
        model, state = Model(graph, SETTINGS), None
        state = GrowthState(model, 2)
        first = try_place(model, state, 0, 0)
        commit(model, state, first)
        original = deepcopy(state.__dict__)
        a, b = try_place(model, state, 1, 0), try_place(model, state, 1, 1)
        self.assertEqual(state.__dict__, original)
        self.assertGreater(dict(b.clocks[0])['PIPE_MTE3'], state.clocks[0]['PIPE_MTE3'])
        self.assertGreaterEqual(b.finish, first.finish + 1 + SETTINGS['cross_core_copy_delay_cycles'] + 1 + 20)
        commit(model, state, a)
        with self.assertRaisesRegex(ValueError, 'stale'):
            commit(model, state, b)

    def test_two_consumers_on_one_remote_core_reuse_one_copy_pair(self):
        graph = graph_of([10,20,30], [(0,1), (0,2)])
        model = Model(graph, SETTINGS)
        state = GrowthState(model, 2)
        commit(model, state, try_place(model, state, 0, 0))
        first = try_place(model, state, 1, 1)
        commit(model, state, first)
        second = try_place(model, state, 2, 1)
        self.assertEqual(first.added_bytes - second.added_bytes, 120)
        commit(model, state, second)
        self.assertEqual(state.copy_bytes, model.base_traffic(state.core))
        plan = encode_order(graph, state.core, [0,1,2], 2)
        result = evaluate(graph.raw, plan, SETTINGS)
        self.assertEqual(result['data_movement_bytes']['scheduled_copy_bytes'], state.copy_bytes)
        self.assertEqual(len(result['cross_core_transfers']), 1)

    def test_growth_and_all_global_candidates_pass_official_evaluation(self):
        graph = graph_of([100,100,500,100,100,500], [(0,1),(1,2),(3,4),(4,5)])
        core, order, regions, info = grow(graph, 3, SETTINGS, beta=2)
        self.assertEqual(set(order), set(graph.ops))
        self.assertEqual(set(regions), set(graph.ops))
        self.assertGreater(info['predicted_base_copy_bytes'], 0)
        for candidate in global_candidates(graph, 3, SETTINGS):
            self.assertGreater(evaluate(graph.raw, candidate.plan, SETTINGS)['makespan'], 0)

    def test_growth_checks_cooperative_deadline(self):
        graph = graph_of([10] * 40)
        def expired():
            raise TimeoutError('deadline')
        with self.assertRaises(TimeoutError):
            grow(graph, 2, SETTINGS, check=expired)

    def test_single_component_exposes_parallel_chains_before_coarse_variants(self):
        edges = []
        for start in (1,4,7,10):
            edges.extend([(0,start),(start,start+1),(start+1,start+2),(start+2,13)])
        graph = graph_of([10]+[1000]*12+[10],edges)
        self.assertEqual(len(graph.components),1)
        first = next(global_candidates(graph,2,SETTINGS))
        self.assertEqual(first.family,'global_chain')
        serial = make_plan([list(graph.ops)],[[0],[]])
        self.assertLess(evaluate(graph.raw,first.plan,SETTINGS)['makespan'],
                        evaluate(graph.raw,serial,SETTINGS)['makespan'])


class JointTests(unittest.TestCase):
    def test_atomic_encoding_preserves_actual_official_step1_compute_projection(self):
        import multicore_cut_evaluate_problem_3 as official
        graph = graph_of([100]*6, [(0,2),(1,3),(2,4),(3,5)])
        core = {0:0,1:1,2:0,3:1,4:1,5:0}
        order = [1,3,0,2,5,4]
        plan = encode_order(graph,core,order,2)
        original, captured = official._prioritize_task_seq, []
        def observe(*args,**kwargs):
            seq = original(*args,**kwargs)
            captured.append([v for v in seq if v in graph.ops])
            return seq
        with patch.object(official,'_prioritize_task_seq',side_effect=observe):
            evaluate(graph.raw,plan,SETTINGS)
        self.assertEqual(captured, [[v for v in order if core[v]==k] for k in range(2)])

    def test_nonconvex_regions_use_safe_operation_encoding(self):
        graph = graph_of([10]*4, [(0,1),(1,2),(2,3)])
        index = RegionIndex(graph, [[0,2],[1,3]], SETTINGS)
        core = index.cores({0:0, 1:1})
        for encoding in ('atomic','runs'):
            plan = encode_order(graph, core, topological_order(graph), 2,
                                encoding=encoding, region_of=index.owner)
            self.assertEqual(operation_cores(PlanState(graph,plan)), core)
            self.assertGreater(evaluate(graph.raw,plan,SETTINGS)['makespan'],0)
        with self.assertRaises(ValueError):
            PlanState(graph, make_plan([[0,2],[1,3]], [[0],[1]]))

    def test_best_prefix_can_cross_flat_load_plateau(self):
        graph = graph_of([100]*12)
        index = RegionIndex(graph, [[v] for v in graph.ops], SETTINGS)
        before = {v:v//4 for v in graph.ops}
        after = refine_assignment(index, before, 4, affinity=0)
        def peak(mapping):
            return max(sum(graph.cost[v] for v in graph.ops if mapping[v]==k) for k in range(4))
        self.assertEqual(peak(before),400)
        self.assertEqual(peak(after),300)
        self.assertGreaterEqual(sum(before[v]!=after[v] for v in graph.ops),3)
        self.assertEqual(before,{v:v//4 for v in graph.ops})

    def test_regions_change_with_core_count_and_ignore_inherited_boundaries(self):
        graph = graph_of([100]*12, [(v,v+1) for v in range(11)])
        a,b = coarsen_regions(graph,2,SETTINGS,beta=1), coarsen_regions(graph,4,SETTINGS,beta=1)
        self.assertGreater(len(b.groups),len(a.groups))
        # Initially every operation is its own bucket on alternating cores.
        plan = encode_order(graph,{v:v%2 for v in graph.ops},graph.order,2)
        result = evaluate(graph.raw,plan,SETTINGS)
        candidates = list(joint_candidates(graph,plan,result,SETTINGS))
        self.assertTrue(any(operation_cores(PlanState(graph,c.plan)) != operation_cores(PlanState(graph,plan))
                            for c in candidates))
        self.assertTrue(any(len(PlanState(graph,c.plan).blocks) < len(graph.ops) for c in candidates))
        self.assertTrue(all(evaluate(graph.raw,c.plan,SETTINGS)['makespan']>0 for c in candidates))

    def test_new_incumbent_expands_before_stale_queue_drains(self):
        graph = graph_of([100]*6)
        seed = make_plan([[v] for v in graph.ops], [list(graph.ops),[]])
        better = make_plan([[v] for v in graph.ops], [[0,1,2],[3,4,5]])
        seen = []
        original_bridge = search.export_seeds
        def bridge(*args,**kwargs):
            data = original_bridge(*args,**kwargs)
            data['plans'] = [dict(name='controlled_seed', plan=seed)]
            return data
        def neighbors(g,plan,result,settings,**kwargs):
            seen.append(PlanState(g,plan).digest)
            return [Candidate(better,'order','controlled improvement')] + [Candidate(seed,'order','stale')]*20
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            write_json(folder/'graph.json',graph.raw)
            with patch('q3v3.search.export_seeds',side_effect=bridge), \
                 patch('q3v3.search.construction_candidates',return_value=iter(())), \
                 patch('q3v3.neighborhoods.generate_candidates',side_effect=neighbors):
                summary,_ = search.solve_one(folder/'graph.json',2,ROOT/'official/data/config.txt',
                    folder/'run',DEFAULT_Q1_SOURCE,seconds=5,max_evaluations=4,
                    evaluation_timeout=20,near_bound_fraction=0,enable_global=False,enable_joint=False)
            self.assertEqual(seen[:2],[PlanState(graph,seed).digest,PlanState(graph,better).digest])
            self.assertLess(summary['makespan_cycles'],summary['seed_makespan'])


if __name__ == '__main__':
    unittest.main()
