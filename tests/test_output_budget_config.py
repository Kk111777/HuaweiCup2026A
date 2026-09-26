"""Output selection and case-wide budgets must not change plan semantics."""
import copy
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from scheduling.configuration import intermediate_artifacts, resolve_time_budget
from scheduling.budget import SearchBudget, CaseSliceBudget
from q1.unified.api import solve_case, solve_file, default_parameters
from q1 import official
from q1.unified.io import read, write
from test_unified import graph_of, SETTINGS


def inline(target, args, deadline, telemetry=None):
    target(*args)
    return False


class OutputBudgetConfigurationTests(unittest.TestCase):
    def test_legacy_defaults_and_explicit_seconds(self):
        self.assertTrue(intermediate_artifacts({}))
        self.assertEqual(resolve_time_budget({}), dict(target_seconds=600,extension_seconds=900,hard_limit_seconds=1200))
        self.assertEqual(resolve_time_budget({},20), dict(target_seconds=20,extension_seconds=30,hard_limit_seconds=40))

    def test_custom_budget_and_override_scale(self):
        p={'time_budget':dict(target_seconds=1000,extension_seconds=1200,hard_limit_seconds=1800)}
        before=copy.deepcopy(p)
        self.assertEqual(resolve_time_budget(p),p['time_budget'])
        self.assertEqual(resolve_time_budget(p,500),dict(target_seconds=500,extension_seconds=600,hard_limit_seconds=900))
        self.assertEqual(p,before)
        with patch('scheduling.budget.time.perf_counter',return_value=0):
            parent=SearchBudget(1000,started=0,extension=1200,hard=1800)
            child=CaseSliceBudget(parent,1)
            self.assertEqual((child.target,child.extension,child.hard),(1000,1200,1800))

    def test_legacy_extended_small_slice_is_not_capped_at_target(self):
        with patch('scheduling.budget.time.perf_counter',return_value=1):
            parent=SearchBudget(20,started=0);parent.extended=True
            child=CaseSliceBudget(parent,1)
            self.assertEqual(child.target,29)

    def test_invalid_configuration(self):
        for value in (0,-1,True,float('inf'),float('nan'),'600'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                resolve_time_budget({'time_budget':{'target_seconds':value}})
        for value in ({'target_seconds':10,'extension_seconds':9}, {'target_seconds':10,'hard_limit_seconds':14}, {'typo':10}):
            with self.subTest(value=value),self.assertRaises(ValueError):
                resolve_time_budget({'time_budget':value})
        for value in (0,'false',None):
            with self.assertRaises(TypeError):intermediate_artifacts({'artifacts':{'intermediate':value}})

    def parameters(self,enabled):
        p=default_parameters();p.update(max_rounds=0,enable_initial_candidates=False)
        p['artifacts']={'intermediate':enabled}
        p['time_budget']=dict(target_seconds=20,extension_seconds=25,hard_limit_seconds=30)
        return p

    def test_debug_and_minimal_keep_same_plans_and_official_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);graph=root/'graph.json';raw=graph_of(8,shared=False);write(graph,raw)
            results={}
            for enabled in (True,False):
                out=root/str(enabled);p=self.parameters(enabled);before=copy.deepcopy(p)
                with patch('scheduling.api.run_bounded',side_effect=inline):
                    result=solve_case(graph,official.REFERENCE/'data/config.txt',[2,3,4,5],out,parameters=p)
                self.assertEqual(p,before)
                self.assertEqual(result['status'],'ok',result)
                self.assertEqual((result['target_seconds'],result['extension_seconds'],result['hard_limit_seconds']),(20,25,30))
                results[enabled]=[read(out/f'N{n}/plan.json') for n in (2,3,4,5)]
                if not enabled:
                    self.assertEqual({f.relative_to(out).as_posix() for f in out.rglob('*') if f.is_file()}, {f'N{n}/plan.json' for n in (2,3,4,5)})
            self.assertEqual(results[True],results[False])
            for plan in results[False]:self.assertGreater(official.evaluate(raw,plan,SETTINGS)['makespan'],0)

    def test_single_count_minimal_does_not_restore_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);graph=root/'graph.json';write(graph,graph_of(6,shared=False));out=root/'out'
            with patch('scheduling.api.run_bounded',side_effect=inline):
                result=solve_file(graph,official.REFERENCE/'data/config.txt',2,out,parameters=self.parameters(False))
            self.assertEqual(result['status'],'ok',result)
            self.assertEqual([p.name for p in out.iterdir()],['plan.json'])

    def test_minimal_recovers_checkpoint_after_supervisor_timeout(self):
        def expire(target,args,deadline,telemetry=None):
            request,out,*_=args;out=Path(out);folder=out/'N2';folder.mkdir()
            plan={'node_to_subgraph':{'0':0},'core_schedules':[[0],[]]}
            write(folder/'checkpoint.json',dict(plan=plan,result={'makespan':7,'evaluation_source':'approximate'},wall_seconds=1,cpu_seconds=1))
            write(out/'progress.json',dict(cores=2,phase='official_evaluation',timing={}))
            return True
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);graph=root/'graph.json';write(graph,graph_of(1));out=root/'out'
            with patch('scheduling.api.run_bounded',side_effect=expire):
                result=solve_case(graph,official.REFERENCE/'data/config.txt',[2],out,parameters=self.parameters(False))
            self.assertTrue(result['hard_timeout'])
            self.assertEqual(result['rows'][0]['score_source'],'approximate')
            self.assertEqual({p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file()},{'N2/plan.json'})

    def test_nonempty_output_is_never_cleaned(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp);(out/'user.txt').write_text('keep')
            with self.assertRaisesRegex(ValueError,'empty'):
                solve_case('unused','unused',[2],out,parameters=self.parameters(False))
            self.assertEqual((out/'user.txt').read_text(),'keep')

    def test_configured_guard_is_used_by_supervisor(self):
        deadlines=[]
        def capture(target,args,deadline,telemetry=None):
            deadlines.append(deadline-args[2])
            return inline(target,args,deadline,telemetry)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);graph=root/'graph.json';write(graph,graph_of(5,shared=False))
            with patch('scheduling.api.run_bounded',side_effect=capture):
                result=solve_case(graph,official.REFERENCE/'data/config.txt',[2],root/'out',parameters=self.parameters(False))
            self.assertEqual(result['target_seconds'],20)
            self.assertAlmostEqual(deadlines[0],30)

    def test_batch_cli_respects_plan_only_and_configured_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);write(root/'case_001.json',graph_of(5,shared=False))
            params=root/'params.json';write(params,self.parameters(False))
            repo=Path(__file__).resolve().parents[1]
            command=[sys.executable,'-B',str(repo/'tools/benchmark_unified.py'),
                '--data',str(root),'--config',str(official.REFERENCE/'data/config.txt'),
                '--output',str(root/'out'),'--parameters',str(params),'--cases','1','--cores','2','3']
            process=subprocess.run(command,capture_output=True,text=True,timeout=40)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertEqual({p.relative_to(root/'out').as_posix() for p in (root/'out').rglob('*') if p.is_file()},
                             {'case_001/N2/plan.json','case_001/N3/plan.json'})
            plans={p.relative_to(root/'out').as_posix():p.read_bytes()
                   for p in (root/'out').rglob('plan.json')}
            again=subprocess.run(command,capture_output=True,text=True,timeout=40)
            self.assertNotEqual(again.returncode,0)
            self.assertIn('directory must be empty',again.stderr)
            self.assertEqual(plans,{p.relative_to(root/'out').as_posix():p.read_bytes()
                                   for p in (root/'out').rglob('plan.json')})


if __name__=='__main__':unittest.main()
