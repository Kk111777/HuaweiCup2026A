"""Q3 mechanism, exact-event equivalence, import/bootstrap and publication gates."""
from fractions import Fraction
import contextlib
import copy
import io
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from common.artifacts import file_hash, write_json
from q2.official import read_json, normalized
from q2.state import plan_digest
from q3.baseline import find_input, fixed_singlecore
from q3.bounds import bandwidth_lower_bound, cannot_improve
from q3.config import parameters
from q3.official import Settings, Evaluator, evaluate_fresh, objective, replay
from q3.pipeline import solve_case, verified_directory
from q3.proposals import candidates
from q3.report import summarize
from q3.runtime import run_command
from q3.validation import audit_result
from q3.worker import Worker

FIX = ROOT / 'tests/fixtures/q3'
SETTINGS = Settings.read()


def fixture(name):
    folder = FIX / name
    return read_json(folder / 'graph.json'), read_json(folder / 'initial_plan.json')


class ExactEventTests(unittest.TestCase):
    def test_all_136_fixture_plans_match_unchanged_official_and_physical_audit(self):
        count = 0
        for folder in sorted(FIX.iterdir()):
            if not folder.is_dir():
                continue
            evaluator = Evaluator(read_json(folder / 'graph.json'), SETTINGS)
            for label in ('initial', 'final'):
                with self.subTest(case=folder.name, plan=label):
                    plan = read_json(folder / f'{label}_plan.json')
                    result, prepared, status = evaluator.evaluate(plan)
                    fresh = evaluate_fresh(evaluator.raw, plan, SETTINGS)
                    self.assertEqual(normalized(result), normalized(fresh))
                    if label == 'final':
                        self.assertEqual(normalized(result), normalized(read_json(folder / 'final_result.json')))
                    self.assertLessEqual(prepared.lower_bound, result['makespan'])
                    audit_result(prepared.bundle, result, SETTINGS)
                    self.assertNotEqual(prepared.scene_state.environment_digest, prepared.score_state.environment_digest)
                    count += 1
        self.assertEqual(count, 136)

    def test_cached_result_is_independent_and_plan_order_is_not_canonicalized(self):
        raw, plan = fixture('shared_0')
        evaluator = Evaluator(raw, SETTINGS)
        result, _, _ = evaluator.evaluate(plan)
        expected = copy.deepcopy(result)
        result['per_core_timeline'].clear()
        repeated, _, status = evaluator.evaluate(plan)
        self.assertEqual(repeated, expected)
        self.assertEqual(status, 'CACHED_OFFICIAL')
        reordered = copy.deepcopy(plan)
        reordered['node_to_subgraph'] = dict(reversed(list(plan['node_to_subgraph'].items())))
        self.assertNotEqual(plan_digest(plan), plan_digest(reordered))
        new, _, _ = evaluator.evaluate(reordered)
        self.assertEqual(normalized(new), normalized(evaluate_fresh(raw, reordered, SETTINGS)))

    def test_q2_q3_report_the_same_official_copy_count(self):
        for name in ('memory', 'shared_3', 'fifo_pressure'):
            raw, plan = fixture(name)
            a = evaluate_fresh(raw, plan, SETTINGS, problem=2)
            b = evaluate_fresh(raw, plan, SETTINGS)
            self.assertEqual(a['data_movement_bytes'], b['data_movement_bytes'])

    def test_corrupt_global_timeline_fails_independent_audit(self):
        raw, plan = fixture('memory')
        result, prepared, _ = Evaluator(raw, SETTINGS).evaluate(plan)
        result['makespan'] += 1
        with self.assertRaises(AssertionError):
            audit_result(prepared.bundle, result, SETTINGS)

    def test_empty_core_padding_replays_without_changing_objective(self):
        raw, _ = fixture('shared_3')
        plan = read_json(FIX / 'shared_3/final_plan.json')
        base = objective(evaluate_fresh(raw, plan, SETTINGS))
        for n in (3, 4, 5):
            padded = copy.deepcopy(plan)
            padded['core_schedules'].extend([[] for _ in range(n - 2)])
            result, prepared, _ = Evaluator(raw, SETTINGS).evaluate(padded)
            self.assertEqual(objective(result), base)
            audit_result(prepared.bundle, result, SETTINGS)

    def test_original_official_files_remain_pinned(self):
        self.assertEqual(Settings.read().scene.hashes, SETTINGS.scene.hashes)
        provenance = read_json(FIX / 'provenance.json')
        for name, record in provenance['files'].items():
            self.assertEqual(file_hash(FIX / name), record['sha256'])


