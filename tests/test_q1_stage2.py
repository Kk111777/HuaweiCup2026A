"""独立穷举、剪枝反例、官方完整结果对照及运行产物契约。"""
from copy import deepcopy
from itertools import combinations, islice, permutations, product
from pathlib import Path
import json
import random
import sys
import subprocess
import tempfile
from time import perf_counter
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
from q1 import official
from q1.stage2 import improve, SearchOptions
from q1.stage2.state import Context, PlanState, CycleError, objective, digest
from q1.stage2.evaluate import Evaluator
from q1.stage2.bounds import basic, expanded, ddr_violation, cannot_improve
from q1.stage2.diagnose import diagnose
from q1.stage2.neighborhoods import insertions, region_window, domain_record, domains, layer1, cuts_for_seed
from q1.stage2.search import Search, SearchStopped
from q1.stage2.run import read_initial, reusable, run_case
from q1.stage2 import run as stage2_run
from q1.run import write_json, run_case as initialize_case
from stub_multicore_cut_and_schedule import MulticoreCutError
from test_q1_initialization import graph_of, SETTINGS


def plan(groups, orders):
    return {"node_to_subgraph": {str(v): j for j, ns in groups.items() for v in ns}, "core_schedules": orders}


def partitions_by_assignments(n):
    """Independent exhaustive oracle: all n**n labels, then set-partition dedup."""
    seen = set()
    for labels in product(range(n), repeat=n):
        groups = tuple(sorted(tuple(i for i, l in enumerate(labels) if l == label) for label in set(labels)))
        if groups not in seen:
            seen.add(groups)
            yield groups


class OfficialAndBoundsTests(unittest.TestCase):
    def test_adapter_matches_official_and_does_not_mutate_templates(self):
        graph = graph_of(5, [(0, 2), (1, 3), (2, 4)], size=60)
        ctx = Context(graph, SETTINGS)
        evaluator = Evaluator(ctx)
        for orders in ([[7, 2], [11]], [[7], [11, 2]], [[11], [7, 2]]):
            p = plan({7: [0, 2], 11: [1, 3], 2: [4]}, orders)
            state = PlanState(ctx, p)
            prepared = evaluator.prepare(state)
            before = deepcopy(prepared)
            actual = evaluator.evaluate(state, prepared)
            self.assertEqual(actual, official.evaluate(graph, p, SETTINGS))
            self.assertEqual(prepared, before)
            self.assertEqual(evaluator.evaluate(state), actual)
        self.assertEqual(evaluator.statistics["preparations"], 1)

    def test_copy_contraction_is_not_an_internal_time_bound(self):
        graph = {"ops": [dict(id=0, op="M", pipe="PIPE_M", cycles=100),
                         dict(id=1, op="COPY_IN", pipe="PIPE_MTE2", cycles=1),
                         dict(id=2, op="V", pipe="PIPE_V", cycles=100)],
                 "tensors": [], "edges": [{"source": 0, "target": 1}, {"source": 1, "target": 2}]}
        ctx = Context(graph, SETTINGS)
        state = PlanState(ctx, plan({3: [0, 2]}, [[3], []]))
        self.assertEqual(ctx.index.preds[2], {0})
        self.assertEqual(basic(state)["time"], 100)
        ev = Evaluator(ctx)
        self.assertEqual(expanded(state, ev.prepare(state))["time"], ev.evaluate(state)["makespan"])

    def test_spill_capture_has_real_logical_tensor_mapping(self):
        graph = graph_of(6, [(0, 3), (1, 4), (2, 5)], shared=False, size=60)
        graph["edges"].extend({"source": i, "target": i+1} for i in range(5))
        settings = official.Settings({"L1": 1024, "UB": 150}, 60, 100, 1000)
        ctx = Context(graph, settings)
        state = PlanState(ctx, plan({9: list(range(6))}, [[9], []]))
        ev = Evaluator(ctx)
        prepared = ev.prepare(state)
        spills = prepared["diagnostics"][9]["step2"]["spill_records"]
        self.assertTrue(spills)
        self.assertEqual(prepared["traffic"]["spill_added_copy_bytes"],
                         sum(s["size"]*(1+int(s["spill_out_copies_data"])) for s in spills))
        self.assertEqual(ev.evaluate(state), official.evaluate(graph, state.plan, settings))
        bound = expanded(state, prepared)
        self.assertLessEqual(basic(state)["time"], bound["time"])
        self.assertLessEqual(bound["time"], ev.evaluate(state)["makespan"])
        self.assertIsNone(ddr_violation(bound, ev.evaluate(state)["makespan"]))
        diag = diagnose(state, ev.evaluate(state), prepared, SearchOptions())
        self.assertTrue(any(s["kind"] == "spill" for s in diag["seeds"]))

    def test_random_bounds_and_every_rejection_against_official(self):
        rng = random.Random(20260925)
        for iteration in range(30):
            n = rng.randrange(2, 8)
            links = [(a, b) for a in range(n) for b in range(a+1, n) if rng.random() < .22]
            graph = graph_of(n, links, cycles=[rng.randrange(0, 90) for _ in range(n)], size=rng.randrange(0, 130))
            groups = {}
            j = 0
            for v in range(n):
                if v and rng.random() < .5:
                    j += 1
                groups.setdefault(j*7+3, []).append(v)
            orders = [[], [], []]
            for j in groups:
                orders[rng.randrange(3)].append(j)
            state = PlanState(Context(graph, SETTINGS), plan(groups, orders))
            ev = Evaluator(state.context)
            result = official.evaluate(graph, state.plan, SETTINGS)
            pre = ev.prepare(state)
            self.assertEqual(ev.evaluate(state, pre), result)
            a, b = basic(state), expanded(state, pre)
            self.assertLessEqual(a["time"], b["time"])
            self.assertLessEqual(b["time"], result["makespan"])
            self.assertLessEqual(a["movement"], objective(result)[1])
            self.assertEqual(b["movement"], objective(result)[1])
            self.assertIsNone(ddr_violation(b, result["makespan"]))
            for upper in (result["makespan"]-1, result["makespan"], result["makespan"]+1):
                threshold = upper, objective(result)[1]
                for bound in (a, b):
                    if cannot_improve(bound["time"], bound["movement"], threshold):
                        self.assertGreaterEqual(objective(result), threshold)
                if ddr_violation(b, upper):
                    self.assertGreater(result["makespan"], upper)

    def test_ddr_interval_stronger_than_individual_windows(self):
        b = {"copies": {(1, 0, 0): 40, (1, 1, 0): 40},
             "release": {(1, 0, 0): 120, (1, 1, 0): 120},
             "tail": {(1, 0, 0): 20, (1, 1, 0): 20}}
        evidence = ddr_violation(b, 200)
        self.assertEqual(evidence["kind"], "ddr_interval")
        self.assertGreater(evidence["required_work"], evidence["capacity"])


