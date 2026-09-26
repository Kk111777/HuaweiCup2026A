"""Production repair components checked against exhaustive and official oracles."""
from pathlib import Path
import copy,itertools,json,os,random,sys,unittest
ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'tests/fixtures/q2_repair'
sys.path.insert(0, str(ROOT/'src'))
from q2.official import Settings, digest as stable_hash, objective
SETTINGS=Settings.read()
from q2.beta import PlateauBeta
from q2.construct import construct_candidate as recorded_construct, State, try_place, commit, close
from q2.encoding import encode_atomic, assert_compute_fidelity, tail_order
from q2.affinity import component_core_map, component_block_plan
from q2.capacity import profile, CapacityWindow, minimum_disruption, apply_window
from q2.graph import Graph
from multicore_cut_evaluate_problem_2 import evaluate_scene_b
from schedule_step2 import step2_spill_insertion

def prefix_for(g, plan):
    return g.settings.prefix(g.raw, plan)

COUNTS={'random_graphs':0,'permutations_checked':0,'feasible_orders':0,'sequence_rechecks':0}


def graph_fixture(seed=0,chains=2,shared=True):
    rng=random.Random(seed);tensors=[];ops=[];edges=[];nextid=1
    def tensor(size,pos):
        nonlocal nextid
        i=nextid;nextid+=1;tensors.append(dict(id=i,size=size,pos=pos));return i
    def op(name,inputs,outputs,cycles,pipe='PIPE_V'):
        nonlocal nextid
        i=nextid;nextid+=1;ops.append(dict(id=i,op=name,pipe=pipe,cycles=cycles))
        edges.extend(dict(source=t,target=i) for t in inputs)
        edges.extend(dict(source=i,target=t) for t in outputs)
        return i
    common=None
    if shared:
        a=tensor(120,'DDR');common=tensor(120,'UB');op('COPY_IN',[a],[common],2,'PIPE_MTE2')
    heads=[];tails=[]
    for _ in range(chains):
        a=tensor(120,'DDR');inp=tensor(120,'UB');op('COPY_IN',[a],[inp],2,'PIPE_MTE2')
        mid=tensor(rng.choice([35000,40000,50000]),'UB')
        out=tensor(rng.choice([50000,60000,65000]),'UB');ddr=tensor(tensors[-1]['size'],'DDR')
        heads.append(op('ADD',[inp]+([common] if shared else []),[mid],100))
        tails.append(op('ADD',[mid],[out],100))
        op('COPY_OUT',[out],[ddr],100,'PIPE_MTE3')
    raw=dict(ops=ops,tensors=tensors,edges=edges)
    g=Graph(raw,SETTINGS);order=tuple(g.dense[o] for o in heads+tails)
    plan=encode_atomic(g,2,(0,)*len(g.ids),order)
    return g,plan


class BetaTests(unittest.TestCase):
    def run_values(self,values):
        b=PlateauBeta(['x'],len(values));seen=[]
        while (x:=b.next()):
            s,k=x;seen.append(k);b.observe(s,k,values[k-1])
        return b,seen
    def test_equal_continues_strict_worse_stops(self):
        b,seen=self.run_values([(100,1),(100,99),(90,1000),(95,0),(70,0)])
        self.assertEqual(seen,[1,2,3,4]);self.assertEqual(b.best['x'],(90,1000))
        self.assertEqual(b.stops['x'],'STRICT_TIME_INCREASE')
    def test_plateau_reaches_cap(self):
        b,seen=self.run_values([(100,1)]*4);self.assertEqual(seen,[1,2,3,4])
    def test_cached_equal_must_continue(self):
        b=PlateauBeta(['x']);s,k=b.next();b.observe(s,k,(6,0));s,k=b.next()
        b.observe(s,k,(6,0),'CACHED_OFFICIAL');self.assertEqual(b.next(),('x',3))
    def test_separate_streams(self):
        b=PlateauBeta(['a','b'],3)
        while (pair:=b.next()):
            s,k=pair;b.observe(s,k,((10+k) if s=='a' else 100-k,0))
        self.assertEqual([r['beta'] for r in b.records if r['stream']=='a'],[1,2])
        self.assertEqual([r['beta'] for r in b.records if r['stream']=='b'],[1,2,3])
    def test_invalid_is_not_numeric_worse(self):
        b=PlateauBeta(['x'],4)
        s,k=b.next();b.observe(s,k,(10,0));s,k=b.next();b.observe(s,k,None,'INVALID')
        s,k=b.next();r=b.observe(s,k,(20,0));self.assertEqual(r['relation'],'first')
        self.assertEqual(b.next(),('x',4))
    def test_timeout_incomplete(self):
        b=PlateauBeta(['x']);s,k=b.next();r=b.observe(s,k,None,'TIMEOUT')
        self.assertEqual(r['stop_reason'],'INCOMPLETE_EVALUATION');self.assertIsNone(b.next())
    def test_stale_feedback_rejected(self):
        b=PlateauBeta(['x']);b.next()
        with self.assertRaises(ValueError):b.observe('x',2,(10,0))


