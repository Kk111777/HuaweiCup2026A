"""自适应 β 的 Q1 语义对照、Q2 管线接线、官方去重及预算回退。"""
from pathlib import Path
import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from q2 import pipeline
from q2.beta import BetaSearch
from q2.construct import BudgetExpired
from q2.official import Q2Error, digest, read_json
import test_q1_initialization as q1_tests
import test_q2 as q2_tests


class BetaPolicyTests(unittest.TestCase):
    def test_matches_q1_visited_betas_and_stops_on_controlled_curves(self):
        curves_list = [
            {'global': [100, 90, 90, 1], 'components': [140, 130, 120, 80]},
            {'global': [(100, 10), (100, 5), (1, 0), (1, 0)], 'components': [150, 160, 1, 1]},
            {'global': [100, 110, 1, 1], 'components': [140, 130, 120, 90]},
            {'global': [100, None, 1, 1], 'components': [140, 130, 120, 90]},
            {'global': [None, 1, 1, 1], 'components': [140, 150, 1, 1]},
        ]
        for policy in ('adaptive', 'fixed', 'grid'):
            for curves in curves_list:
                with self.subTest(policy=policy, curves=curves):
                    seen = []
                    with patch('q1.solver._evaluate_candidate',
                               side_effect=q1_tests.BetaSearchTests.evaluator(curves, seen)):
                        q1 = q1_tests.initialize(q1_tests.graph_of(2), 2, q1_tests.SETTINGS, beta_search=policy)
                    search = BetaSearch(['global', 'component_grouped'], [1, 2, 3, 4], policy)
                    visits = []; successful = []
                    while (spec := search.next_candidate()) is not None:
                        mode, beta = spec
                        old_mode = 'components' if mode == 'component_grouped' else mode
                        value = curves[old_mode][beta-1]
                        value = (value, 0) if isinstance(value, int) else value
                        visits.append((old_mode, beta)); search.observe(mode, beta, value)
                        if value is not None:
                            successful.append((*value, int(mode != 'global'), beta))
                    self.assertEqual(sorted(visits), sorted(seen))
                    self.assertEqual([s['stop_reason'] for s in search.report['modes']],
                                     [s['stop_reason'] for s in q1.search['modes']])
                    best = min(successful)
                    self.assertEqual((best[0], best[1], best[3]),
                                     (q1.evaluation['makespan'], q1.evaluation['data_movement_bytes']['added_copy_bytes'], q1.selected['beta']))
                    self.assertEqual(q1.selected['mode'], 'global' if best[2] == 0 else 'components')
                    # 跨模式轮转仅改变访问先后，先取得所有模式的 β=1。
                    self.assertEqual(visits[:2], [('global', 1), ('components', 1)])

    def test_tie_stops_without_probing_later_reversal(self):
        search = BetaSearch(['global'], [1, 2, 3, 4], 'adaptive')
        self.assertEqual(search.next_candidate(), ('global', 1))
        search.observe('global', 1, (100, 10))
        self.assertEqual(search.next_candidate(), ('global', 2))
        decision = search.observe('global', 2, (100, 5), cached=False)
        self.assertIsNone(search.next_candidate())
        self.assertFalse(decision['strictly_improved'])
        self.assertEqual(search.states['global']['best_beta'], 2)

    def test_invalid_options_and_legacy_version_rejected(self):
        good = pipeline.load_options(ROOT/'configs/q2/legacy_1_1.json')
        self.assertEqual(good['construction']['beta_search'], 'adaptive')
        for key, value in [('beta_search', 'unknown'), ('betas', [2, 1]), ('betas', [1, 3]),
                           ('betas', [True]), ('betas', []), ('betas', [1, 2, 3, 4, 5])]:
            options = copy.deepcopy(good); options['construction'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Q2Error):
                pipeline.validate_options(options)
        options = copy.deepcopy(good); options['algorithm_version'] = 'Q2-Baseline-Handoff-1.0'
        with self.assertRaises(Q2Error): pipeline.validate_options(options)

    def test_cli_policy_override_and_config_default(self):
        from q2.__main__ import main
        for policy in (None, 'adaptive', 'fixed', 'grid'):
            args = ['solve', str(q2_tests.FIX/'minimal.json'), '--cores', '2', '--without-baseline', '--output-dir', 'outputs/unused']
            if policy is not None: args += ['--beta-search', policy]
            with patch('q2.__main__.batch', return_value=dict(successful=1, requested=1, complete=True)) as batch:
                self.assertEqual(main(args), 0)
                self.assertEqual(batch.call_args.args[3]['construction']['beta_search'], policy or 'adaptive')