class DomainTests(unittest.TestCase):
    def test_overlapping_closures_are_refined_without_node_duplication(self):
        ctx = Context(graph_of(4, [(0, 1), (0, 2)]), SETTINGS)
        state = PlanState(ctx, plan({7: [0, 1, 2, 3]}, [[7], []]))
        ev = Evaluator(ctx)
        result, prepared = ev.evaluate(state), ev.prepare(state)
        seed = dict(id="overlap", principle="boundary", kind="release", tasks=[7],
                    cuts={"7": [[0, 1], [0, 2]]})
        diag = dict(seeds=[seed], core_work=[(100, 100), (0, 0)], core_finish=[1000, 0])
        found = list(domains(state, diag, result, prepared, SearchOptions(), 3, []))
        self.assertEqual(found[0]["fragments"], [[0], [1], [2], [3]])

    def test_indirect_path_blocks_endpoint_merge_but_enters_third_layer_region(self):
        ctx = Context(graph_of(3, [(0, 1), (1, 2), (0, 2)]), SETTINGS)
        state = PlanState(ctx, plan({j: [j] for j in range(3)}, [[0, 1, 2], []]))
        with self.assertRaises(CycleError):
            PlanState(ctx, plan({0: [0, 2], 1: [1]}, [[0, 1], []]))
        ev = Evaluator(ctx)
        seed = dict(id="path", principle="locality", kind="boundary_reuse", tasks=[0, 2], cuts={})
        diag = dict(seeds=[seed], core_work=[(100, 100), (0, 0)], core_finish=[1000, 0])
        found = list(domains(state, diag, ev.evaluate(state), ev.prepare(state), SearchOptions(), 3, []))
        self.assertEqual(found[0]["old_task_ids"], [0, 1, 2])

    def test_joint_swap_and_half_work_true_prefix(self):
        ctx = Context(graph_of(3, cycles=[1, 1, 1000], pipes=["PIPE_M"]*3, shared=False), SETTINGS)
        state = PlanState(ctx, plan({4: [0], 9: [1, 2]}, [[4], [9]]))
        ev = Evaluator(ctx)
        result, prepared = ev.evaluate(state), ev.prepare(state)
        seed = dict(id="swap", tasks=[4, 9], kind="load", cuts={})
        choices = list(layer1(state, {"seeds": [seed]}, result))
        self.assertTrue(any(p["core_schedules"] == [[9], [4]] and m["action"] == "swap" for p, m in choices))
        self.assertEqual(cuts_for_seed(state, seed, prepared), {})

    def test_insertion_interval_equals_brute_force_cycles(self):
        graph = graph_of(5, [(0, 2), (1, 3), (2, 4)])
        ctx = Context(graph, SETTINGS)
        base = PlanState(ctx, plan({i: [i] for i in range(5)}, [[0, 2, 4], [1, 3]]))
        for task in base.nodes:
            expected = set()
            removed = [[j for j in order if j != task] for order in base.orders]
            for k, order in enumerate(removed):
                for position in range(len(order)+1):
                    orders = deepcopy(removed)
                    orders[k].insert(position, task)
                    if orders == base.orders:
                        continue
                    try:
                        PlanState(ctx, {**base.plan, "core_schedules": orders})
                    except CycleError:
                        continue
                    expected.add((k, position))
            self.assertEqual({(k, p) for k, p, _ in insertions(base, task)}, expected)

    def test_region_contiguity_and_size_limit(self):
        ctx = Context(graph_of(4), SETTINGS)
        state = PlanState(ctx, plan({j: [j] for j in range(4)}, [[0, 1, 2], [3]]))
        ev = Evaluator(ctx)
        result = ev.evaluate(state)
        diag = diagnose(state, result, ev.prepare(state), SearchOptions())
        region = region_window(state, [0, 2], diag, result, SearchOptions())
        self.assertEqual(region[0], {0, 1, 2})
        self.assertEqual(region[1][0], (0, 3))
        self.assertIsNone(region_window(state, [0, 2], diag, result, SearchOptions(max_region_tasks=2)))

    def test_exact_domain_matches_independent_complete_enumeration(self):
        graph = graph_of(4, [(0, 2)], cycles=[13, 21, 18, 9], size=12)
        ctx = Context(graph, SETTINGS)
        original = {7: {0, 1}, 19: {2, 3}}
        base = PlanState(ctx, plan(original, [[7, 19], []]))
        ev = Evaluator(ctx)
        initial = ev.evaluate(base)
        seed = dict(id="oracle", principle="pipeline")
        domain = domain_record(base, seed, ({7, 19}, {0: (0, 2), 1: (0, 0)}), [{v} for v in range(4)], "repartition")
        best = objective(initial)
        valid = 0
        for grouping in partitions_by_assignments(4):
            used, groups, waiting = set(), {}, []
            for group in grouping:
                old_id = next((j for j, ns in original.items() if ns == set(group)), None)
                if old_id is not None:
                    groups[old_id] = group
                    used.add(old_id)
                else:
                    waiting.append(group)
            for group in sorted(waiting, key=min):
                j = next(i for i in range(10) if i not in used)
                groups[j] = group
                used.add(j)
            ids = sorted(groups)
            for assignment in product(range(2), repeat=len(ids)):
                buckets = [[j for j, k in zip(ids, assignment) if k == core] for core in range(2)]
                for left, right in product(permutations(buckets[0]), permutations(buckets[1])):
                    p = plan(groups, [list(left), list(right)])
                    try:
                        official.validate_task_order(official.derive_multicore_plan(graph, p))
                    except (ValueError, MulticoreCutError):
                        continue
                    valid += 1
                    best = min(best, objective(official.evaluate(graph, p, SETTINGS)))
        self.assertGreater(valid, 100)
        for pruning in ((), ("partial", "b0", "b1", "ddr")):
            solver = Search(ctx, SearchOptions(region_seconds=60, region_evaluations=100000, pruning=pruning))
            answer, report = solver.domain(base, initial, domain, objective(initial), perf_counter()+60)
            self.assertEqual(report["status"], "OPTIMAL_IN_DOMAIN")
            self.assertEqual(objective(answer[1]), best)

    def test_incomplete_domain_never_claims_optimality(self):
        ctx = Context(graph_of(2), SETTINGS)
        base = PlanState(ctx, plan({0: [0], 1: [1]}, [[0], [1]]))
        solver = Search(ctx, SearchOptions(region_seconds=0))
        initial = solver.evaluator.evaluate(base)
        domain = domain_record(base, dict(id="x", principle="locality"), ({0, 1}, {0: (0, 1), 1: (0, 1)}), [{0}, {1}], "repartition")
        _, report = solver.domain(base, initial, domain, objective(initial), perf_counter()+10)
        self.assertEqual(report["status"], "INCOMPLETE")


