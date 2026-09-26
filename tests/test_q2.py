"""Q2 独立小图 oracle 与实际官方接口验收；不启动全量数据。"""
from pathlib import Path
import copy
import itertools
import json
import random
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from q2.official import Settings, Q2Error, read_json, signature
SETTINGS = Settings.read()
from q2.graph import Graph
from q2.construct import State, try_place, commit, close, construct, BudgetExpired
from q2.memory import profile
from q2.window import Window, solve_window, apply_order, precedence

FIX = ROOT/'tests/fixtures/q2'


def fixture(name):
    return read_json(FIX/(name+'.json')), read_json(FIX/(name+'_multicore_res.json'))


def random_graph(seed, n=5, size=16):
    rng = random.Random(seed); nodes = itertools.count(101, 7)
    raw = dict(ops=[], tensors=[], edges=[])
    def tensor(pos, size):
        t = next(nodes); raw['tensors'].append(dict(id=t, pos=pos, size=size)); return t
    def op(name, pipe, ins, outs, cycles):
        v = next(nodes); raw['ops'].append(dict(id=v, op=name, pipe=pipe, cycles=cycles))
        raw['edges'].extend(dict(source=t, target=v) for t in ins)
        raw['edges'].extend(dict(source=v, target=t) for t in outs)
        return v
    d = tensor('DDR', size); shared = tensor('UB', size)
    op('COPY_IN', 'PIPE_MTE2', [d], [shared], 1)
    outputs = []
    for i in range(n):
        ins = [shared]+[t for t in outputs if rng.random() < .27]
        out = tensor('UB', size)
        op('ADD', 'PIPE_V' if rng.random() < .6 else 'PIPE_M', ins, [out], rng.randrange(0, 12))
        outputs.append(out)
    for out in outputs:
        if rng.random() < .4 or not any(e['source'] == out for e in raw['edges']):
            d = tensor('DDR', size); op('COPY_OUT', 'PIPE_MTE3', [out], [d], 1)
    return raw


def slow_trial(g, s, v, k):
    """测试专用：全状态 deepcopy，直接按规范推进，再整图重算虚拟驻留。"""
    r = copy.deepcopy(s); oldq, oldbytes = r.copy_work, r.copy_bytes
    for ti in g.inputs[v]:
        t = g.tensors[ti]
        if (ti, k) not in r.tensor_ready:
            if t.producer is None:
                r.clocks[k][2] += t.q; copies = 1
            else:
                src = r.core[t.producer]
                r.clocks[src][3] = max(r.clocks[src][3], r.tensor_ready[ti, src])+t.q
                r.clocks[k][2] = max(r.clocks[k][2], r.clocks[src][3]+g.settings.delay)+t.q
                copies = 2
            r.tensor_ready[ti, k] = r.clocks[k][2]
            r.copy_work += copies*t.q; r.copy_bytes += copies*t.size
    end = max(r.clocks[k][g.pipe[v]], max((r.tensor_ready[t, k] for t in g.inputs[v]), default=0))+g.duration[v]
    r.clocks[k][g.pipe[v]] = end
    for ti in g.outputs[v]:
        t = g.tensors[ti]; r.tensor_ready[ti, k] = end
        if t.final:
            r.clocks[k][3] = max(r.clocks[k][3], end)+t.q
            r.copy_work += t.q; r.copy_bytes += t.size
    kept = {i for i in range(len(g.tensors)) if r.remaining_consumers[i] and r.holders[i] & (1 << k)}
    for p in (0, 1):
        transient = sum(g.tensors[t].size for t in kept | set(g.touched[v]) if g.tensors[t].pos == p)
        r.peaks[k][p] = max(r.peaks[k][p], transient)
    for t in g.touched[v]: r.holders[t] |= 1 << k
    for t in g.inputs[v]: r.remaining_consumers[t] -= 1; r.consumer_mask[t] |= 1 << k
    for h in range(len(r.live)):
        for p in (0, 1):
            r.live[h][p] = sum(t.size for i, t in enumerate(g.tensors) if t.pos == p and
                                r.remaining_consumers[i] and r.holders[i] & (1 << h))
    r.tail = max(r.tail, end+g.b[v]-g.duration[v])
    pressure = 2*sum((max(0, r.peaks[h][p]-g.settings.capacity[pos])+g.settings.bandwidth-1)//g.settings.bandwidth
                     for h in range(len(r.peaks)) for p, pos in enumerate(('L1', 'UB')))
    r.score = max(max(map(max, r.clocks)), r.copy_work, r.tail)+pressure
    return r, end, r.copy_work-oldq, r.copy_bytes-oldbytes


