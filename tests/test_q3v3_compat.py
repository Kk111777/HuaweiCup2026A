"""Real Q1 import isolation and graph-only seed contract checks."""
from __future__ import annotations

import json
import importlib.abc
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from q3v3.q1_compat import DEFAULT_Q1_SOURCE, export_seeds, snapshot_source


def small_graph():
    return {
        "ops": [
            {"id": 1, "op": "COPY_IN", "pipe": "PIPE_MTE2", "cycles": 1},
            {"id": 2, "op": "ADD", "pipe": "PIPE_M", "cycles": 100},
            {"id": 3, "op": "ADD", "pipe": "PIPE_V", "cycles": 70},
            {"id": 4, "op": "COPY_OUT", "pipe": "PIPE_MTE3", "cycles": 1},
            {"id": 5, "op": "COPY_OUT", "pipe": "PIPE_MTE3", "cycles": 1},
        ],
        "tensors": [dict(id=t, pos="DDR" if t in (10, 14, 15) else "UB", size=60)
                    for t in range(10, 16)],
        "edges": [dict(source=a, target=b) for a, b in (
            (10, 1), (1, 11), (11, 2), (11, 3), (2, 12), (3, 13),
            (12, 4), (4, 14), (13, 5), (5, 15))],
    }


@unittest.skipUnless((DEFAULT_Q1_SOURCE / "q1/unified/seed.py").exists(),
                     "current Q1 integration source is not present")
