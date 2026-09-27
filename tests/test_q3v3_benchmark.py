"""Sampling balance and cross-run warm-start identity gates."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from benchmark_q3v3 import check_warm_comparison, stratified_sample


class BenchmarkTests(unittest.TestCase):
    def test_sample_is_balanced_distinct_and_reproducible(self):
        keys=[(f'case_{i:03}',n) for i in range(1,101) for n in (2,3,4,5)]
        population, pairs=stratified_sample(keys,5,20260927)
        self.assertEqual(len(population),100)
        self.assertEqual(len(pairs),20)
        self.assertEqual(len({c for c,n in pairs}),20)
        self.assertEqual([sum(n==k for c,n in pairs) for k in (2,3,4,5)],[5]*4)
        self.assertEqual((population,pairs),stratified_sample(reversed(keys),5,20260927))
        self.assertNotEqual(pairs,stratified_sample(keys,5,20260928)[1])

    def test_incomplete_population_cannot_silently_reduce_sample(self):
        keys=[(f'case_{i:03}',n) for i in range(1,20) for n in (2,3,4,5)]
        with self.assertRaises(ValueError):
            stratified_sample(keys,5,1)

    def test_reference_gate_rejects_different_plan_score_and_secondary_regression(self):
        item=dict(plan_sha256='a',reference_cycles=100,reference_added_copy_bytes=10)
        inherited=dict(makespan_cycles=100,added_copy_bytes=10)
        row=dict(source_hashes=dict(warm_plan='a'),supplied_plan_makespan=100,
                 makespan_cycles=90,added_copy_bytes=20)
        check_warm_comparison(row,item,inherited)
        wrong=copy.deepcopy(row)
        wrong['source_hashes']['warm_plan']='b'
        with self.assertRaises(ValueError):
            check_warm_comparison(wrong,item,inherited)
        wrong=copy.deepcopy(row)
        wrong['supplied_plan_makespan']=99
        with self.assertRaises(ValueError):
            check_warm_comparison(wrong,item,inherited)
        wrong=copy.deepcopy(row)
        wrong['makespan_cycles']=100
        wrong['added_copy_bytes']=11
        with self.assertRaises(ValueError):
            check_warm_comparison(wrong,item,inherited)


if __name__=='__main__':
    unittest.main()