class OfficialTests(unittest.TestCase):
    def test_fixtures_prefix_and_complete(self):
        from multicore_cut_evaluate_problem_2 import _build_scene_b_tasks
        from schedule_step2 import step2_spill_insertion, _build_extended_graph
        from schedule_step3 import prepare_step3_execution
        for name, expected in [('minimal', 6), ('cross_fanout', 514), ('memory_interleaved', 3736), ('memory_grouped', 2302)]:
            with self.subTest(name=name):
                raw, plan = fixture(name); prefix = SETTINGS.prefix(raw, plan)
                tasks, links, _, traffic, view = _build_scene_b_tasks(raw, plan, SETTINGS.bandwidth, SETTINGS.capacity)
                self.assertEqual(prefix['cross_links'], links); self.assertEqual(prefix['plan_view'], view)
                spill = 0
                for k, c in prefix['cores'].items():
                    p = profile(c['graph'], c['seq'], SETTINGS.capacity)
                    r2 = step2_spill_insertion(c['graph'], c['seq'], SETTINGS.capacity) if c['seq'] else dict(new_ops=[], new_tensors=[], new_edges=[], spill_records=[], seq_ext=[])
                    self.assertEqual(p.no_spill, not bool(r2['spill_records']))
                    r3 = prepare_step3_execution(_build_extended_graph(c['graph'], r2), SETTINGS.capacity, SETTINGS.bandwidth)
                    for field in ('graph', 'seq', 'pipe_ops'): self.assertEqual(r3[field], tasks[k][field])
                    spill += sum(s['size']*(1+int(s['spill_out_copies_data'])) for s in r2['spill_records'])
                self.assertEqual(prefix['base_copy_bytes']+spill, traffic['scheduled_copy_bytes'])
                self.assertEqual(SETTINGS.evaluate(raw, plan)['makespan'], expected)

    def test_bad_json_and_plan(self):
        from stub_multicore_cut_and_schedule import derive_multicore_plan
        raw, plan = fixture('minimal')
        bad = [dict(plan, extra=0), dict(plan, node_to_subgraph={}),
               dict(plan, core_schedules=[[0], [0]]), dict(plan, node_to_subgraph={'11': True}),
               dict(plan, node_to_subgraph={'11': -1}), dict(plan, node_to_subgraph={'011': 0, '11': 0})]
        for p in bad:
            with self.assertRaises((ValueError, RuntimeError)): derive_multicore_plan(raw, p)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'有 空格.json'; p.write_text('{"x":1,"x":2}')
            with self.assertRaises(Q2Error): read_json(p)

    def test_reference_path_mix_and_hash(self):
        with tempfile.TemporaryDirectory() as d:
            import shutil
            dest = Path(d)/'official'; shutil.copytree(ROOT/'official/code', dest/'code')
            (dest/'data').mkdir(); shutil.copyfile(ROOT/'official/data/config.txt', dest/'data/config.txt')
            with self.assertRaises(Q2Error): Settings.read(dest)
            (dest/'code/schedule_step1.py').write_text('# changed')
            with self.assertRaises(Q2Error): Settings.read(dest)


