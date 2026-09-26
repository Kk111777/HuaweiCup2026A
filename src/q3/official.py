"""Shared Q2 preparation, unchanged official Q3 events, independent CLI replay."""
from dataclasses import dataclass, replace
import copy
import json
from pathlib import Path
import sys
import types
import pickle

from .artifacts import file_hash, write_json
from q2.official import ROOT, Settings as SceneSettings, digest, read_json
from q2.preparation import PreparationCache, PreparationService
from q2.state import build_state, plan_digest
from q2.evaluation import binding, ScoredState
from q2.contracts import LegalityRecord, ScoreRecord
from .bounds import lower_bounds, cannot_improve


@dataclass(frozen=True)
class Settings:
    scene: SceneSettings
    cache_capacity: int
    cache_bandwidth: int | float
    namespace: str

    @classmethod
    def read(cls, reference=ROOT / 'official', config=None):
        scene = SceneSettings.read(reference, config)
        from multicore_cut_evaluate_problem_3 import read_cache_config
        cache = read_cache_config(scene.config)
        namespace = digest(dict(problem=3, official=scene.hashes, cache=cache,
                                fifo='official_completion_insert', adapter='Q3-Reproduction-1.0'))
        return cls(scene, cache['cache_capacity_bytes'], cache['cache_bandwidth_bytes_per_cycle'], namespace)


def objective(result):
    return result['makespan'], result['data_movement_bytes']['added_copy_bytes']


def evaluate_fresh(raw, plan, settings, problem=3):
    s = settings.scene
    if problem == 2:
        return s.evaluate(raw, plan)
    from multicore_cut_evaluate_problem_3 import evaluate_problem_3
    return evaluate_problem_3(raw, plan, s.bandwidth, s.capacity, s.delay,
                              settings.cache_capacity, settings.cache_bandwidth)


@dataclass
class Prepared:
    scene_state: object
    score_state: object
    legality: object
    prefix: dict
    bundle: tuple
    lower_bound: int
    lower_parts: dict
    cross_delay: int


