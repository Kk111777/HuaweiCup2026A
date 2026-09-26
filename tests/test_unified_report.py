"""Reports must use the case clock and reject old per-core budget artifacts."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('report_unified', ROOT/'tools/report_unified.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class UnifiedReportTests(unittest.TestCase):
    def fixture(self, folder):
        folder = Path(folder)
        case = folder/'case_001'
        case.mkdir()
        manifest = dict(cases=[1], cores=[2,3,4,5], target_seconds=600,
                        budget_scope='case_all_cores', singlecore_in_solver=False)
        request = dict(source_signature='fixture', cores=manifest['cores'], seconds=600,
                       budget_scope='case_all_cores')
        summary = dict(source_signature='fixture', cores=manifest['cores'],
            budget_scope='case_all_cores', target_seconds=600, extension_seconds=900,
            hard_limit_seconds=1200, baseline_seconds=0, wall_seconds=640,
            cpu_seconds=610, shared_preparation_seconds=25, exceeded_target=True,
            extended=True, exceeded_extension=False, hard_timeout=False, status='ok')
        for name, value in (('request', request), ('summary', summary)):
            (case/f'{name}.json').write_text(json.dumps(value), encoding='utf-8')
        rows = [dict(case='case_001', cores=n, wall_seconds=150, cpu_seconds=145,
                     cycles=100, status='ok', hard_timeout=False, exceeded_target=False)
                for n in manifest['cores']]
        return folder, rows, manifest, case, summary

    def test_case_overrun_is_not_hidden_by_four_short_core_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            root, rows, manifest, _, _ = self.fixture(folder)
            actual = report.case_timing_rows(root, rows, manifest, 'fixture')[0]
            self.assertEqual(actual['wall_seconds'], 640)
            self.assertEqual(actual['sum_target_wall_seconds'], 600)
            self.assertTrue(actual['exceeded_target'])
            self.assertEqual(actual['shared_preparation_seconds'], 25)

    def test_legacy_per_core_manifest_cannot_claim_case_budget_compliance(self):
        with tempfile.TemporaryDirectory() as folder:
            root, rows, manifest, _, _ = self.fixture(folder)
            manifest['budget_scope'] = 'case_core'
            with self.assertRaisesRegex(ValueError, 'legacy per-core'):
                report.case_timing_rows(root, rows, manifest, 'fixture')

    def test_mixed_budget_or_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, rows, manifest, case, summary = self.fixture(folder)
            for field, value in (('target_seconds', 150), ('source_signature', 'old'),
                                 ('baseline_seconds', 20)):
                changed = dict(summary, **{field:value})
                (case/'summary.json').write_text(json.dumps(changed), encoding='utf-8')
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'inconsistent'):
                    report.case_timing_rows(root, rows, manifest, 'fixture')


if __name__ == '__main__':
    unittest.main()