class ConstructionTests(unittest.TestCase):
    def test_fast_slow_trials_purity_and_commit(self):
        for seed in range(12):
            g = Graph(random_graph(seed, 7, size=0 if seed == 0 else 40000), SETTINGS)
            s = State(g, 3); rng = random.Random(seed)
            while s.ready:
                v = min(s.ready); before = copy.deepcopy(s.__dict__)
                fast = [try_place(g, s, v, k) for k in range(3)]
                self.assertEqual(before, s.__dict__)
                self.assertEqual(fast, list(reversed([try_place(g, s, v, k) for k in reversed(range(3))])))
                k = rng.randrange(3); t = fast[k]; reference, end, work, size = slow_trial(g, s, v, k)
                self.assertEqual((t.score, t.end, t.added_work, t.added_bytes), (reference.score, end, work, size))
                close(s); commit(g, s, t, seed=True, limit=1)
                for name in ('clocks', 'live', 'peaks', 'tensor_ready', 'remaining_consumers', 'consumer_mask', 'holders', 'copy_work', 'copy_bytes', 'score', 'tail'):
                    self.assertEqual(getattr(s, name), getattr(reference, name), name)
                with self.assertRaises(ValueError): commit(g, s, t)
            self.assertEqual(g.base_traffic(s.core), (s.copy_work, s.copy_bytes))
            self.assertEqual(SETTINGS.prefix(g.raw, s.plan(g))['base_copy_bytes'], s.copy_bytes)

    def test_construction_deterministic_and_no_expensive_calls(self):
        from unittest.mock import patch
        for seed in range(5):
            g = Graph(random_graph(seed, 7), SETTINGS)
            for n in (1, 2, 5):
                for mode in ('global', 'component_grouped'):
                    with patch('multicore_cut_evaluate_problem_2._build_scene_b_tasks', side_effect=AssertionError('expensive')):
                        a, info = construct(g, n, mode, 2); b, _ = construct(g, n, mode, 2)
                    self.assertEqual(a, b); self.assertEqual(len(a['core_schedules']), n)
                    pref = SETTINGS.prefix(g.raw, a)
                    self.assertEqual(pref['base_copy_bytes'], info['base_copy_bytes'])
                    self.assertLessEqual(info['shortlist_max'], 32)

    def test_traffic_all_core_maps(self):
        raw, _ = fixture('cross_fanout'); g = Graph(raw, SETTINGS)
        from multicore_cut_evaluate_problem_1 import _copy_traffic_bytes
        from schedule_step3 import _build_graph_views, _op_duration
        for cores in itertools.product(range(3), repeat=len(g.ids)):
            plan = dict(node_to_subgraph={str(v): i for i, v in enumerate(g.ids)},
                        core_schedules=[[i for i in g.order if cores[i] == k] for k in range(3)])
            pref = SETTINGS.prefix(raw, plan); work = size = 0
            for c in pref['cores'].values():
                graph = c['graph']; ts = {t['id']: t for t in graph['tensors']}
                ins, outs, _, _ = _build_graph_views(graph['ops'], graph['edges'])
                work += sum(_op_duration(o, ins, outs, ts, SETTINGS.bandwidth) for o in graph['ops'] if o['op'] in ('COPY_IN', 'COPY_OUT'))
                size += _copy_traffic_bytes(graph)
            self.assertEqual(g.base_traffic(cores), (work, size))

    def test_close_preserves_core_state(self):
        g = Graph(random_graph(11), SETTINGS); s = State(g, 2)
        v = min(s.ready); commit(g, s, try_place(g, s, v, 0), seed=True, limit=1)
        before = copy.deepcopy(s.__dict__); close(s)
        for k in before:
            if k not in ('current', 'current_core', 'subgraph_work', 'limit', 'close_records'): self.assertEqual(before[k], getattr(s, k))

    def test_unsupported_and_empty(self):
        raw = dict(ops=[], tensors=[], edges=[]); g = Graph(raw, SETTINGS)
        plan, _ = construct(g, 5, 'global', 1)
        self.assertEqual(SETTINGS.evaluate(raw, plan)['makespan'], 0)
        raw = random_graph(0); raw['ops'][0]['pipe'] = 'PIPE_M'
        # COPY pipe semantics comes from official fields; malformed copy endpoints are rejected.
        raw['tensors'][0]['size'] += 1
        with self.assertRaises(Q2Error): Graph(raw, SETTINGS)