class FeedbackTests(unittest.TestCase):
    def test_tied_latest_predecessors_form_a_joint_seed(self):
        graph = graph_of(5, [(0, 4), (2, 4)], cycles=[10, 1000, 10, 1000, 20],
                         pipes=["PIPE_V", "PIPE_M", "PIPE_V", "PIPE_M", "PIPE_V"], shared=False)
        graph["ops"] = [op for op in graph["ops"] if op["id"] < 5]
        graph["tensors"] = []
        graph["edges"] = [{"source": a, "target": b} for a, b in ((0, 4), (2, 4), (0, 1), (2, 3))]
        ctx = Context(graph, SETTINGS)
        state = PlanState(ctx, plan({0: [0, 1], 1: [2, 3], 2: [4]}, [[0], [1], [2]]))
        ev = Evaluator(ctx)
        pre = ev.prepare(state)
        diag = diagnose(state, ev.evaluate(state), pre, SearchOptions())
        self.assertTrue(any(s["kind"] == "tied_release" and s["tasks"] == [0, 1, 2] for s in diag["seeds"]))

    def test_incomplete_first_layer_still_visits_later_layers(self):
        graph = graph_of(2)
        initial = plan({0: [0], 1: [1]}, [[0, 1], []])
        events = []
        with patch.object(Search, "candidate", side_effect=SearchStopped("test_budget")):
            answer = improve(graph, initial, SETTINGS,
                             options=SearchOptions(search_seconds=1, region_seconds=0), progress=events.append)
        self.assertEqual([e["layer"] for e in events if e["event"] == "layer_start"], [1, 2, 3])
        self.assertEqual(answer.status, "INCOMPLETE")

    def test_initial_mismatch_stops_before_diagnosis(self):
        graph = graph_of(2)
        initial = plan({0: [0], 1: [1]}, [[0, 1], []])
        expected = official.evaluate(graph, initial, SETTINGS)
        expected["makespan"] += 1
        events = []
        with self.assertRaisesRegex(ValueError, "initial result differs"):
            improve(graph, initial, SETTINGS, expected_initial=expected, progress=events.append)
        self.assertFalse(any(e["event"] == "layer_start" for e in events))

    def test_unexpected_program_error_is_not_infeasibility(self):
        ctx = Context(graph_of(2), SETTINGS)
        initial = plan({0: [0], 1: [1]}, [[0], [1]])
        solver = Search(ctx, SearchOptions(pruning=()))
        with patch.object(solver.evaluator, "prepare", side_effect=RuntimeError("program bug")):
            with self.assertRaisesRegex(RuntimeError, "program bug"):
                solver.candidate(initial, (100000, 100000), perf_counter()+2, {})
        self.assertFalse(any(r.get("status") == "INVALID" for r in solver.records))

    def test_official_iteration_guard_is_incomplete_not_infeasible(self):
        from multicore_cut_evaluate_problem_1 import SceneAEvaluationError
        ctx = Context(graph_of(2), SETTINGS)
        solver = Search(ctx, SearchOptions(pruning=()))
        with patch.object(solver.evaluator, "prepare", side_effect=SceneAEvaluationError("max_iter exceeded")):
            with self.assertRaises(SearchStopped):
                solver.candidate(plan({0: [0], 1: [1]}, [[0], [1]]), (999999, 999999), perf_counter()+2, {})
        self.assertEqual(solver.records[-1]["status"], "INCOMPLETE")

    def test_acceptance_and_restart_after_equal_time_less_movement(self):
        graph = graph_of(3, cycles=[10, 10, 1000], pipes=["PIPE_M", "PIPE_V", "PIPE_M"])
        initial = plan({0: [0], 1: [1], 2: [2]}, [[0, 1], [2]])
        events = []
        result = improve(graph, initial, SETTINGS, options=SearchOptions(search_seconds=3, region_seconds=.3), progress=events.append)
        self.assertLessEqual(objective(result.evaluation), objective(result.initial_evaluation))
        self.assertTrue(any(h["delta_cycles"] == 0 and h["delta_added_bytes"] > 0 for h in result.history))
        for history in result.history:
            self.assertLess(tuple(history["after"]), tuple(history["before"]))
            next_layers = [e for e in events if e["event"] == "layer_start" and e["version"] == history["version"]]
            if next_layers:
                self.assertEqual(next_layers[0]["layer"], 1)
        self.assertFalse(cannot_improve(100, 9, (100, 10)))
        self.assertTrue(cannot_improve(100, 10, (100, 10)))

    def test_zero_budget_preserves_initial_exact_plan(self):
        graph = graph_of(2)
        initial = plan({5: [0], 8: [1]}, [[5, 8], []])
        result = improve(graph, initial, SETTINGS, options=SearchOptions(search_seconds=0))
        self.assertEqual(result.plan, initial)
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertEqual(result.evaluation, official.evaluate(graph, initial, SETTINGS))

    def test_invalid_options_fail_explicitly(self):
        for kwargs in ({"search_seconds": float("nan")}, {"layer_shares": [1, 0, 0]},
                       {"max_fragments": 0}, {"region_evaluations": True}, {"pruning": ["unknown"]}):
            with self.assertRaises(ValueError):
                SearchOptions(**kwargs)


