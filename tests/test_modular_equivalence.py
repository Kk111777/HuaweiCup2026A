"""Cross-source acceptance, not a second implementation of search decisions."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify_modular_equivalence',
                                               ROOT / 'tools/verify_modular_equivalence.py')
equivalence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(equivalence)


class DifferentialHarnessTests(unittest.TestCase):
    def test_equal_final_score_does_not_hide_candidate_or_acceptance_drift(self):
        left = dict(final=dict(cycles=100, plan='p'), events=[dict(action='split', accepted=True, plan='a')])
        right = dict(final=dict(cycles=100, plan='p'), events=[dict(action='split', accepted=True, plan='b')])
        self.assertEqual(equivalence.first_difference(left, right)['path'], '$.events[0].plan')
        self.assertIsNone(equivalence.first_difference(left, left))

    def test_snapshot_rejects_traversal_and_changed_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as out:
                out.writestr('../outside.py', 'bad')
            with self.assertRaisesRegex(ValueError, 'unsafe snapshot'):
                equivalence.extract_snapshot(archive, root / 'extracted')
            self.assertFalse((root / 'outside.py').exists())
            with zipfile.ZipFile(archive, 'w') as out:
                out.writestr('src/q1/unified/engine.py', 'changed')
            with self.assertRaisesRegex(ValueError, 'manifest mismatch'):
                equivalence.extract_snapshot(archive, root / 'extracted',
                                              {'src/q1/unified/engine.py': 'wrong'})

    def test_additive_metadata_is_checked_without_hiding_history_changes(self):
        old = dict(parameters={'max_rounds': 1}, behavior=dict(outputs=[dict(history={
            'records': [{'accepted': True}], 'structure_control': None})]))
        new = dict(parameters={'max_rounds': 1}, behavior=dict(outputs=[dict(history={
            'records': [{'accepted': True}], 'structure_control': None,
            'problem_id': 'q1', 'backend_version': 'test',
            'requested_params': {'max_rounds': 1}, 'effective_params': {'max_rounds': 1}})]))
        self.assertIsNone(equivalence.check_added_metadata(old, new))
        self.assertEqual(equivalence.behavior_projection(old), equivalence.behavior_projection(new))
        new['behavior']['outputs'][0]['history']['records'][0]['accepted'] = False
        self.assertIsNotNone(equivalence.first_difference(equivalence.behavior_projection(old),
                                                          equivalence.behavior_projection(new)))
        new['behavior']['outputs'][0]['history']['effective_params']['max_rounds'] = 2
        self.assertIn('effective_params', equivalence.check_added_metadata(old, new))

    @unittest.skipUnless(equivalence.DEFAULT_BEFORE.is_file(), 'frozen pre-refactor archive not installed')
    def test_frozen_version_matches_new_in_both_modes_and_budget_scopes(self):
        with tempfile.TemporaryDirectory() as folder:
            summary = equivalence.compare_suite(equivalence.DEFAULT_BEFORE, ROOT, folder,
                cases=['tiny_independent'], cores=[2, 3], rounds=1,
                manifest=equivalence.DEFAULT_BEFORE.with_name('before_manifest.json'))
            self.assertTrue(summary['passed'], summary['scenarios'])
            self.assertEqual(len(summary['scenarios']), 4)
            coverage = summary['coverage']
            self.assertEqual(coverage['solves'], 8)
            self.assertGreater(coverage['approximate_records'], 0)
            self.assertGreater(coverage['approximate_acceptances'], 0)
            self.assertGreater(coverage['calibration_events'], 0)
            self.assertEqual(coverage['shared_case_solves'], 4)
            artifact = equivalence.read(Path(folder) / 'tiny_independent_adaptive_shared_case/after.json')
            old = equivalence.read(Path(folder) / 'tiny_independent_adaptive_shared_case/before.json')
            self.assertNotEqual(artifact['identity']['source'], old['identity']['source'])
            self.assertTrue(Path(artifact['identity']['engine_file']).is_relative_to(ROOT))
            self.assertGreater(coverage['proposal_batches'], 0)


if __name__ == '__main__':
    unittest.main()
