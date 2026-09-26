"""Q2 current-state contracts, honest evidence and semantic cache regression."""
from dataclasses import asdict, replace
import copy
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from test_q2_repairs import graph_fixture, SETTINGS, ROOT
from q2 import pipeline
from q2.affinity import component_block_plan, component_core_map
from q2.construct import construct_candidate, BudgetExpired
from q2.contracts import BlockState, CandidateOrigin, ScheduleState, ScoreRecord, LegalityRecord, BaselineResult
from q2.encoding import encode_atomic, encode_candidate, tail_order, realized_blocks
from q2.state import build_state, derive_state, validate_state, candidate_from_state
from q2.preparation import PreparationCache, PreparationService
from q2.evaluation import ExecutionValidator, evaluate_approximate, binding, exact_score, ScoredState, reconcile
from q2.snapshots import write_search_snapshot, load_search_snapshot, import_baseline_v2
from q2.candidates import CandidateEvaluator
from q2.graph import Graph
from q2.official import read_json, objective, digest, file_hash, normalized


def renamed(plan):
    result = copy.deepcopy(plan)
    result['node_to_subgraph'] = {o:j+100 for o,j in plan['node_to_subgraph'].items()}
    result['core_schedules'] = [[j+100 for j in q] for q in plan['core_schedules']]
    return result


def legal(state):
    return LegalityRecord(**binding(state), level='execution', status='VALID')


def estimate(state, time, added=None):
    return ScoreRecord(**binding(state), makespan=time, added_copy_bytes=added,
                       source='approximate', exact=False, evaluator_version='test-only-v1')


class StateTests(unittest.TestCase):
    def test_migration_uses_current_map_despite_old_draft(self):
        g,plan = graph_fixture(0,2)
        old = build_state(g,plan); before = asdict(old)
        core = component_core_map(g,2)
        new_plan = encode_atomic(g,2,core,tail_order(g))
        new = derive_state(g,old,new_plan)
        self.assertEqual(new.core_of_op,core); self.assertNotEqual(old.core_of_op,new.core_of_op)
        self.assertEqual(asdict(old),before)
        self.assertEqual(new.parent_signature,old.state_signature)
        draft = construct_candidate(g,2,'global',1)
        # Attach deliberately obsolete constructor ownership to a current state.
        draft = replace(draft,operation_core=old.core_of_op)
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d); path=directory/'input.json'; path.write_text(json.dumps(g.raw))
            service=CandidateEvaluator(g,path,directory,pipeline.Budget(pipeline.load_options()['runtime']),[],[],pipeline.run_official,'test')
            value,row=service.evaluate(candidate_from_state(new,draft=draft))
            self.assertIsNotNone(value)
            self.assertEqual(row['base_copy_bytes'],g.base_traffic(core)[1])
            self.assertEqual(row['split_components'],0)
            self.assertEqual(row['state_signature'],new.state_signature)

    def test_stale_map_order_and_plan_are_rejected(self):
        g,plan=graph_fixture(0,2);state=build_state(g,plan)
        with self.assertRaises(ValueError):validate_state(g,replace(state,core_of_op=(1,)*len(g.ids)))
        with self.assertRaises(ValueError):validate_state(g,replace(state,plan_json=json.dumps(renamed(plan))))
        with self.assertRaises(ValueError):validate_state(g,replace(state,compute_orders_by_core=tuple(reversed(state.compute_orders_by_core))))

    def test_renumber_origin_and_order_signatures(self):
        g,plan=graph_fixture(0,2);a=build_state(g,plan)
        b=build_state(g,renamed(plan),origin=CandidateOrigin('other','other',4,'external'))
        self.assertNotEqual(a.plan_digest,b.plan_digest)
        self.assertEqual(a.state_signature,b.state_signature)
        self.assertEqual(a.execution_digest,b.execution_digest)
        q=copy.deepcopy(plan);q['core_schedules'][0]=list(reversed(q['core_schedules'][0][:2]))+q['core_schedules'][0][2:]
        c=build_state(g,q)
        self.assertNotEqual(a.state_signature,c.state_signature)
        self.assertNotEqual(a.execution_digest,c.execution_digest)

    def test_same_execution_distinct_blockings_empty_core_and_roundtrip(self):
        g,plan=graph_fixture(0,2)
        a=build_state(g,plan)
        b=build_state(g,plan,blocks_by_core=((tuple(plan['core_schedules'][0]),),()))
        self.assertEqual(a.execution_digest,b.execution_digest)
        self.assertNotEqual(a.state_signature,b.state_signature)
        self.assertEqual(b.realized_blocks_by_core[1],())
        self.assertEqual(len(b.realized_blocks_by_core[0][0].sgids),len(g.ids))
        self.assertEqual(ScheduleState.from_record(json.loads(json.dumps(asdict(b)))),b)
        view=b.plan;view['core_schedules'][0].clear();self.assertNotEqual(view,b.plan)
        self.assertEqual(derive_state(g,a,plan,blocks_by_core=b.realized_blocks_by_core).state_signature,b.state_signature)

    def test_blocks_cannot_split_sgid_or_confuse_dense_indices(self):
        g,_=graph_fixture(1,2);core=component_core_map(g,2)
        plan=component_block_plan(g,2,core);state=build_state(g,plan)
        blocks=[list(row) for row in state.realized_blocks_by_core]
        b=blocks[0][0]
        blocks[0][0]=replace(b,ops=tuple(g.ids[v] for v in b.ops))
        with self.assertRaises(ValueError):build_state(g,plan,blocks_by_core=blocks)
        with self.assertRaises(ValueError):build_state(g,plan,blocks_by_core=(((),),state.realized_blocks_by_core[1]))

    def test_origin_seeded_blocks_match_20_for_all_sizes(self):
        g,_=graph_fixture(2,3);draft=construct_candidate(g,2,'global',1)
        for encoding in ('legacy','commit','tail'):
            candidate=encode_candidate(g,2,draft,encoding)
            bundle=g.settings.prefix(g.raw,candidate.plan)
            for maximum in (1,2,32,64):
                old=realized_blocks(g,candidate.plan,bundle,draft,maximum)
                new=build_state(g,candidate.plan,construction_blocks_ops=draft.construction_blocks_ops,max_operations=maximum)
                self.assertEqual(old,{k:tuple(b.sgids for b in row) for k,row in enumerate(new.realized_blocks_by_core)})