class RunnerTests(unittest.TestCase):
    def test_stage1_directory_zip_and_parallel_cli_from_other_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root/"config.txt"
            config.write_bytes((official.REFERENCE/"data"/"config.txt").read_bytes())
            for case in ("toy_a", "toy_b"):
                path = root/(case+".json")
                write_json(path, graph_of(2))
                result = initialize_case(str(path), str(config), str(root/"initial"), [2], False, 1,
                                         {"makespan_cycles": 10000, "baseline_ref": "test"}, beta_search="fixed")
                self.assertEqual(result[-1]["status"], "success")
                direct = read_initial(root/"initial", case, 2, path, config)
                (root/"archive"/"cases").mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(root/"archive"/"cases"/(case+".zip"), "w") as z:
                    for member in (root/"initial"/case).rglob("*"):
                        if member.is_file():
                            z.write(member, member.relative_to(root/"initial").as_posix())
                zipped = read_initial(root/"archive", case, 2, path, config)
                self.assertEqual(direct[:2], zipped[:2])
                write_json(root/"initial"/case/"N2"/"plan.json", {"tampered": True})
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    read_initial(root/"initial", case, 2, path, config)
            command = [sys.executable, "-B", str(ROOT/"src"/"solve_problem1.py"), "improve-batch",
                       "--data-dir", str(root), "--initial-root", str(root/"archive"), "--cases", "toy_a", "toy_b",
                       "--cores", "2", "--jobs", "2", "--recompute-baseline", "--search-seconds", "0",
                       "--output-root", str(root/"output")]
            completed = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stdout+completed.stderr)
            report = json.loads((root/"output"/"summary.json").read_text())
            self.assertTrue(report["complete"])
            self.assertEqual(report["average_speedup"][-1]["expected_cases"], 2)

    def test_standalone_replay_resume_and_changed_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph_path, config_path, initial = root/"toy.json", root/"config.txt", root/"input.json"
            write_json(graph_path, graph_of(2))
            config_path.write_bytes((official.REFERENCE/"data"/"config.txt").read_bytes())
            write_json(initial, plan({0: [0], 1: [1]}, [[0, 1], []]))
            args = (str(graph_path), str(config_path), str(initial), str(root/"out"), [2], SearchOptions(search_seconds=0))
            baseline = dict(makespan_cycles=10000, baseline_ref="test")
            rows = run_case(*args, False, baseline, True)
            self.assertEqual(rows[-1]["status"], "success")
            directory = root/"out"/"toy"/"N2"
            marker = json.loads((directory/"complete.json").read_text())
            self.assertTrue(reusable(directory, marker["fingerprint"]))
            self.assertTrue(run_case(*args, True, baseline, True)[-1]["resumed"])
            changed = list(args)
            changed[-1] = SearchOptions(search_seconds=1)
            self.assertEqual(run_case(*changed, True, baseline, True)[-1]["status"], "failed")
            self.assertTrue(reusable(directory, marker["fingerprint"]))