class WindowTests(unittest.TestCase):
    def test_memory_control_dp_finds_improvement(self):
        raw, plan = fixture('memory_interleaved'); bundle = SETTINGS.prefix(raw, plan); bundle['_raw'] = raw
        w = Window(bundle, 0, 0, 4, SETTINGS.capacity); r = solve_window(w)
        self.assertEqual(r['peak_before'], 4*140000); self.assertEqual(r['peak_after'], 4*100000)
        improved, _ = apply_order(plan, bundle, w, r, SETTINGS)
        self.assertEqual(SETTINGS.evaluate(raw, improved)['makespan'], 2302)

    def test_dp_vs_independent_official_permutations(self):
        from stub_multicore_cut_and_schedule import MulticoreCutError
        from multicore_cut_evaluate_problem_2 import SceneBEvaluationError
        for seed in range(8):
            raw = random_graph(seed, 5, size=0 if seed == 0 else 9000)
            g = Graph(raw, SETTINGS)
            plan = dict(node_to_subgraph={str(g.ids[v]): i for i, v in enumerate(g.order)}, core_schedules=[list(range(5)), []])
            pref = SETTINGS.prefix(raw, plan)
            for left, right in ((0, 5), (1, 4)):
                w = Window(pref, 0, left, right, SETTINGS.capacity); r = solve_window(w)
                best = w.before
                for perm in itertools.permutations(range(w.m)):
                    mask = 0; legal = True
                    for j in perm:
                        if w.pred[j] & ~mask: legal = False; break
                        mask |= 1 << j
                    if not legal: continue
                    p = copy.deepcopy(plan); p['core_schedules'][0][left:right] = [w.original[j] for j in perm]
                    c = SETTINGS.prefix(raw, p)['cores'][0]
                    # 独立区间 oracle 由原版重建 COPY 桶；比较窗口覆盖的位置及入口 live。
                    q = profile(c['graph'], c['seq'], SETTINGS.capacity)
                    indices = [i for i, o in enumerate(c['seq']) if c['op_subgraph'][o] in w.original]
                    value = max([0]+[max(q.curve[0][i], 4*q.curve[1][i]) for i in indices])
                    self.assertEqual(w.scan_order(perm)[0], value)
                    best = min(best, value)
                self.assertEqual(r['peak_after'], best)

    def test_external_paths_and_data_order_overlap(self):
        view = dict(subgraph_ids=[0, 1, 2, 3], core_orders={0: [0, 3], 1: [1, 2]},
                    dependency_pairs=[(0, 1), (2, 3), (0, 3)])
        self.assertEqual(precedence(view, 0, 0, 2), (0, 1))
        view['dependency_pairs'].remove((0, 3))
        self.assertEqual(precedence(view, 0, 0, 2), (0, 1))

    def test_dp_timeout_and_no_expensive_calls(self):
        from unittest.mock import patch
        raw, plan = fixture('memory_interleaved'); w = Window(SETTINGS.prefix(raw, plan), 0, 0, 4, SETTINGS.capacity)
        def expired(): raise BudgetExpired()
        self.assertEqual(solve_window(w, expired)['status'], 'WINDOW_INCOMPLETE')
        with patch('multicore_cut_evaluate_problem_2._build_scene_b_tasks', side_effect=AssertionError()):
            self.assertTrue(solve_window(w)['completed'])

    def test_closed_interval_simultaneous_input_output(self):
        raw, plan = fixture('minimal'); c = SETTINGS.prefix(raw, plan)['cores'][0]
        self.assertEqual(profile(c['graph'], c['seq'], SETTINGS.capacity).peaks, (0, 32))

    def test_random_memory_certificate_against_step2(self):
        from schedule_step2 import step2_spill_insertion, Step2SchedulingError
        for seed in range(24):
            raw = random_graph(seed, 5, size=10000 if seed % 2 else 40000); g = Graph(raw, SETTINGS)
            plan = dict(node_to_subgraph={str(v): i for i, v in enumerate(g.ids)}, core_schedules=[list(g.order)])
            c = SETTINGS.prefix(raw, plan)['cores'][0]; p = profile(c['graph'], c['seq'], SETTINGS.capacity)
            try: spills = step2_spill_insertion(c['graph'], c['seq'], SETTINGS.capacity)['spill_records']
            except Step2SchedulingError:
                self.assertFalse(p.no_spill)
            else: self.assertEqual(p.no_spill, not bool(spills))

    def test_same_mask_histories_and_equal_retains_original(self):
        raw, plan = fixture('memory_interleaved'); w = Window(SETTINGS.prefix(raw, plan), 0, 0, 4, SETTINGS.capacity)
        # 第一、第二桶无依赖，共享输入 COPY 在第一位消费桶动态执行。
        self.assertEqual(w.counts(3), tuple(a+b+c for a,b,c in zip(w.pre,
                         [w.fixed_counts[0].get(t,0)+w.fixed_counts[1].get(t,0) for t in range(len(w.meta))],
                         [sum(t in w.touches[o] for o,m in w.mobile if m & 3) for t in range(len(w.meta))])))
        raw, plan = fixture('memory_grouped'); w = Window(SETTINGS.prefix(raw, plan), 0, 0, 4, SETTINGS.capacity)
        self.assertEqual(solve_window(w)['proposed_order'], list(w.original))

    def test_six_bucket_oracle_and_eight_bucket_dp_size(self):
        for n in (6, 8):
            raw = random_graph(17, n, size=128); g = Graph(raw, SETTINGS)
            plan = dict(node_to_subgraph={str(g.ids[v]): i for i,v in enumerate(g.order)}, core_schedules=[list(range(n))])
            pref = SETTINGS.prefix(raw,plan); w = Window(pref,0,0,n,SETTINGS.capacity); r = solve_window(w)
            self.assertLessEqual(r['states'], 1 << n); self.assertLessEqual(r['transitions'], n*(1 << (n-1)))
            if n == 6:
                best = w.before
                for order in itertools.permutations(range(n)):
                    try: w.scan_order(order)
                    except ValueError: continue
                    p = copy.deepcopy(plan); p['core_schedules'][0] = list(order)
                    c = SETTINGS.prefix(raw,p)['cores'][0]
                    best = min(best, profile(c['graph'],c['seq'],SETTINGS.capacity).scaled)
                self.assertEqual(r['peak_after'],best)