class Q1CompatibilityTests(unittest.TestCase):
    def test_real_source_is_isolated_case_name_independent_and_unscored(self):
        # A pre-existing root q1 module must survive a foreign q1 package export.
        import q1
        original_q1, original_file = q1, q1.__file__
        original_path = list(sys.path)
        before = {name: module for name, module in sys.modules.items()
                  if name == "q1" or name.startswith("q1.")}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            graph_a, graph_b = folder / "case_001.json", folder / "arbitrary_name.json"
            content = json.dumps(small_graph())
            graph_a.write_text(content, encoding="utf-8")
            graph_b.write_text(content, encoding="utf-8")
            config = ROOT / "official/data/config.txt"
            first = export_seeds(graph_a, config, 3, DEFAULT_Q1_SOURCE, folder / "first")
            second = export_seeds(graph_b, config, 3, DEFAULT_Q1_SOURCE.parent, folder / "second")
            self.assertEqual(first["plans"], second["plans"])
            self.assertEqual(first["features"], second["features"])
            self.assertEqual(first["block_candidates"], second["block_candidates"])
            self.assertEqual(first["provenance"]["input_sha256"], second["provenance"]["input_sha256"])
            provenance = first["provenance"]
            self.assertFalse(provenance["baseline_used"])
            self.assertEqual(provenance["q1_scoring_calls"], 0)
            self.assertTrue(provenance["process_isolated"])
            self.assertTrue(provenance["q1_source_files"])
            self.assertEqual(len(provenance["q1_source_sha256"]), 64)
            plan = first["plans"][0]["plan"]
            self.assertEqual(set(plan), {"node_to_subgraph", "core_schedules"})
            self.assertEqual(set(plan["node_to_subgraph"]), {"2", "3"})
            self.assertEqual(len(plan["core_schedules"]), 3)
            self.assertEqual(first["plans"][0]["score_source"], "unscored")
            self.assertNotIn("makespan", first)
            self.assertEqual(provenance["source_api"],
                             "scheduling.registry.resolve_problem(q1) / ProblemDefinition")
            self.assertEqual(provenance['api_mode'], 'problem_definition_v1')
            self.assertIn('src/scheduling/contracts.py', provenance['q1_source_files'])
            with self.assertRaises(FileExistsError):
                export_seeds(graph_a, config, 3, DEFAULT_Q1_SOURCE, folder / "first")
        self.assertIs(sys.modules["q1"], original_q1)
        self.assertEqual(q1.__file__, original_file)
        self.assertEqual(sys.path, original_path)
        self.assertEqual(before, {name: module for name, module in sys.modules.items()
                                 if name == "q1" or name.startswith("q1.")})

    def test_invalid_configuration_fails_before_spawning(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("q3v3.q1_compat.run_command", side_effect=AssertionError("unexpected worker")):
                with self.assertRaises(ValueError):
                    export_seeds("missing.json", "missing.txt", 1, DEFAULT_Q1_SOURCE, Path(temporary))
                with self.assertRaises(ValueError):
                    export_seeds("missing.json", "missing.txt", 3, Path(temporary), Path(temporary))
                with self.assertRaises(ValueError):
                    export_seeds("missing.json", "missing.txt", 3, DEFAULT_Q1_SOURCE,
                                 Path(temporary), timeout=float("inf"))


def provider_fixture(folder, *, score_in_seed=False):
    """A refactored provider with no q1/model.py or q1/unified/seed.py at all."""
    (folder/'src/scheduling').mkdir(parents=True)
    (folder/'src/scheduling/__init__.py').write_text('',encoding='utf-8')
    (folder/'official/code').mkdir(parents=True)
    (folder/'official/code/fixture.py').write_text('# Fixture has no official imports\n',encoding='utf-8')
    code = '''from types import SimpleNamespace as NS
def make_model(raw):
    ids = sorted(o['id'] for o in raw['ops'] if o['op'] not in ('COPY_IN','COPY_OUT'))
    return NS(order=ids, ops={v:None for v in ids})
def renamed_score(*args):
    return {'makespan':123456}
def seed(model, cores, settings):
    SCORE_LINE
    plan = {'node_to_subgraph':{str(v):i for i,v in enumerate(model.order)},
            'core_schedules':[[i for i in range(len(model.order)) if i%cores==c] for c in range(cores)]}
    return plan, {'method':'public_contract_fixture'}
def resolve_problem(name):
    assert name == 'q1'
    return NS(id='q1',version='fixture-v2',supported_cores=(2,3,4,5),
        execution=NS(read_settings=lambda path: {}, settings_dict=lambda s:s,
            create_model=make_model, evaluate=renamed_score,preanalyze=renamed_score),
        prediction=NS(recognize=lambda model,s:{'operations':len(model.ops)}),
        proposals=NS(seed=seed),semantics=NS(validate=lambda *args:None))
'''.replace('SCORE_LINE', 'renamed_score(model)' if score_in_seed else 'pass')
    (folder/'src/scheduling/registry.py').write_text(code,encoding='utf-8')
    return folder


class RefactorBoundaryTests(unittest.TestCase):
    def inputs(self, folder):
        graph=folder/'graph.json'
        graph.write_text(json.dumps(small_graph()),encoding='utf-8')
        return graph, ROOT/'official/data/config.txt'

    def test_public_provider_survives_private_module_removal_and_live_edits(self):
        with tempfile.TemporaryDirectory() as name:
            folder=Path(name)
            live=provider_fixture(folder/'live')
            graph,config=self.inputs(folder)
            captured=snapshot_source(live,folder/'snapshot')
            # The user can rename/break the live Q1 entry after capture.
            (live/'src/scheduling/registry.py').write_text('raise RuntimeError("live refactor")\n',encoding='utf-8')
            exported=export_seeds(graph,config,2,Path(captured['snapshot_source_root']),folder/'export')
            self.assertEqual(exported['provenance']['q1_source_sha256'],captured['source_sha256'])
            self.assertEqual(exported['provenance']['api_mode'],'problem_definition_v1')
            self.assertEqual(exported['provenance']['backend_version'],'fixture-v2')
            self.assertIsNone(exported['provenance']['q1_module'])
            self.assertEqual(exported['features'],{'operations':2})
            self.assertEqual(set(exported['plans'][0]['plan']['node_to_subgraph']),{'2','3'})
            with self.assertRaisesRegex(RuntimeError,'live refactor'):
                export_seeds(graph,config,2,live,folder/'broken_export')

    def test_bound_score_alias_is_forbidden_even_without_legacy_official_module(self):
        with tempfile.TemporaryDirectory() as name:
            folder=Path(name)
            live=provider_fixture(folder/'live',score_in_seed=True)
            graph,config=self.inputs(folder)
            with self.assertRaisesRegex(RuntimeError,'forbids baseline'):
                export_seeds(graph,config,2,live,folder/'export')

    def test_q3_runtime_graph_and_official_import_with_q1_blocked(self):
        program = '''import sys, importlib.abc
sys.path.insert(0,sys.argv[1])
class NoQ1(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='q1' or fullname.startswith('q1.'):
            raise AssertionError('Q3 runtime imported Q1: '+fullname)
sys.meta_path.insert(0,NoQ1())
from q3v3.state import Graph, PlanState
from q3v3.official import evaluate, read_settings
from q3v3.search import solve_one
raw={'ops':[{'id':1,'op':'ADD','pipe':'PIPE_V','cycles':9}], 'tensors':[], 'edges':[]}
plan={'node_to_subgraph':{'1':0},'core_schedules':[[0],[]]}
state=PlanState(Graph(raw),plan)
result=evaluate(raw,plan,read_settings(sys.argv[2]))
assert result['makespan']==9
assert not any(k=='q1' or k.startswith('q1.') for k in sys.modules)
'''
        completed=subprocess.run([sys.executable,'-I','-B','-c',program,str(ROOT/'src'),str(ROOT/'official/data/config.txt')],
            capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)


if __name__ == "__main__":
    unittest.main()