def _performance_fixtures():
    graph = graph_of(4, [], cycles=[13, 21, 18, 9], shared=False, size=12)
    context = Context(graph, SETTINGS)
    groups = {7: [0], 11: [1], 19: [2], 31: [3]}
    states = [PlanState(context, plan(groups, [list(order), []]))
              for order in islice(permutations(groups), 8)]
    return graph, context, states


class ModelEntryTests(unittest.TestCase):
    def test_default_graph_entry_remains_v3_and_improve_selects_v2(self):
        from q1.__main__ import main
        for argument, target in (("new_graph.json", "q1.unified.__main__.main"),
                                 ("improve", "q1.stage2.run.main"),
                                 ("improve-batch", "q1.stage2.run.main")):
            with self.subTest(argument=argument), patch.object(sys, "argv", ["solve_problem1.py", argument]), \
                    patch(target, return_value=0) as selected:
                self.assertEqual(main(), 0)
                selected.assert_called_once_with()

    def test_explicit_v2_entry_and_shared_entry_have_same_arguments(self):
        # -m q1.stage2 requires src on sys.path; run it from src to avoid an env edit.
        command = [sys.executable, "-B", "-m", "q1.stage2", "improve-batch", "--help"]
        explicit = subprocess.run(command, cwd=ROOT/"src", capture_output=True, text=True, timeout=30)
        shared = subprocess.run([sys.executable, "-B", str(ROOT/"src/solve_problem1.py"),
                                 "improve-batch", "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(shared.returncode, 0, shared.stderr)
        for option in ("--initial-root", "--inherit-completed", "--region-evaluations"):
            self.assertIn(option, explicit.stdout)
            self.assertIn(option, shared.stdout)


class InheritanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.graph = self.root/"toy.json"
        self.config = self.root/"config.txt"
        write_json(self.graph, graph_of(2))
        self.config.write_bytes((official.REFERENCE/"data"/"config.txt").read_bytes())
        self.baseline = dict(makespan_cycles=10000, baseline_ref="test")
        self.catalog = {"toy": self.baseline}
        self.options = SearchOptions(search_seconds=0)
        self.initial, self.old, self.new = (self.root/n for n in ("initial", "old", "new"))
        initialize_case(str(self.graph), str(self.config), str(self.initial), [2, 3], False, 1,
                        self.baseline, beta_search="fixed")
        initial = read_initial(self.initial, "toy", 2, self.graph, self.config)[2]
        source = stage2_run.source_identity(self.graph, self.config, initial, self.options, self.baseline)
        current = source["solver_sha256"]
        previous = {**current, "src/q1/stage2/evaluate.py": "0"*64}
        with patch.object(stage2_run, "source_identity", return_value={**source, "solver_sha256": previous}):
            rows = run_case(str(self.graph), str(self.config), str(self.initial), str(self.old),
                            [2], self.options, False, self.baseline)
        self.assertEqual(rows[-1]["status"], "success")
        self.manifest = dict(previous_output=str(self.old), previous_solver_sha256=previous,
                             current_solver_sha256=current)
        # Partial work must remain in the original directory and must not be inherited.
        write_json(self.old/"toy/N3/checkpoint_plan.json", {"partial": True})

    def inherit(self, **changes):
        arguments = dict(graphs=[self.graph], config=self.config, initial_root=self.initial,
                         output=self.new, cores=[2, 3], options=self.options, catalog=self.catalog,
                         manifest=self.manifest)
        return stage2_run.inherit_completed(**{**arguments, **changes})

    def test_mixed_versions_finish_missing_work_and_resume_without_recomputation(self):
        old_bytes = {p.name: p.read_bytes() for p in (self.old/"toy/N2").iterdir()}
        with patch.object(stage2_run, "load_baseline_catalog", return_value=self.catalog), \
                patch.object(stage2_run, "improve", wraps=stage2_run.improve) as solve:
            args = ([self.graph], self.config, self.initial, self.new, [2, 3], self.options)
            self.assertEqual(stage2_run.run_cases(*args, resume=True, baseline_archive=self.root,
                                                 inheritance=self.manifest), 0)
            self.assertEqual(solve.call_count, 1)
            self.assertEqual(stage2_run.run_cases(*args, resume=True, baseline_archive=self.root,
                                                 inheritance=self.manifest), 0)
            self.assertEqual(solve.call_count, 1)
        for name, content in old_bytes.items():
            self.assertEqual((self.old/"toy/N2"/name).read_bytes(), content)
            self.assertEqual((self.new/"toy/N2"/name).read_bytes(), content)
        report = official.read_json(self.new/"summary.json")
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["rows"]), 3)
        self.assertTrue(report["rows"][1]["inherited"])
        self.assertNotIn("inherited", report["rows"][2])
        self.assertFalse((self.new/"toy/N3/checkpoint_plan.json").exists())
        self.assertTrue((self.old/"toy/N3/checkpoint_plan.json").exists())

    def test_unapproved_source_and_changed_options_are_rejected_before_copy(self):
        for key in ("previous_solver_sha256", "current_solver_sha256"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.inherit(manifest={**self.manifest, key: {}})
            self.assertFalse(self.new.exists())
        with self.assertRaises(ValueError):
            self.inherit(options=SearchOptions(search_seconds=1))
        self.assertFalse(self.new.exists())

    def test_damaged_artifact_and_false_official_marker_are_rejected(self):
        marker_path = self.old/"toy/N2/complete.json"
        done = official.read_json(marker_path)
        write_json(marker_path, {**done, "official_replay_verified": False})
        with self.assertRaises(ValueError):
            self.inherit()
        write_json(marker_path, done)
        (self.old/"toy/N2/result.log").write_text("damaged", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.inherit()
        self.assertFalse(self.new.exists())

    def test_original_output_and_conflicting_destination_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "separate"):
            self.inherit(output=self.old)
        write_json(self.new/"toy/N2/partial.json", {})
        with self.assertRaisesRegex(ValueError, "different results"):
            self.inherit()


class MemoizationTests(unittest.TestCase):
    def test_lru_miss_reuses_complete_result_but_charges_legacy_budget(self):
        graph, ctx, states = _performance_fixtures()
        ev = Evaluator(ctx)
        expected = official.evaluate(graph, states[0].plan, SETTINGS)
        for state in states[:6]:
            ev.evaluate(state)
        self.assertEqual(ev.statistics["evaluations"], 6)
        self.assertEqual(ev.statistics["budget_evaluations"], 6)
        self.assertEqual(len(ev.result_cache), 4)
        self.assertIsNone(ev.cached(states[0]))
        returned = ev.evaluate(states[0])
        self.assertEqual(returned, expected)
        self.assertEqual(ev.statistics["evaluations"], 6)
        self.assertEqual(ev.statistics["budget_evaluations"], 7)
        self.assertEqual(ev.statistics["secondary_hits"], 1)
        returned["makespan"] = -1
        returned["per_core_timeline"][0]["ops"][0]["start"] = -1
        self.assertEqual(ev.evaluate(states[0]), expected)
        self.assertEqual(ev.statistics["budget_evaluations"], 7)
        # Evict primary again and verify secondary bytes were not mutated either.
        for state in states[1:6]:
            ev.evaluate(state)
        self.assertEqual(ev.evaluate(states[0]), expected)

    def test_secondary_payload_cap_eviction_and_oversize(self):
        _, ctx, states = _performance_fixtures()
        ev = Evaluator(ctx)
        ev.evaluate(states[0])
        one = ev.statistics["secondary_bytes"]
        self.assertGreater(one, 0)
        ev.SECONDARY_CACHE_BYTES = one
        ev.evaluate(states[1])
        self.assertLessEqual(ev.statistics["secondary_bytes"], one)
        self.assertGreaterEqual(ev.statistics["secondary_evictions"], 1)
        self.assertEqual(ev.statistics["secondary_bytes"], sum(map(len, ev.secondary_cache.values())))
        tiny = Evaluator(ctx)
        tiny.SECONDARY_CACHE_BYTES = 1
        tiny.evaluate(states[0])
        self.assertFalse(tiny.secondary_cache)
        self.assertEqual(tiny.statistics["secondary_bytes"], 0)

    def test_input_identities_are_isolated(self):
        graph, ctx, states = _performance_fixtures()
        a = Evaluator(ctx)
        a.evaluate(states[0])
        changed = deepcopy(graph)
        next(op for op in changed["ops"] if op["id"] == 0)["cycles"] += 123
        other = Context(changed, SETTINGS)
        b = Evaluator(other)
        self.assertNotEqual(a.identity, b.identity)
        self.assertEqual(b.evaluate(PlanState(other, states[0].plan)),
                         official.evaluate(changed, states[0].plan, SETTINGS))
        self.assertEqual(b.statistics["secondary_hits"], 0)

    def test_failures_never_enter_cache_or_success_budget(self):
        _, ctx, states = _performance_fixtures()
        ev = Evaluator(ctx)
        prepared = ev.prepare(states[0])
        def fail(*args, **kwargs):
            raise official.SceneAEvaluationError("max_iter exceeded")
        with patch("q1.stage2.evaluate.isolated", return_value=fail):
            with self.assertRaises(official.SceneAEvaluationError):
                ev.evaluate(states[0], prepared)
        self.assertFalse(ev.result_cache)
        self.assertFalse(ev.secondary_cache)
        self.assertEqual(ev.statistics["budget_evaluations"], 0)
        self.assertEqual(ev.statistics["evaluations"], 0)

    def test_budget_stop_and_candidate_sequence_match_without_secondary(self):
        graph, ctx, states = _performance_fixtures()
        base = states[0]
        initial = official.evaluate(graph, base.plan, SETTINGS)
        domain = domain_record(base, dict(id="budget", principle="pipeline"),
                               (set(base.nodes), {0: (0, 4), 1: (0, 0)}),
                               list(base.nodes.values()), "fixed")
        traces = []
        for enabled in (False, True):
            solver = Search(ctx, SearchOptions(region_seconds=100, region_evaluations=6, pruning=()))
            if not enabled:
                solver.evaluator.SECONDARY_CACHE_BYTES = 0
            with patch("q1.stage2.search.perf_counter", return_value=0):
                solver.domain(base, initial, domain, objective(initial), 100)
                solver.evaluator.result_cache.clear()
                solver.records.clear()
                answer, report = solver.domain(base, initial, domain, objective(initial), 100)
            traces.append(([r["plan_hash"] for r in solver.records if r.get("scope") == "complete_plan"],
                           answer[0].plan, answer[1], report["stop_reason"], report["budget_evaluations"]))
            self.assertEqual(report["stop_reason"], "region_evaluation_budget")
            self.assertEqual(report["budget_evaluations"], 6)
            self.assertEqual(report["evaluations"], 0 if enabled else 6)
        self.assertEqual(*traces)


class TemplateTests(unittest.TestCase):
    def test_signature_and_state_containers_remain_independent(self):
        _, ctx, states = _performance_fixtures()
        a = states[0]
        self.assertEqual(a.signature, digest(a.plan))
        original = deepcopy(a.plan)
        a.nodes[7].clear()
        a.preds[7].add(999)
        a.plan["node_to_subgraph"]["0"] = 999
        b = PlanState(ctx, original)
        self.assertEqual(b.nodes[7], {0})
        self.assertNotIn(999, b.preds[7])
        self.assertEqual(b.signature, digest(original))
        reverse = dict(original, node_to_subgraph=dict(reversed(list(original["node_to_subgraph"].items()))))
        self.assertEqual(PlanState(ctx, reverse).signature, b.signature)

    def test_partition_cache_bounded_and_invalid_inputs_still_rejected(self):
        _, ctx, states = _performance_fixtures()
        for j in range(4):
            PlanState(ctx, plan({100+j: list(range(4))}, [[100+j], []]))
        self.assertEqual(len(ctx._partitions), 2)
        bad = deepcopy(states[0].plan)
        bad["core_schedules"][0].append(7)
        with self.assertRaises(ValueError):
            PlanState(ctx, bad)
        dag = Context(graph_of(2, [(0, 1)]), SETTINGS)
        with self.assertRaises(CycleError):
            PlanState(dag, plan({7: [0], 19: [1]}, [[19, 7], []]))

    def test_b1_and_simulation_cannot_pollute_templates(self):
        graph, ctx, states = _performance_fixtures()
        ev = Evaluator(ctx)
        prepared = ev.prepare(states[0])
        before = deepcopy(prepared)
        reference_bound = expanded(states[0], prepared)
        damaged = expanded(states[0], prepared)
        damaged["duration"].clear()
        damaged["copies"].clear()
        for state in states:
            self.assertEqual(ev.evaluate(state), official.evaluate(graph, state.plan, SETTINGS))
            expanded(state, ev.prepare(state))
        self.assertEqual(prepared, before)
        self.assertEqual(expanded(states[0], prepared), reference_bound)


if __name__ == "__main__":
    unittest.main()