class CacheTests(unittest.TestCase):
    def test_prefix_profile_reuse_and_isolation(self):
        g,plan=graph_fixture(1,2);service=PreparationService(g)
        with patch.object(type(g.settings),'prefix',autospec=True,side_effect=type(g.settings).prefix) as spy:
            a=service.prefix(plan);service.profiles(a)
            b=service.prefix(plan);service.profiles(b)
            self.assertEqual(spy.call_count,1)
        b['cores'][0]['seq'].clear()
        self.assertEqual(service.prefix(plan),a)
        self.assertEqual(service.cache.stats['profile_builds'],2)
        self.assertEqual(service.cache.stats['profile_hits'],2)

    def test_namespace_invalidation_and_bounded_eviction(self):
        cache=PreparationCache(max_bytes=1000000,limits={'prefix':1,'profile':1,'legality':1,'exact':2})
        g,plan=graph_fixture(0,1);a=PreparationService(g,cache);a.prefix(plan)
        a.prefix(renamed(plan));self.assertEqual(cache.stats['prefix_evictions'],1)
        altered=Graph(copy.deepcopy(g.raw),replace(g.settings,delay=g.settings.delay+1))
        b=PreparationService(altered,cache);self.assertNotEqual(a.namespace,b.namespace);b.prefix(plan)
        self.assertEqual(cache.stats['prefix_builds'],3)
        cache.invalidate();self.assertEqual(cache.bytes,0)
        small=PreparationCache(max_bytes=1);small.put('prefix','x',{'huge':list(range(100))})
        self.assertIsNone(small.get('prefix','x'));self.assertLessEqual(small.bytes,1)

    def test_mutated_hardware_cannot_reuse_old_prefix(self):
        g,plan=graph_fixture(0,1)
        g=Graph(g.raw,replace(g.settings,capacity=dict(g.settings.capacity)))
        service=PreparationService(g);before=service.prepare(plan)
        g.settings.capacity['UB']-=1
        after=service.prepare(plan)
        self.assertNotEqual(before.environment_digest,after.environment_digest)
        self.assertEqual(service.cache.stats['prefix_builds'],2)
        self.assertEqual(service.cache.stats['namespace_invalidations'],1)

    def test_full_mapping_iteration_order_is_not_erased_from_cache_key(self):
        g,plan=graph_fixture(0,2);other=copy.deepcopy(plan)
        other['node_to_subgraph']=dict(reversed(list(plan['node_to_subgraph'].items())))
        service=PreparationService(g);a=service.prepare(plan);b=service.prepare(other)
        self.assertNotEqual(a.plan_digest,b.plan_digest)
        self.assertEqual(service.cache.stats['prefix_builds'],2)

    def test_equivalent_plan_reuses_only_numeric_result_and_materializes_own_labels(self):
        g,plan=graph_fixture(0,1);a=build_state(g,plan);b=build_state(g,renamed(plan))
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);path=directory/'input.json';path.write_text(json.dumps(g.raw))
            service=CandidateEvaluator(g,path,directory,pipeline.Budget(pipeline.load_options()['runtime']),[],[],pipeline.run_official,'test')
            value,first=service.evaluate(candidate_from_state(a))
            other,row=service.evaluate(candidate_from_state(b))
            self.assertEqual(value,other);self.assertEqual(len(service.calls),1)
            self.assertEqual(row['evaluation_status'],'EQUIVALENT_OFFICIAL')
            self.assertEqual(row['score']['source'],'equivalent_official');self.assertIsNone(row['result_path'])
            service.evaluate(candidate_from_state(b));self.assertEqual(len(service.calls),1)
            result=service.materialize_result(candidate_from_state(b),row)
            self.assertEqual(objective(result),value);self.assertEqual(len(service.calls),2)
            self.assertEqual(normalized(result),normalized(g.settings.evaluate(g.raw,b.plan)))
            self.assertEqual(service.preparation.cache.stats['prefix_builds'],2)

    def test_cached_prefix_does_not_accept_wrong_fidelity(self):
        g,plan=graph_fixture(0,2);draft=construct_candidate(g,2,'global',1)
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);path=directory/'input.json';path.write_text(json.dumps(g.raw))
            service=CandidateEvaluator(g,path,directory,pipeline.Budget(pipeline.load_options()['runtime']),[],[],pipeline.run_official,'test')
            candidate=encode_candidate(g,2,draft,'commit');service.evaluate(candidate)
            with self.assertRaises(AssertionError):service.evaluate(replace(candidate,intended_order=tuple(reversed(draft.commit_order))))
            self.assertEqual(service.preparation.cache.stats['prefix_builds'],1)
            self.assertEqual(len(service.calls),1)


