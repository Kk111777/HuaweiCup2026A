"""V3 stays separate from the unchanged v2.1 component entry points."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from q2.preparation import PreparationCache as BaselineCache, PreparationService as BaselinePreparation
from q2.optimized.preparation import PreparationCache, PreparationService
from q2.optimized import VERSION
from q2.optimized.control import parameters
from q2.optimized.api import frozen_output, reusable


class VersionTests(unittest.TestCase):
    def test_v3_version_and_v21_configuration_are_distinct(self):
        self.assertEqual(VERSION, 'Q2-Optimized-3.0')
        self.assertEqual(parameters()['algorithm_version'], VERSION)
        baseline = json.loads((ROOT/'configs/q2/baseline.json').read_text(encoding='utf-8'))
        self.assertEqual(baseline['algorithm_version'], 'Q2-Repair-2.1')

    def test_v3_extends_cache_without_mutating_baseline_class(self):
        baseline = BaselineCache()
        optimized = PreparationCache()
        self.assertEqual(set(baseline.limits), {'prefix','profile','legality','exact'})
        self.assertTrue({'prepared_core','execution','model'} <= set(optimized.limits))
        self.assertEqual(baseline.max_bytes, optimized.max_bytes)
        self.assertFalse(hasattr(BaselinePreparation, 'prepare_execution'))
        self.assertTrue(issubclass(PreparationService, BaselinePreparation))

    def test_old_versions_cannot_be_silently_resumed_or_relabelled(self):
        for old in ('Q2-Repair-2.1','Q2-Optimized-1.0'):
            with self.subTest(version=old), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                (folder/'complete.json').write_text(json.dumps(dict(algorithm_version=old,
                    schema_version=1,status='OPTIMIZATION_COMPLETE')))
                with self.assertRaises(ValueError): reusable(folder,{})
                (folder/'frozen_manifest.json').write_text(json.dumps(dict(algorithm_version=old,
                    schema_version=1,kind='q2_optimized_frozen_output')))
                with self.assertRaises(ValueError): frozen_output(folder)
                with self.assertRaises(ValueError): parameters({'algorithm_version':old})


if __name__ == '__main__': unittest.main()