class BoundAndCandidateTests(unittest.TestCase):
    def test_200_bandwidth_relaxations_against_exhaustive_integer_allocations(self):
        rng = random.Random(20260926)
        for _ in range(200):
            fixed = rng.randrange(30)
            groups = [(rng.randint(1, 4), rng.randint(1, 20), rng.randint(1, 12)) for _ in range(rng.randint(1, 4))]
            integer_best = min(max(fixed + sum((m - h) * d for (m, d, l), h in zip(groups, hits)),
                                   sum(h * l for (m, d, l), h in zip(groups, hits)))
                               for hits in itertools.product(*(range(m) for m, _, _ in groups)))
            value = bandwidth_lower_bound(fixed, groups)
            self.assertIsInstance(value, Fraction)
            self.assertLessEqual(value, integer_best)
            self.assertGreaterEqual(value, 0)

    def test_lexicographic_pruning_allows_copy_tradeoffs_for_faster_schedules(self):
        self.assertFalse(cannot_improve(9, 100000, (10, 0)))
        self.assertFalse(cannot_improve(10, 0, (10, 1)))
        self.assertTrue(cannot_improve(10, 1, (10, 1)))
        self.assertTrue(cannot_improve(11, -1, (10, 0)))

    def test_joint_candidate_escapes_s4_stagnation(self):
        raw, plan = fixture('shared_6')
        evaluator = Evaluator(raw, SETTINGS)
        initial, prepared, _ = evaluator.evaluate(plan)
        options = parameters()
        proposals = list(candidates(evaluator, plan, initial, prepared, 'joint', options))
        self.assertLessEqual(len(proposals), 24)
        results = [evaluator.evaluate(p)[0] for _, p in proposals]
        self.assertEqual(min(r['makespan'] for r in results), 7481)

    def test_capacity_window_reduces_real_spilling(self):
        raw, plan = fixture('memory')
        evaluator = Evaluator(raw, SETTINGS)
        initial, prepared, _ = evaluator.evaluate(plan)
        values = [evaluator.evaluate(p)[0] for family, p in candidates(evaluator, plan, initial, prepared, 'cheap', parameters())
                  if family == 'capacity']
        self.assertTrue(values)
        self.assertTrue(any(r['data_movement_bytes']['spill_added_copy_bytes'] == 0 for r in values))
        self.assertLess(min(r['makespan'] for r in values), initial['makespan'])

    def test_fixed_limits_reject_nonfinite_or_unbounded_parameters(self):
        for change in ({'seconds': float('nan')}, {'max_candidates': 0}, {'window_width': 9},
                       {'diagnostic_seeds': 9}, {'migration_targets': 4}, {'joint_candidates': 25},
                       {'use_bounds': 1}, {'seconds': 10, 'verification_reserve': 20}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parameters(change)


class EndToEndTests(unittest.TestCase):
    def test_published_q2_and_fixed_singlecore_are_reused_as_known_results(self):
        graph = ROOT / 'official/data/case_001.json'
        if not graph.is_file():
            self.skipTest('official case inputs are optional outside the reproduction package')
        plan, record = find_input(graph, 2, None, SETTINGS)
        self.assertTrue(record['frozen_objective'])
        self.assertEqual(tuple(record['expected_objective']), objective(evaluate_fresh(read_json(graph), plan, SETTINGS, problem=2)))
        fixed = fixed_singlecore(graph, SETTINGS)
        self.assertTrue(fixed['frozen_objective'])
        self.assertEqual(fixed['expected_objective'][0], 233110)

    def test_transient_windows_file_lock_retries_and_persistent_failure_is_not_hidden(self):
        from q3.artifacts import retry_locked
        with patch('q3.artifacts.time.sleep'):
            work = unittest.mock.Mock(side_effect=[PermissionError('sharing violation'), PermissionError('sharing violation'), 'written'])
            self.assertEqual(retry_locked(work), 'written')
            self.assertEqual(work.call_count, 3)
            with self.assertRaises(PermissionError):
                retry_locked(unittest.mock.Mock(side_effect=PermissionError('persistent')))

    def test_s1_s4_search_with_and_without_bounds_reproduces_reported_times(self):
        for name, expected in [('shared_0', 7121), ('shared_1', 7121), ('shared_3', 10722), ('shared_6', 7481)]:
            results = []
            for bounds in (False, True):
                with self.subTest(case=name, bounds=bounds), tempfile.TemporaryDirectory() as temp:
                    output = Path(temp)
                    rows = solve_case(FIX / name / 'graph.json', [2], output,
                        q2_source=FIX / name / 'initial_plan.json',
                        options={'seconds': 60, 'verification_reserve': 10, 'use_bounds': bounds})
                    self.assertEqual(rows[0]['status'], 'SUCCESS_VERIFIED', rows)
                    self.assertEqual(rows[0]['t3_cycles'], expected)
                    self.assertTrue(verified_directory(output / 'graph'))
                    results.append((read_json(output / 'graph/N2/graph_multicore_res.json'),
                                    normalized(read_json(output / 'graph/N2/verified/result.json'))))
            self.assertEqual(results[0], results[1])

    def test_missing_q2_automatically_bootstraps_all_requested_cores_and_resume_is_hashed(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'results'
            empty = Path(temp) / 'empty_q2'
            empty.mkdir()
            kwargs = dict(q2_source=empty, options={'seconds': 60, 'verification_reserve': 10, 'q2_seconds': 30})
            rows = solve_case(FIX / 'minimal/graph.json', [1, 2, 3, 4, 5], output, **kwargs)
            self.assertTrue(all(r['status'] == 'SUCCESS_VERIFIED' and r['t3_cycles'] == 6 for r in rows), rows)
            self.assertEqual(rows[0]['q2_source_kind'], 'fixed_singlecore')
            self.assertTrue(all(r['q2_source_kind'] == 'generated_q2' and r['q2_generated_seconds'] > 0 for r in rows[1:]))
            self.assertGreater(rows[0]['case_prerequisite_seconds'], 0)
            self.assertLess(rows[0]['case_q3_seconds'], rows[0]['case_end_to_end_seconds'])
            resumed = solve_case(FIX / 'minimal/graph.json', [1, 2, 3, 4, 5], output, resume=True, **kwargs)
            self.assertEqual(resumed, rows)
            result_path = output / 'graph/N2/verified/result.json'
            result_path.write_text('{}', encoding='utf-8')
            self.assertFalse(verified_directory(output / 'graph'))
            with self.assertRaises(ValueError):
                solve_case(FIX / 'minimal/graph.json', [1, 2, 3, 4, 5], output, resume=True, **kwargs)

    def test_invalid_q2_plan_is_not_replaced_with_a_silent_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bad_plan.json'
            write_json(path, {'node_to_subgraph': {}, 'core_schedules': [[], []]})
            rows = solve_case(FIX / 'minimal/graph.json', [2], Path(temp) / 'out', q2_source=path,
                              options={'seconds': 30, 'verification_reserve': 10})
            self.assertEqual(rows[0]['status'], 'FAILED')
            self.assertFalse((Path(temp) / 'out/graph/complete.json').exists())

    def test_worker_timeout_does_not_poison_next_evaluation(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = Worker(FIX / 'minimal/graph.json', SETTINGS, parameters(), temp)
            request = dict(action='evaluate', plan=str(FIX / 'minimal/initial_plan.json'), output=str(Path(temp) / 'r.json'))
            with self.assertRaises(TimeoutError):
                worker.call(request, .000001)
            self.assertIsNone(worker.process)
            try:
                result = worker.call(request, 10)
                self.assertEqual(tuple(result['objective']), (6, 0))
            finally:
                worker.close()

    def test_subprocess_timeout_is_bounded(self):
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_command([sys.executable, '-c', 'import time; time.sleep(20)'], timeout=.2, text=True)
        self.assertLess(time.monotonic() - start, 5)

    def test_official_trace_and_log_are_real_and_match_result(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            result = replay(FIX / 'shared_3/graph.json', FIX / 'shared_3/final_plan.json', SETTINGS, directory, 15)
            self.assertEqual(result['makespan'], 10722)
            self.assertTrue(read_json(directory / 'trace.json')['traceEvents'])
            self.assertGreater((directory / 'log.txt').stat().st_size, 0)


class ReportingTests(unittest.TestCase):
    def test_ratios_use_per_case_arithmetic_mean_and_partial_coverage_is_explicit(self):
        rows = []
        for name, t2, t3 in [('small', 100, 10), ('large', 10000, 10000)]:
            rows.append(dict(case=name, num_cores=2, status='SUCCESS_VERIFIED', t2_cycles=t2,
                             t2_to_3_cycles=t2, t3_cycles=t3, hardware_speedup=1,
                             algorithm_speedup=t2 / t3, cache_speedup=t2 / t3,
                             added_copy_bytes=0, cache_hit_rate_bytes=0, actual_ddr_bytes=0))
        with tempfile.TemporaryDirectory() as temp:
            summary = summarize(rows, [('small', 2), ('large', 2), ('missing', 2)], Path(temp), plots=False)
            self.assertFalse(summary['complete'])
            self.assertEqual(summary['by_core'][2]['mean_cache_speedup'], 5.5)
            self.assertEqual(summary['missing'], [('missing', 2)])
            self.assertNotIn(b'\r\r\n', (Path(temp) / 'details.csv').read_bytes())

    def test_duplicate_result_cannot_inflate_coverage(self):
        row = dict(case='a', num_cores=2, status='FAILED')
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
            summarize([row, row], [('a', 2)], Path(temp), plots=False)


if __name__ == '__main__':
    unittest.main()