class EvidenceTests(unittest.TestCase):
    def test_execution_check_matches_official_and_never_simulates(self):
        for seed in range(4):
            g,_=graph_fixture(seed,2)
            for core in ((0,)*len(g.ids),component_core_map(g,2)):
                plan=encode_atomic(g,2,core,tail_order(g));state=build_state(g,plan)
                with patch('multicore_cut_evaluate_problem_2.evaluate_scene_b',side_effect=AssertionError('global simulation')):
                    value=ExecutionValidator(g).validate(state)
                self.assertEqual(value.status,'VALID')
                self.assertIsInstance(g.settings.evaluate(g.raw,plan)['makespan'],int)

    def test_step2_infeasibility_is_invalid(self):
        g,_=graph_fixture(0,1);g=Graph(g.raw,replace(g.settings,capacity={'L1':524288,'UB':1}))
        state=build_state(g,encode_atomic(g,2,(0,)*len(g.ids),tail_order(g)))
        self.assertEqual(ExecutionValidator(g).validate(state).status,'INVALID')
        with self.assertRaises(RuntimeError):g.settings.evaluate(g.raw,state.plan)

    def test_unknown_is_not_invalid_or_accepted_or_cached(self):
        g,plan=graph_fixture(0,1);state=build_state(g,plan);validator=ExecutionValidator(g)
        for error in (BudgetExpired(),TimeoutError(),MemoryError()):
            def check():raise error
            record=validator.validate(state,check=check)
            self.assertEqual(record.status,'UNKNOWN')
            with self.assertRaises(ValueError):evaluate_approximate(state,record,lambda s:estimate(s,1))
        self.assertEqual(validator.validate(state).status,'VALID')
        self.assertEqual(validator.preparation.cache.stats['execution_validations'],1)

    def test_bounded_validation_reaps_child(self):
        g,plan=graph_fixture(0,1);state=build_state(g,plan)
        before={p.pid for p in multiprocessing.active_children()}
        record=ExecutionValidator(g).validate(state,timeout_seconds=.000001)
        self.assertEqual(record.status,'UNKNOWN')
        self.assertEqual({p.pid for p in multiprocessing.active_children()},before)
        self.assertEqual(ExecutionValidator(g).validate(state,timeout_seconds=10).status,'VALID')

    def test_approximate_calibration_dual_incumbents_and_unknown_bytes(self):
        g,plan=graph_fixture(0,2);a=build_state(g,plan)
        other=copy.deepcopy(plan);other['core_schedules'].reverse()
        b=build_state(g,other)
        approx=ScoredState(a,evaluate_approximate(a,legal(a),lambda s:estimate(s,50)),legal(a))
        exact=ScoredState(b,exact_score(b,(100,5)),legal(b))
        result=reconcile([approx,exact]);self.assertEqual(result.search,approx);self.assertEqual(result.exact,exact)
        corrected=ScoredState(a,exact_score(a,(120,4)),legal(a))
        result=reconcile([approx,exact,corrected,approx])
        self.assertEqual(result.search.state,b);self.assertEqual(result.exact.state,b)
        self.assertTrue(all(row.score.exact for row in result.candidates))
        x=ScoredState(a,estimate(a,100,None),legal(a));y=ScoredState(b,estimate(b,100,0),legal(b))
        self.assertEqual(reconcile([x,y]).search,x)
        self.assertEqual(reconcile([y,x]).search,y)

    def test_distinct_blocks_survive_reconcile_and_exact_rebinds(self):
        g,plan=graph_fixture(0,2);a=build_state(g,plan)
        b=build_state(g,plan,blocks_by_core=((tuple(plan['core_schedules'][0]),),()))
        result=reconcile([ScoredState(a,exact_score(a,(100,0)),legal(a)),ScoredState(b,estimate(b,1),legal(b))])
        self.assertEqual(len(result.candidates),2)
        self.assertTrue(all(row.score.makespan==100 and row.score.exact for row in result.candidates))

    def test_reconcile_deduplicates_label_only_states_without_losing_exact_evidence(self):
        g,plan=graph_fixture(0,1);a=build_state(g,plan);b=build_state(g,renamed(plan))
        result=reconcile([ScoredState(a,estimate(a,1),legal(a)),ScoredState(b,exact_score(b,(100,0)),legal(b))])
        self.assertEqual(len(result.candidates),1)
        self.assertEqual(result.search.state,a)
        self.assertEqual(result.search.score.makespan,100)
        self.assertEqual(result.search.score.source,'equivalent_official')
        self.assertEqual(result.search.score.evidence_plan_digest,b.plan_digest)

    def test_estimator_cannot_claim_official_or_reuse_foreign_evidence(self):
        g,plan=graph_fixture(0,1);state=build_state(g,plan);other=build_state(g,renamed(plan))
        with self.assertRaises(ValueError):evaluate_approximate(state,legal(state),lambda s:exact_score(s,(1,0)))
        with self.assertRaises(ValueError):evaluate_approximate(state,legal(other),lambda s:estimate(s,1))
        with self.assertRaises(ValueError):replace(estimate(state,1),exact=True)
        with self.assertRaises(ValueError):BaselineResult(state,estimate(state,1),legal(state),official_json='{}')

    def test_search_snapshot_cannot_resume_as_formal_output(self):
        g,plan=graph_fixture(0,1);state=build_state(g,plan)
        incumbents=reconcile([ScoredState(state,estimate(state,15),legal(state))])
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'search'
            self.assertEqual(write_search_snapshot(target,incumbents),'SUCCESS_ESTIMATED')
            self.assertEqual(load_search_snapshot(target,g),incumbents)
            self.assertFalse(pipeline.reusable(target,None));self.assertFalse((target/'complete.json').exists())
            with self.assertRaises(FileExistsError):write_search_snapshot(target,incumbents)
            (target/'search_state.json').write_text('{}')
            with self.assertRaises(ValueError):load_search_snapshot(target,g)

    def test_unknown_serialized_versions_fail(self):
        g,plan=graph_fixture(0,1);state=build_state(g,plan)
        record=asdict(state);record['schema_version']=100
        with self.assertRaises(ValueError):ScheduleState.from_record(record)
        result=BaselineResult(state,estimate(state,1),legal(state))
        value=asdict(result);value['schema_version']=1
        with self.assertRaises(ValueError):BaselineResult.from_record(value)