class PipelineTests(unittest.TestCase):
    def options(self):
        from q2.pipeline import load_options
        o = load_options(ROOT/'configs/q2/legacy_1_1.json'); o['runtime'].update(wall_budget_seconds=30, final_verification_reserve_seconds=5)
        return o

    def test_official_acceptance_rules(self):
        from q2.pipeline import select_final
        def r(t,b): return dict(makespan=t, data_movement_bytes=dict(added_copy_bytes=b))
        self.assertEqual(select_final(r(10,10),r(11,0)), 'B0')
        self.assertEqual(select_final(r(10,10),r(10,9)), 'B1')
        self.assertEqual(select_final(r(10,10),r(10,10)), 'B0')
        self.assertEqual(select_final(r(10,10),None), 'B0')

    def test_real_enhancement_pipeline_and_three_calls(self):
        from unittest.mock import patch
        from q2 import pipeline
        raw, plan = fixture('memory_interleaved'); g = Graph(raw,SETTINGS)
        work, size = g.base_traffic([0]*len(g.ids))
        o = self.options(); o['construction'].update(scope_modes=['global'],betas=[1])
        info = dict(mode='global',beta=1,proxy_J=0,base_copy_work=work,base_copy_bytes=size)
        with tempfile.TemporaryDirectory() as d, patch.object(pipeline,'construct',return_value=(plan,info)):
            result = pipeline.solve(FIX/'memory_interleaved.json',2,d,o)
            self.assertEqual(result['selected'],'B1'); self.assertEqual(result['final_objective'][0],2302)
            self.assertEqual(result['official_evaluations'],3)

    def test_batch_workers_and_failure_isolation(self):
        from q2.pipeline import batch
        with tempfile.TemporaryDirectory() as d:
            raw, _ = fixture('minimal'); a=Path(d)/'a.json'; b=Path(d)/'b.json'
            a.write_text(json.dumps(raw)); b.write_text('{"ops":[],"tensors":[]}')
            r = batch([a,b],[2,5],Path(d)/'out',self.options(),ROOT/'official',None,workers=2)
            self.assertFalse(r['complete']); self.assertEqual(r['successful'],2)
            self.assertTrue((Path(d)/'out/a/N2/complete.json').exists())
            self.assertFalse((Path(d)/'out/b/N2/complete.json').exists())

    def test_solve_replay_resume_tamper_and_changed_identity(self):
        from q2.pipeline import solve, reusable
        with tempfile.TemporaryDirectory(prefix='q2 中文 空格') as d:
            a = solve(FIX/'minimal.json', 2, d, self.options())
            self.assertEqual(a['status'], 'SUCCESS_VERIFIED'); self.assertEqual(a['final_objective'], (6,0))
            self.assertEqual(a['official_evaluations'], 2)
            p = Path(d)/'minimal/N2'
            self.assertTrue(reusable(p,a['fingerprint']))
            b = solve(FIX/'minimal.json', 2, d, self.options(), resume=True)
            self.assertTrue(b['resumed'])
            other = self.options(); other['enhancement']['max_windows'] = 1
            with self.assertRaises(Q2Error): solve(FIX/'minimal.json', 2, d, other, resume=True)
            (p/'trace.json').write_text('tampered')
            self.assertFalse(reusable(p,a['fingerprint']))

    def test_failed_first_candidate_and_fallback(self):
        from unittest.mock import patch
        from q2 import pipeline
        real = pipeline.run_official
        def failed_candidates(graph, plan, settings, directory, timeout, calls, phase):
            if phase == 'B0':
                calls.append(dict(phase=phase, plan_hash='test-failure', launched=False, status='OFFICIAL_ERROR', wall_seconds=0))
                return None
            return real(graph, plan, settings, directory, timeout, calls, phase)
        with tempfile.TemporaryDirectory() as d, patch.object(pipeline, 'run_official', side_effect=failed_candidates):
            r = pipeline.solve(FIX/'minimal.json', 5, d, self.options())
            self.assertEqual(r['status'], 'SUCCESS_VERIFIED')
            self.assertEqual(r['selected_candidate']['mode'], 'fallback')
            self.assertEqual([c['phase'] for c in r['official_calls']], ['B0','fallback','final_verification'])

    def test_verification_failure_never_publishes(self):
        from unittest.mock import patch
        from q2 import pipeline
        real = pipeline.run_official
        def fail_final(graph, plan, settings, directory, timeout, calls, phase):
            if phase == 'final_verification':
                calls.append(dict(phase=phase, status='EVAL_TIMEOUT', launched=False, wall_seconds=0))
                return None
            return real(graph, plan, settings, directory, timeout, calls, phase)
        with tempfile.TemporaryDirectory() as d, patch.object(pipeline, 'run_official', side_effect=fail_final):
            r = pipeline.solve(FIX/'minimal.json', 2, d, self.options())
            self.assertEqual(r['status'], 'EVAL_TIMEOUT')
            self.assertFalse((Path(d)/'minimal/N2/complete.json').exists())
            self.assertTrue(Path(r['attempt'],'failure.json').is_file())

    def test_interrupted_construction_records_work_and_verifies_fallback(self):
        from unittest.mock import patch
        from q2 import pipeline
        real = pipeline.construct
        def interrupted(g, cores, mode, beta, count, check, trace, **kwargs):
            visits = 0
            def expire():
                nonlocal visits
                visits += 1
                if visits > 1: raise BudgetExpired()
            return real(g, cores, mode, beta, count, expire, trace, **kwargs)
        with tempfile.TemporaryDirectory() as d, patch.object(pipeline, 'construct', side_effect=interrupted):
            r = pipeline.solve(FIX/'cross_fanout.json', 2, d, self.options())
            self.assertEqual(r['status'], 'SUCCESS_VERIFIED')
            self.assertEqual(r['selected_candidate']['mode'], 'fallback')
            self.assertGreater(r['interrupted_construction']['trials'], 0)
            self.assertGreaterEqual(r['stages']['construction_seconds'], r['interrupted_construction']['wall_seconds'])
            self.assertEqual(r['stop_reason'], 'construction_budget')

    def test_real_subprocess_timeout(self):
        from q2.pipeline import run_official
        raw, plan = fixture('minimal')
        with tempfile.TemporaryDirectory() as d:
            calls = []; result = run_official(FIX/'minimal.json', plan, SETTINGS, Path(d)/'attempt', 1e-6, calls, 'test')
            self.assertIsNone(result); self.assertEqual(calls[0]['status'], 'EVAL_TIMEOUT')

    def test_resource_error_status_and_no_success_artifact(self):
        from unittest.mock import patch
        from q2 import pipeline
        with tempfile.TemporaryDirectory() as d, patch.object(pipeline, 'Graph', side_effect=MemoryError('test resource failure')):
            r = pipeline.solve(FIX/'minimal.json', 2, d, self.options())
            self.assertEqual(r['status'], 'RESOURCE_LIMIT')
            self.assertFalse((Path(d)/'minimal/N2/complete.json').exists())
            self.assertIn('MemoryError', r['traceback'])

    def test_summary_partial_and_pairing(self):
        from q2.pipeline import summarize
        with tempfile.TemporaryDirectory() as d:
            rows = [dict(case='a',num_cores=2,status='SUCCESS_VERIFIED',speedup=2,final_objective=(5,0)),
                    dict(case='a',num_cores=5,status='EVAL_TIMEOUT')]
            r = summarize(rows,[('a',2),('a',5),('b',2)],d)
            self.assertFalse(r['complete']); self.assertEqual(r['successful'],1)
            self.assertEqual(r['averages'][0]['requested_cases'],2)
            self.assertEqual(len((Path(d)/'summary.csv').read_text().splitlines()), 3)


if __name__ == '__main__': unittest.main()