class EncodingTests(unittest.TestCase):
    def test_no_trial_side_effect_and_close_retains_state(self):
        g,_=graph_fixture();s=State(g,2);v=min(s.ready);old=copy.deepcopy(s.__dict__)
        t=try_place(g,s,v,0);self.assertEqual(old,s.__dict__)
        commit(g,s,t,seed=True,limit=10000)
        before={k:copy.deepcopy(s.__dict__[k]) for k in ['clocks','live','work','tensor_ready','peaks']}
        close(s)
        for k,v in before.items():self.assertEqual(v,s.__dict__[k])
    def test_recorded_constructor_and_fidelity(self):
        g,_=graph_fixture(3,3)
        for mode in ['global','component_grouped']:
            draft=recorded_construct(g,3,mode,2)
            plan=encode_atomic(g,3,draft.operation_core,draft.commit_order)
            bundle=prefix_for(g,plan)
            assert_compute_fidelity(g,bundle,draft.operation_core,draft.commit_order)
            self.assertEqual(sum(map(len,draft.search_blocks)),len(g.ids))
    def test_components_no_cross_computation_transfers(self):
        g,_=graph_fixture(1,3,shared=True);core=component_core_map(g,3)
        plan=component_block_plan(g,3,core);bundle=prefix_for(g,plan)
        self.assertFalse(bundle['cross_links'])
        self.assertEqual(bundle['base_copy_bytes'],g.base_traffic(core)[1])
    def test_atomic_bad_order_rejected(self):
        g,_=graph_fixture()
        with self.assertRaises(ValueError):encode_atomic(g,2,(0,)*len(g.ids),tuple(reversed(g.order)))
    def test_draft_plan_is_independent(self):
        g,_=graph_fixture();draft=recorded_construct(g,2,'global',1)
        plan=draft.legacy_plan;plan['core_schedules'][0].clear()
        self.assertNotEqual(plan,draft.legacy_plan)

    def test_encoding_rejects_duplicate_nodes(self):
        g,_=graph_fixture()
        with self.assertRaises(ValueError):encode_atomic(g,2,(0,)*len(g.ids),(0,)*len(g.ids))


class WindowTests(unittest.TestCase):
    def test_exact_vs_exhaustive_and_official_sequence(self):
        for seed in range(12):
            g,plan=graph_fixture(seed,2+(seed%2),shared=bool(seed%3))
            bundle=prefix_for(g,plan);blocks=[(sg,) for sg in plan['core_schedules'][0]]
            w=CapacityWindow(bundle,0,blocks,SETTINGS.capacity);ans=minimum_disruption(w)
            feasible=[];COUNTS['random_graphs']+=1
            for order in itertools.permutations(range(w.m)):
                mask=0;legal=True
                for j in order:
                    if w.pred[j]&~mask:legal=False;break
                    mask|=1<<j
                if not legal:continue
                COUNTS['permutations_checked']+=1
                peaks,seq=w.scan(order)
                p=copy.deepcopy(plan);p['core_schedules'][0]=[blocks[j][0] for j in order]
                true=prefix_for(g,p)
                self.assertEqual(seq,true['cores'][0]['seq']);COUNTS['sequence_rechecks']+=1
                actual=profile(true['cores'][0]['graph'],seq,SETTINGS.capacity)
                self.assertEqual(tuple(actual['peaks']),peaks)
                step2=step2_spill_insertion(true['cores'][0]['graph'],seq,SETTINGS.capacity)
                self.assertEqual(not step2['spill_records'],actual['no_spill_certificate'])
                if all(a<=b for a,b in zip(peaks,w.caps)):
                    inv=sum(order[i]>order[j] for i in range(w.m) for j in range(i+1,w.m))
                    feasible.append((inv,order));COUNTS['feasible_orders']+=1
            if feasible:
                self.assertEqual((ans['inversions'],tuple(ans['block_order'])),min(feasible))
                apply_window(g,plan,bundle,w,ans,prefix_for)
            else:self.assertEqual(ans['status'],'NO_FEASIBLE_ORDER_IN_FIXED_WINDOW')
    def test_feasible_original_zero_disruption(self):
        g,p=graph_fixture(5,2);p['core_schedules'][0]=[0,2,1,3]
        b=prefix_for(g,p);w=CapacityWindow(b,0,[(j,) for j in p['core_schedules'][0]],SETTINGS.capacity)
        a=minimum_disruption(w);self.assertEqual(a['inversions'],0)
        self.assertEqual(a['subgraph_order'],p['core_schedules'][0])
    def test_fixed_multisg_blocks_preserve_internal_order(self):
        g,p=graph_fixture(5,3)
        # Chunk two heads together; exact state remains valid with mobile shared input.
        b=prefix_for(g,p);w=CapacityWindow(b,0,[(0,1),(2,),(3,4),(5,)],SETTINGS.capacity)
        for order in itertools.permutations(range(w.m)):
            try:_,seq=w.scan(order)
            except ValueError:continue
            q=copy.deepcopy(p);q['core_schedules'][0]=[sg for j in order for sg in w.blocks[j]]
            self.assertEqual(prefix_for(g,q)['cores'][0]['seq'],seq)
    def test_domain_infeasible_not_global_invalid(self):
        g,p=graph_fixture(0,3);b=prefix_for(g,p)
        # One fixed block cannot change an overflowing sequence; spills still allow official execution.
        w=CapacityWindow(b,0,[tuple(p['core_schedules'][0])],SETTINGS.capacity)
        ans=minimum_disruption(w);self.assertEqual(ans['status'],'NO_FEASIBLE_ORDER_IN_FIXED_WINDOW')
        r=evaluate_scene_b(g.raw,p,bandwidth=SETTINGS.bandwidth,capacity=SETTINGS.capacity,cross_core_copy_delay=SETTINGS.delay)
        self.assertGreater(r['makespan'],0)