class MigrationTests(unittest.TestCase):
    def unpack(self, directory):
        with zipfile.ZipFile(ROOT/'tests/fixtures/q2_interfaces/v20_minimal_publication.zip') as archive:
            for name in archive.namelist():self.assertEqual(Path(name).name,name)
            archive.extractall(directory)

    def test_verified_20_import_is_read_only_and_not_resume(self):
        path=ROOT/'tests/fixtures/q2/minimal.json';g=Graph(read_json(path),SETTINGS)
        with tempfile.TemporaryDirectory() as d:
            target=Path(d);self.unpack(target)
            before={p.name:file_hash(p) for p in target.iterdir()}
            result=import_baseline_v2(target,g,path)
            self.assertEqual(result.score.makespan,6)
            self.assertEqual(result.plan,read_json(target/'plan.json'))
            self.assertEqual(validate_state(g,result.state),result.state)
            self.assertEqual(before,{p.name:file_hash(p) for p in target.iterdir()})
            self.assertFalse(pipeline.reusable(target,digest(pipeline.identity(path,2,SETTINGS,pipeline.load_options(),None))))
            with self.assertRaises(ValueError):pipeline.load_baseline_result(target)
            with self.assertRaises(ValueError):import_baseline_v2(target,g,ROOT/'tests/fixtures/q2/cross_fanout.json')

    def test_import_rejects_tampering_and_unknown_version(self):
        path=ROOT/'tests/fixtures/q2/minimal.json';g=Graph(read_json(path),SETTINGS)
        with tempfile.TemporaryDirectory() as d:
            target=Path(d);self.unpack(target)
            (target/'baseline_state.json').write_text('{}')
            with self.assertRaises(ValueError):import_baseline_v2(target,g,path)
        with tempfile.TemporaryDirectory() as d:
            target=Path(d);self.unpack(target)
            provenance=read_json(target/'provenance.json')
            provenance['identity']['algorithm']['algorithm_version']='unknown'
            provenance['fingerprint']=digest(provenance['identity'])
            (target/'provenance.json').write_text(json.dumps(provenance),encoding='utf-8')
            run=read_json(target/'run.json');run['fingerprint']=provenance['fingerprint']
            (target/'run.json').write_text(json.dumps(run),encoding='utf-8')
            complete=read_json(target/'complete.json');complete['fingerprint']=provenance['fingerprint']
            for name in ('provenance.json','run.json'):complete['artifacts_sha256'][name]=file_hash(target/name)
            (target/'complete.json').write_text(json.dumps(complete),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'only Q2 2.0'):import_baseline_v2(target,g,path)


