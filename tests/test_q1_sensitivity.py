"""Experiment selection, official replay, identity protection and paired statistics."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
sys.path.insert(0,str(ROOT/'src'))
from q1_sensitivity import select_cases, prepare, job, audit, run, official
from report_q1_sensitivity import summarize, report
from test_q1_initialization import graph_of
from q1.solver import initialize


class SensitivityTests(unittest.TestCase):
    def test_stratified_sample_is_deterministic_and_balanced(self):
        population=[{'case':f'case_{i:03d}','compute_operations':i} for i in range(100)]
        params={'sampling_seed':20260927,'strata':4,'cases_per_stratum':5,'expected_population':100}
        first=select_cases(population,params)
        self.assertEqual(first,select_cases(list(reversed(population)),params))
        self.assertEqual(len({r['case'] for r in first}),20)
        for stratum in range(1,5):
            group=[r for r in first if r['size_stratum']==stratum]
            self.assertEqual(len(group),5)
            self.assertTrue(all(25*(stratum-1)<=r['compute_operations']<25*stratum for r in group))
        with self.assertRaises(ValueError):select_cases(population[:-1],params)
        with self.assertRaises(ValueError):select_cases(population,params,['missing'])

    def test_official_roundtrip_native_beta1_resume_and_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);data=root/'data';data.mkdir();output=root/'output'
            graph=graph_of(8,links=((0,1),(1,2),(3,4),(4,5)))
            (data/'case_001.json').write_text(json.dumps(graph),encoding='utf-8')
            manifest=prepare(output,data,cases=['case_001'],cores=[2])
            self.assertEqual(manifest['expected_rows'],5)
            stale=output/'runs/case_001/.N2-interrupted';stale.mkdir(parents=True)
            (stale/'evidence.txt').write_text('preserve',encoding='utf-8')
            rows=job('case_001',2,str(output))
            self.assertEqual(len(rows),5)
            self.assertEqual((stale/'evidence.txt').read_text(),'preserve')
            status,_=audit(output)
            self.assertEqual(status['status'],'SUCCESS_VERIFIED')
            native=initialize(graph,2,official.Settings.read(ROOT/'official/data/config.txt'),beta_search='fixed')
            pair=output/'runs/case_001/N2'
            self.assertEqual(official.read_json(pair/'beta1/plan.json'),native.plan)
            completed_at=(pair/'complete.json').stat().st_mtime_ns
            self.assertEqual(job('case_001',2,str(output)),rows)
            self.assertEqual((pair/'complete.json').stat().st_mtime_ns,completed_at)
            report(output,no_plots=True)
            self.assertTrue((output/'summary.csv').is_file())
            p=pair/'beta1/plan.json';p.write_text('{}',encoding='utf-8')
            with self.assertRaises(ValueError):audit(output)

    def test_partial_and_input_identity_and_run_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);data=root/'data';data.mkdir();output=root/'output'
            path=data/'case_001.json';path.write_text(json.dumps(graph_of(3)),encoding='utf-8')
            prepare(output,data,cases=['case_001'],cores=[2])
            with self.assertRaises(ValueError):audit(output)
            status,rows=audit(output,allow_partial=True)
            self.assertEqual(status['verified_rows'],0);self.assertFalse(rows)
            (output/'run.lock').write_text('{}')
            with self.assertRaisesRegex(ValueError,'run.lock'):run(output,1)
            path.write_text(path.read_text()+'\n')
            with self.assertRaisesRegex(AssertionError,'Input changed'):audit(output,allow_partial=True)

    def test_statistics_pair_before_averaging(self):
        rows=[]
        for case,base,fixed in [('a',10,20),('b',100,100)]:
            for variant in ['beta1','beta2','beta3','beta4','adaptive']:
                rows.append({'case':case,'num_cores':2,'variant':variant,
                             'makespan_cycles':base if variant=='adaptive' else fixed,
                             'task_count':2,'added_copy_bytes':0,'spill_added_copy_bytes':0,
                             'solve_seconds':1,'verification_seconds':1})
        _,summary=summarize(rows)
        beta1=next(r for r in summary if r['variant']=='beta1')
        self.assertEqual(beta1['mean_relative_pct'],50)
        self.assertEqual((beta1['better'],beta1['equal'],beta1['worse']),(0,1,1))


if __name__=='__main__':unittest.main()