class Evaluator:
    def __init__(self, raw, settings, cache_bytes=64 * 1024 * 1024):
        from q2.graph import Graph
        self.raw, self.settings = raw, settings
        self.graph = Graph(raw, settings.scene)
        cache = PreparationCache(cache_bytes, dict(prefix=8, profile=64, legality=64,
                                                   exact=32, prepared_core=16, execution=4))
        self.service = PreparationService(self.graph, cache)
        self.global_calls = 0

    def prepare(self, plan):
        from schedule_step2 import step2_spill_insertion, _build_extended_graph
        from schedule_step3 import prepare_step3_execution
        from evaluation_validation import validate_execution
        prefix = self.service.prefix(plan)
        # Reconstruct bounded contiguous blocks from the current submitted plan.
        # No old Q2 construction/search state is imported. Large atomic plans
        # must not reduce an eight-block capacity window to eight operations.
        blocks_by_core = []
        for item in prefix['cores'].values():
            counts = {sg: 0 for sg in item['subgraph_order']}
            for op in item['seq']:
                if op in self.graph.dense:
                    counts[item['op_subgraph'][op]] += 1
            rows, active, count = [], [], 0
            limit = 32 if len(counts) > 32 else 1
            for sg in item['subgraph_order']:
                if active and count + counts[sg] > limit:
                    rows.append(tuple(active))
                    active, count = [], 0
                active.append(sg)
                count += counts[sg]
            if active:
                rows.append(tuple(active))
            blocks_by_core.append(tuple(rows))
        state = build_state(self.graph, plan, preparation=self.service, blocks_by_core=tuple(blocks_by_core))
        key = (self.settings.namespace, state.execution_digest, state.plan_digest)
        bundle = self.service.cache.get('execution', key)
        if bundle is None:
            tasks, spill_bytes = {}, 0
            for core, item in prefix['cores'].items():
                local_key = (state.environment_digest, digest(dict(graph=item['graph'], seq=item['seq'])))
                local = self.service.cache.get('prepared_core', local_key)
                if local is None:
                    step2 = step2_spill_insertion(item['graph'], item['seq'], capacity=self.settings.scene.capacity) if item['seq'] else dict(
                        new_ops=[], new_tensors=[], new_edges=[], spill_records=[], seq_ext=[])
                    expanded = _build_extended_graph(item['graph'], step2)
                    task = prepare_step3_execution(expanded, capacity=self.settings.scene.capacity,
                                                   bandwidth=self.settings.scene.bandwidth)
                    local = task, step2.get('spill_records', [])
                    self.service.cache.put('prepared_core', local_key, local)
                    self.service.cache.stats['core_builds'] += 1
                task, spills = local
                labels = dict(item['op_subgraph'])
                for row in spills:
                    following = labels.get(row.get('next_use_op'))
                    previous = labels.get(row.get('prev_use_op'))
                    if row['spill_out_id'] is not None:
                        labels[row['spill_out_id']] = previous if previous is not None else following
                    labels[row['spill_in_id']] = following
                    spill_bytes += row['size'] * (1 + int(row['spill_out_copies_data']))
                task.update(task_id=core, core_id=core, subgraph_ids=item['subgraph_order'], op_subgraph=labels)
                tasks[core] = task
            validate_execution(tasks, prefix['cross_links'])
            original, base = prefix['original_copy_bytes'], prefix['base_copy_bytes']
            traffic = dict(original_graph_copy_bytes=original, scheduled_copy_bytes=base + spill_bytes,
                           added_copy_bytes=base + spill_bytes - original,
                           partition_added_copy_bytes=base - original, spill_added_copy_bytes=spill_bytes)
            bundle = tasks, prefix['cross_links'], prefix['cross_task_traffic'], traffic, prefix['plan_view']
            self.service.cache.put('execution', key, bundle)
        environment = digest((state.environment_digest, self.settings.namespace))
        score_state = replace(state, environment_digest=environment,
                              state_signature=digest((environment, state.state_signature)),
                              execution_digest=digest((environment, state.execution_digest)))
        legality = LegalityRecord(**binding(score_state), level='execution', status='VALID',
                                  validator_version='Q3-SceneB-1.0')
        lower, parts = lower_bounds(bundle, self.settings)
        return Prepared(state, score_state, legality, prefix, bundle, lower, parts, self.settings.scene.delay)

    def evaluate(self, plan, incumbent=None, use_bounds=True):
        prepared = self.prepare(plan)
        added = prepared.bundle[3]['added_copy_bytes']
        if use_bounds and cannot_improve(prepared.lower_bound, added, incumbent):
            return None, prepared, 'LOWER_BOUND'
        key = (self.settings.namespace, prepared.score_state.execution_digest, plan_digest(plan))
        result = self.service.cache.get('exact', key)
        status = 'CACHED_OFFICIAL' if result is not None else 'OFFICIAL'
        if result is None:
            # Bind a private function namespace, never patch the official module.
            # This uses its exact code and event order on independent prepared data.
            from multicore_cut_evaluate_problem_3 import evaluate_problem_3
            namespace = dict(evaluate_problem_3.__globals__)
            snapshot = pickle.dumps(prepared.bundle, protocol=5)
            namespace['_build_scene_b_tasks'] = lambda *args: pickle.loads(snapshot)
            evaluate = types.FunctionType(evaluate_problem_3.__code__, namespace,
                                          evaluate_problem_3.__name__, evaluate_problem_3.__defaults__)
            s = self.settings.scene
            result = evaluate(self.raw, plan, s.bandwidth, s.capacity, s.delay,
                              self.settings.cache_capacity, self.settings.cache_bandwidth)
            self.global_calls += 1
            if prepared.lower_bound > result['makespan']:
                raise AssertionError('unsafe lower bound')
            self.service.cache.put('exact', key, result)
        score = ScoreRecord(**binding(prepared.score_state), makespan=result['makespan'],
                            added_copy_bytes=added, source='official', exact=True,
                            evaluator_version=self.settings.namespace,
                            evidence_plan_digest=prepared.score_state.plan_digest)
        ScoredState(prepared.score_state, score, prepared.legality)
        return result, prepared, status


def replay(graph, plan, settings, directory, timeout, problem=3):
    from .runtime import run_command
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result_path, trace_path, log_path = (directory / name for name in ('result.json', 'trace.json', 'log.txt'))
    command = [sys.executable, str(settings.scene.root / 'code' / f'multicore_cut_evaluate_problem_{problem}.py'),
               str(Path(graph).resolve()), str(Path(plan).resolve()), '--config', str(settings.scene.config),
               '-o', str(result_path), '--trace-output', str(trace_path), '--log-output', str(log_path)]
    run = run_command(command, timeout=timeout, text=True, encoding='utf-8', errors='replace')
    if run.returncode:
        raise RuntimeError(f'official Q{problem} replay failed: {run.stderr[-2500:]}')
    result = read_json(result_path)
    trace = read_json(trace_path)
    events = [e for e in trace['traceEvents'] if e.get('ph') == 'X' and 'op_id' in e.get('args', {})]
    # Official Trace metadata has other duration events; compare actual operation count.
    expected_ops = sum(len(c['ops']) for c in result['per_core_timeline'])
    if not trace.get('traceEvents') or not log_path.is_file():
        raise AssertionError('missing official replay artifacts')
    if len(events) != expected_ops or max((e['ts'] + e['dur'] for e in events), default=0) != result['makespan']:
        raise AssertionError('Trace disagrees with result')
    times = {(c['core_id'], o['op_id']): (o['start'], o['duration'])
             for c in result['per_core_timeline'] for o in c['ops']}
    for event in events:
        if times[event['args']['core_id'], event['args']['op_id']] != (event['ts'], event['dur']):
            raise AssertionError('Trace operation time disagrees with result')
    write_json(directory / 'artifacts.json', {p.name: file_hash(p) for p in (result_path, trace_path, log_path)})
    return result