class BetaPipelineTests(unittest.TestCase):
    def run_curves(self, curves, policy='adaptive', modes=None, interrupted=None):
        """真实构造接口/前缀/发布链；用可控官方响应隔离搜索逻辑，不作为实际成绩。"""
        options = pipeline.load_options(ROOT/'configs/q2/legacy_1_1.json')
        options['construction']['beta_search'] = policy
        options['enhancement']['enabled'] = False
        options['runtime'].update(wall_budget_seconds=30, final_verification_reserve_seconds=5)
        if modes is not None: options['construction']['scope_modes'] = modes
        plans = {}; visits = []; calls = []
        raw, _ = q2_tests.fixture('memory_interleaved')

        def construct(g, cores, mode, beta, *args, **kwargs):
            if interrupted == (mode, beta):
                kwargs['stats'].update(trials=7)
                raise BudgetExpired('test interruption')
            visits.append((mode, beta))
            k = int(mode != 'global')
            groups = min(beta, len(g.ids))
            assignment = {str(g.ids[v]): min(i, groups-1) for i, v in enumerate(g.order)}
            plan = dict(node_to_subgraph=assignment, core_schedules=[[], []])
            plan['core_schedules'][k] = list(range(groups))
            plans[digest(plan)] = (mode, beta)
            work, size = g.base_traffic([k]*len(g.ids))
            # proxy 故意偏爱大 β；测试必须由官方曲线决定后续与最终 B0。
            return plan, dict(mode=mode, beta=beta, proxy_J=1000-beta*100,
                              base_copy_work=work, base_copy_bytes=size)

        def evaluate(graph, plan, settings, directory, timeout, records, phase):
            mode, beta = plans[digest(plan)]
            value = curves[mode][beta-1]
            calls.append((mode, beta, phase))
            record = dict(phase=phase, plan_hash=digest(plan), status='OFFICIAL_ERROR' if value is None else 'OK',
                          launched=True, wall_seconds=.001)
            records.append(record)
            if value is None: return None
            time, copied = (value, 0) if isinstance(value, int) else value
            result = dict(makespan=time, data_movement_bytes=dict(added_copy_bytes=copied), scene='B', num_cores=2)
            directory.mkdir(parents=True)
            (directory/'result.json').write_text(json.dumps(result))
            for name in ('trace.json', 'result.log'): (directory/name).write_text('{}')
            return result

        with tempfile.TemporaryDirectory() as d, patch.object(pipeline, 'construct', side_effect=construct), \
                patch.object(pipeline, 'run_official', side_effect=evaluate):
            path = Path(d)/'controlled.json'; path.write_text(json.dumps(raw))
            result = pipeline.solve(path, 2, Path(d)/'out', options)
            self.assertEqual(result['status'], 'SUCCESS_VERIFIED')
            self.assertTrue((Path(d)/'out/controlled/N2/complete.json').is_file())
        return result, visits, calls

    def test_modes_early_stop_and_best_official_not_first_valid(self):
        # 执行签名相同但方案/子图边界不同，也不能复用完整官方结果。
        with patch.object(pipeline, 'execution_digest', return_value='same-execution-different-plans'):
            result, visits, calls = self.run_curves({'global': [100, 90, 90, 1], 'component_grouped': [140, 130, 120, 80]})
        self.assertEqual(result['baseline_objective'], (80, 0))
        self.assertEqual(result['selected_candidate']['mode'], 'component_grouped')
        self.assertEqual(result['selected_candidate']['beta'], 4)
        self.assertNotIn(('global', 4), visits)
        self.assertEqual(result['search']['official_evaluations'], 7)
        self.assertEqual(calls[-1], ('component_grouped', 4, 'final_verification'))

    def test_fixed_and_grid_change_only_parameter_search(self):
        curves = {'global': [100, 100, 1, 20], 'component_grouped': [90, 95, 5, 6]}
        fixed, visits, _ = self.run_curves(curves, 'fixed')
        self.assertEqual(visits, [('global', 1), ('component_grouped', 1)])
        self.assertEqual(fixed['baseline_objective'], (90, 0))
        adaptive, _, _ = self.run_curves(curves)
        self.assertEqual(adaptive['baseline_objective'], fixed['baseline_objective'])
        grid, visits, _ = self.run_curves(curves, 'grid')
        self.assertEqual(len(visits), 8)
        self.assertEqual(grid['baseline_objective'], (1, 0))

    def test_equal_makespan_lower_bytes_selected_but_stops(self):
        result, visits, _ = self.run_curves({'global': [(100, 10), (100, 5), (1, 0)]}, modes=['global'])
        self.assertEqual(result['baseline_objective'], (100, 5))
        self.assertEqual(visits, [('global', 1), ('global', 2)])
        self.assertEqual(result['selected_candidate']['beta'], 2)

    def test_failure_stops_one_mode_preserves_successful_candidate(self):
        result, visits, _ = self.run_curves({'global': [90, None, 1], 'component_grouped': [100, 110, 1]})
        self.assertEqual(result['baseline_objective'], (90, 0))
        self.assertNotIn(('global', 3), visits)
        self.assertEqual(result['search']['modes'][0]['stop_reason'], 'candidate_failed')
        self.assertIn(('component_grouped', 2), visits)

    def test_budget_keeps_incumbent_and_replays_without_fallback(self):
        result, visits, calls = self.run_curves({'global': [100, 90], 'component_grouped': [95, 80]}, interrupted=('global', 2))
        self.assertEqual(result['baseline_objective'], (95, 0))
        self.assertEqual(visits, [('global', 1), ('component_grouped', 1)])
        self.assertFalse(result['search']['fallback_used'])
        self.assertEqual(result['interrupted_construction']['trials'], 7)
        self.assertEqual(result['stop_reason'], 'construction_budget')
        self.assertEqual(calls[-1], ('component_grouped', 1, 'final_verification'))

    def test_real_duplicate_uses_cached_official_feedback_then_stops(self):
        options = pipeline.load_options(ROOT/'configs/q2/legacy_1_1.json'); options['enhancement']['enabled'] = False
        with tempfile.TemporaryDirectory() as d:
            result = pipeline.solve(q2_tests.FIX/'minimal.json', 2, d, options)
            self.assertEqual(result['final_objective'], (6, 0))
            self.assertEqual(result['search']['modes'][0]['evaluated_betas'], [1, 2])
            self.assertEqual(result['search']['official_evaluations'], 1)
            self.assertEqual(result['official_evaluations'], 2)  # B0 + 强制独立复评
            self.assertEqual(result['search']['cache_hits'], 1)
            self.assertEqual(result['candidates'][1]['evaluation_status'], 'CACHED')
            self.assertEqual(result['candidates'][1]['reused_candidate_index'], 0)
            other = copy.deepcopy(options); other['construction']['beta_search'] = 'fixed'
            with self.assertRaises(Q2Error):
                pipeline.solve(q2_tests.FIX/'minimal.json', 2, d, other, resume=True)

    def test_grid_does_not_cache_failed_evaluation(self):
        options = pipeline.load_options(ROOT/'configs/q2/legacy_1_1.json'); options['construction']['beta_search'] = 'grid'
        options['enhancement']['enabled'] = False
        real = pipeline.run_official
        def fail_once(graph, plan, settings, directory, timeout, calls, phase):
            if not calls:
                calls.append(dict(phase=phase, plan_hash=digest(plan), launched=False,
                                  status='EVAL_TIMEOUT', wall_seconds=0))
                return None
            return real(graph, plan, settings, directory, timeout, calls, phase)
        with tempfile.TemporaryDirectory() as d, patch.object(pipeline, 'run_official', side_effect=fail_once):
            result = pipeline.solve(q2_tests.FIX/'minimal.json', 2, d, options)
            self.assertEqual(result['status'], 'SUCCESS_VERIFIED')
            self.assertEqual(result['selected_candidate']['beta'], 2)
            self.assertEqual(result['search']['cache_hits'], 2)
            self.assertEqual([c['phase'] for c in result['official_calls']], ['B0', 'B0', 'final_verification'])

    def test_cached_winner_loads_its_full_matching_result(self):
        options = pipeline.load_options(ROOT/'configs/q2/legacy_1_1.json')
        options['construction'].update(beta_search='fixed', scope_modes=['component_grouped', 'global'])
        options['enhancement']['enabled'] = False
        with tempfile.TemporaryDirectory() as d:
            result = pipeline.solve(q2_tests.FIX/'memory_grouped.json', 1, d, options)
            self.assertEqual(result['status'], 'SUCCESS_VERIFIED')
            self.assertEqual(result['selected_candidate']['mode'], 'global')
            self.assertEqual(result['selected_candidate']['evaluation_status'], 'CACHED')
            self.assertEqual(result['official_evaluations'], 2)
            self.assertEqual(result['search']['cache_hits'], 1)


if __name__ == '__main__': unittest.main()