class OfficialControlTests(unittest.TestCase):
    def test_minimal_six_cycles(self):
        raw=json.loads((FIX/'minimal.json').read_text())
        p=json.loads((FIX/'minimal_multicore_res.json').read_text())
        r=evaluate_scene_b(raw,p,bandwidth=SETTINGS.bandwidth,capacity=SETTINGS.capacity,cross_core_copy_delay=SETTINGS.delay)
        self.assertEqual(objective(r),(6,0));self.assertEqual(r['data_movement_bytes']['scheduled_copy_bytes'],32)

    def test_capacity_repair_official_control(self):
        g=Graph(json.loads((FIX/'memory_interleaved.json').read_text()),SETTINGS)
        p=json.loads((FIX/'memory_interleaved_multicore_res.json').read_text())
        b=prefix_for(g,p);w=CapacityWindow(b,0,[(j,) for j in p['core_schedules'][0]],SETTINGS.capacity)
        ans=minimum_disruption(w)
        self.assertEqual(ans['inversions'],1)
        q,_=apply_window(g,p,b,w,ans,prefix_for)
        old=evaluate_scene_b(g.raw,p,bandwidth=SETTINGS.bandwidth,capacity=SETTINGS.capacity,cross_core_copy_delay=SETTINGS.delay)
        new=evaluate_scene_b(g.raw,q,bandwidth=SETTINGS.bandwidth,capacity=SETTINGS.capacity,cross_core_copy_delay=SETTINGS.delay)
        # Pinned handoff fixture and its archived JSON results use 3736/2302.
        self.assertEqual(old['makespan'],3736);self.assertEqual(new['makespan'],2302)
        self.assertEqual(old['data_movement_bytes']['spill_added_copy_bytes'],80000)
        self.assertEqual(new['data_movement_bytes']['spill_added_copy_bytes'],0)
        COUNTS['control']={'before':old['makespan'],'after':new['makespan'],'before_spill':80000,'after_spill':0,'inversions':ans['inversions']}

    def test_cross_core_path_preserved_in_window(self):
        old,p0=graph_fixture(0,2,False);raw=copy.deepcopy(old.raw)
        for t in raw['tensors']:t['size']=120
        # Computation order is head0, head1, tail0, tail1.
        ord_ids=[int(v) for v,_ in sorted(p0['node_to_subgraph'].items(),key=lambda a:a[1])]
        tail0,tail1=ord_ids[2:]
        out_tid=next(e['target'] for e in raw['edges'] if e['source']==tail0)
        raw['edges'].append({'source':out_tid,'target':tail1})
        g=Graph(raw,SETTINGS);order=tuple(g.dense[v] for v in ord_ids)
        core=[0]*len(g.ids);core[g.dense[tail0]]=1
        p=encode_atomic(g,2,core,order);b=prefix_for(g,p)
        groups=p['core_schedules'][0];w=CapacityWindow(b,0,[(j,) for j in groups],SETTINGS.capacity)
        self.assertTrue(w.pred[-1]&1)  # path head0 -> remote tail0 -> local tail1
        with self.assertRaises(ValueError):w.scan((2,0,1))


def tearDownModule():
    target=os.environ.get('Q2_TEST_COUNTS_PATH')
    if target:Path(target).write_text(json.dumps(COUNTS,indent=2))


if __name__=='__main__':unittest.main()