class BehaviorPreservationTests(unittest.TestCase):
    def test_completed_search_matches_frozen_20_plans_and_beta_trajectory(self):
        fixture=read_json(ROOT/'tests/fixtures/q2_interfaces/v20_behavior.json')
        options=pipeline.load_options()
        options['runtime'].update(wall_budget_seconds=60,final_verification_reserve_seconds=10)
        for expected in fixture['rows']:
            path=ROOT/f"tests/fixtures/q2/{expected['case']}.json"
            self.assertEqual(file_hash(path),expected['input_sha256'])
            with tempfile.TemporaryDirectory() as d:
                actual=pipeline.solve(path,fixture['cores'],d,options)
                self.assertEqual(actual['status'],'SUCCESS_VERIFIED');self.assertTrue(actual['search_complete'])
                plan=read_json(Path(d)/expected['case']/f"N{fixture['cores']}"/'plan.json')
                self.assertEqual(plan,expected['plan'])
                self.assertEqual(list(actual['final_objective']),expected['objective'])
                self.assertEqual(actual['data_movement_bytes'],expected['movement'])
                self.assertEqual(actual['evaluated_candidates'],expected['candidate_requests'])
                beta=[{k:r[k] for k in ('stream','beta','objective','relation','stop_reason')} for r in actual['search']['records']]
                self.assertEqual(json.loads(json.dumps(beta)),expected['beta'])


if __name__=='__main__':unittest.main()
