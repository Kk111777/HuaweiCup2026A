"""Differential Q1 acceptance against a frozen source archive.

Both versions run this SAME driver in separate Python processes. The algorithms,
official evaluator and proposal generators are real; only the clock is replaced.
Each official call advances a logical clock by an explicitly recorded cost, and
finite round/candidate limits bound the experiment. This tests decisions under
equal work/time inputs, not production wall-time or unrestricted final quality.

Example (from the integration checkout)::

    python tools/verify_modular_equivalence.py --output outputs/modular_equivalence
    python tools/verify_modular_equivalence.py --cases case001 case006 case056 \
        --graphs ../../official/data --output outputs/modular_real_equivalence

No solver/config/official files are modified. Artifacts include both full traces,
source hashes, input hashes, coverage and the first exact difference per scenario.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BEFORE = ROOT / 'outputs/modular_engineering_20260926/before.zip'
SYNTHETIC_CASES = ('tiny_independent', 'tiny_fork_join', 'medium_layered')


def digest(value):
    """Full JSON identity, deliberately independent of solver plan_digest."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def source_manifest(root):
    root = Path(root)
    paths = list((root / 'src').rglob('*.py')) + list((root / 'official/code').glob('*.py'))
    paths += list((root / 'configs').glob('*.json'))
    return {p.relative_to(root).as_posix(): file_digest(p) for p in sorted(paths)}


def extract_snapshot(archive, destination, expected=None):
    """Reject traversal and optionally verify the original frozen manifest."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            target = (destination / member.filename).resolve()
            if not target.is_relative_to(destination):
                raise ValueError(f'unsafe snapshot member: {member.filename}')
            if not member.is_dir():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read(member))
    if expected is not None:
        for name, wanted in expected.items():
            path = (destination / name).resolve()
            if not path.is_relative_to(destination) or not path.is_file() or file_digest(path) != wanted:
                raise ValueError(f'snapshot manifest mismatch: {name}')
    if not (destination / 'src/q1/unified/engine.py').is_file():
        raise ValueError('snapshot does not contain the Q1 compatibility entry point')
    return destination


def first_difference(before, after, path='$'):
    """Do not hide equal-score/different-plan or equal-final/different-path drift."""
    if type(before) is not type(after):
        return dict(path=path, before=before, after=after)
    if isinstance(before, dict):
        if set(before) != set(after):
            return dict(path=path + '.keys', before=sorted(before), after=sorted(after))
        for key in sorted(before):
            found = first_difference(before[key], after[key], f'{path}.{key}')
            if found:
                return found
    elif isinstance(before, list):
        if len(before) != len(after):
            return dict(path=path + '.length', before=len(before), after=len(after))
        for index, (left, right) in enumerate(zip(before, after)):
            found = first_difference(left, right, f'{path}[{index}]')
            if found:
                return found
    elif before != after:
        return dict(path=path, before=before, after=after)
    return None


def behavior_projection(result):
    """Exclude ONLY explicitly additive engineering metadata, retaining raw traces."""
    behavior = deepcopy(result['behavior'])
    for output in behavior['outputs']:
        history = output['history']
        for name in ('problem_id', 'backend_version', 'requested_params', 'effective_params'):
            history.pop(name, None)
    return behavior


def check_added_metadata(before, after):
    """Effective knobs must match the old controller's recorded structure controls."""
    controlled = ('stage_widths', 'stage_batches', 'max_region_candidates',
                  'max_structural_candidates', 'max_proposals_per_round',
                  'max_cuts_per_task', 'memory_merges')
    for old, new in zip(before['behavior']['outputs'], after['behavior']['outputs']):
        history = new['history']
        if 'requested_params' not in history:
            continue  # Also supports self-comparison of an archived implementation.
        if history['requested_params'] != before['parameters']:
            return 'requested_params differs from the identical experiment configuration'
        expected = deepcopy(before['parameters'])
        controls = old['history'].get('structure_control')
        if controls:
            expected.update({key: controls[key] for key in controlled})
        if history.get('effective_params') != expected:
            return 'effective_params differs from legacy structure-adjusted configuration'
        if history.get('problem_id') != 'q1' or not history.get('backend_version'):
            return 'missing or incorrect Q1 backend metadata'
    return None


def synthetic_graph(name):
    """Independent, fork/join and layered DAGs with actual tensor/COPY traffic."""
    if name == 'tiny_independent':
        count, links, size = 12, [], 600
    elif name == 'tiny_fork_join':
        count, size = 18, 2400
        links = [(0, n) for n in range(1, 9)] + [(n, 9) for n in range(1, 9)]
        links += [(9, n) for n in range(10, 17)] + [(n, 17) for n in range(10, 17)]
    elif name == 'medium_layered':
        count, size = 48, 7200
        links = [(a, b) for a in range(count) for b in range(count)
                 if b // 8 == a // 8 + 1 and (a % 8 == b % 8 or (a + b) % 5 == 0)]
    else:
        raise ValueError(f'unknown synthetic graph: {name}')
    raw = dict(ops=[dict(id=n, op='ADD', pipe=('PIPE_V' if n % 3 else 'PIPE_M'),
                         cycles=2000 + 137 * (n % 7)) for n in range(count)], tensors=[], edges=[])
    sequence = 1000

    def tensor(position):
        nonlocal sequence
        sequence += 1
        raw['tensors'].append(dict(id=sequence, pos=position, size=size))
        return sequence

    def copy(kind):
        nonlocal sequence
        sequence += 1
        raw['ops'].append(dict(id=sequence, op=kind,
                              pipe='PIPE_MTE2' if kind == 'COPY_IN' else 'PIPE_MTE3',
                              cycles=(size + 59) // 60))
        return sequence

    def edge(a, b):
        raw['edges'].append(dict(source=a, target=b))

    ddr, local, load = tensor('DDR'), tensor('UB'), copy('COPY_IN')
    edge(ddr, load)
    edge(load, local)
    targets = {b for _, b in links}
    for n in range(count):
        if n not in targets:
            edge(local, n)
        out = tensor('UB')
        edge(n, out)
        users = [b for a, b in links if a == n]
        for user in users:
            edge(out, user)
        if not users:
            store, final = copy('COPY_OUT'), tensor('DDR')
            edge(out, store)
            edge(store, final)
    return raw


class LogicalClock:
    def __init__(self):
        self.now = 0.

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _instrument_references(original, replacement):
    # Observe aliases captured by either the legacy module or the new adapters.
    # The original function still supplies the exact return value unchanged.
    for name, module in list(sys.modules.items()):
        if name == 'q1' or name.startswith(('q1.', 'scheduling.')):
            for key, value in list(vars(module).items()):
                if value is original:
                    setattr(module, key, replacement)


def worker(request):
    source = Path(request['source']).resolve()
    # The launcher removes caller PYTHONPATH; this is the only solver source inserted.
    sys.path.insert(0, str(source / 'src'))
    clock = LogicalClock()
    time.perf_counter = clock
    time.process_time = clock
    from q1 import official
    from q1.graph import GraphIndex
    from q1.model import Graph
    from q1.plan import plan_digest, objective
    from q1.improvement.incremental import IncrementalEvaluator
    from q1.improvement import operators
    schedule_module = importlib.import_module('q1.improvement.schedule')
    from q1.unified.budget import SearchBudget, CaseSliceBudget
    from q1.unified.construction import Construction
    from q1.unified.features import recognize, OfficialCostModel
    from q1.unified.seed import structural_plan
    from q1.unified.service import SearchServices
    from q1.unified.engine import search

    settings = official.Settings.read(request['config'])
    raw = read(request['graph'])
    graph = Graph(raw)
    params = read(source / 'configs/unified.json')
    params.update(max_rounds=request['rounds'], max_initial_evaluations_per_core=3,
                  official_per_round=3, stage_widths=[4, 8], stage_batches=[1],
                  max_region_candidates=6, max_structural_candidates=6,
                  max_proposals_per_round=12, memory_merges=24, max_cuts_per_task=3)
    params['evaluation'] = dict(params.get('evaluation', {}), mode=request['mode'],
                                approximate_batch_size=2)
    settings_dict = dict(capacity=settings.capacity, bandwidth=settings.bandwidth,
                         task_same_core_wait_cycles=settings.same_core_wait,
                         task_cross_core_wait_cycles=settings.cross_core_wait)
    graph.structural_features = recognize(graph, settings_dict)
    shared = request['scope'] == 'shared_case'
    events, outputs = [], []
    active_core = [None]

    def event(kind, **payload):
        events.append(dict(kind=kind, cores=active_core[0], **deepcopy(payload)))

    def plan_view(plan):
        return dict(plan_digest=plan_digest(plan), full_plan_sha256=digest(plan))

    for function_name in ('initial_candidates', 'local_candidates'):
        original = getattr(operators, function_name)

        def observed(*args, _original=original, _name=function_name, **kwargs):
            value = _original(*args, **kwargs)
            proposals = value[0] if _name == 'local_candidates' else value
            event('candidates', family=_name, proposals=[dict(action=n, proxy=p, **plan_view(plan))
                                                       for n, plan, p in proposals])
            return value
        _instrument_references(original, observed)

    original_schedule = schedule_module.schedule

    def observed_schedule(*args, **kwargs):
        value = original_schedule(*args, **kwargs)
        event('scheduled_proposal', **plan_view(value[0]), proxy=value[1])
        return value

    _instrument_references(original_schedule, observed_schedule)

    original_estimate, original_observe = IncrementalEvaluator.estimate, IncrementalEvaluator.observe

    def estimate(self, plan, *args, **kwargs):
        value = original_estimate(self, plan, *args, **kwargs)
        event('estimate', **plan_view(plan), cycles=value[0], detail=value[1])
        return value

    def observe(self, plan, result):
        value = original_observe(self, plan, result)
        event('calibration', **plan_view(plan), objective=list(objective(result)),
              source=result.get('evaluation_source', 'official'), samples=len(self.calibration))
        return value

    IncrementalEvaluator.estimate, IncrementalEvaluator.observe = estimate, observe

    class ObservedService(SearchServices):
        def evaluate(self, plan, timeout):
            value, record = super().evaluate(plan, timeout)
            # Same logical cost on both versions; measured wall time never feeds policy.
            cost = request['official_cost']
            clock.advance(cost)
            record.update(seconds=cost, cpu_seconds=cost)
            event('official', **plan_view(plan), result_sha256=digest(value), record=record)
            return value, record

    class ObservedConstruction(Construction):
        def next(self, *args, **kwargs):
            value = super().next(*args, **kwargs)
            event('construction', window=kwargs.get('seconds'), proposal=(None if value is None else
                  dict(action=value[0], proxy=value[2], **plan_view(value[1]))))
            return value

    parent = SearchBudget(600, started=clock()) if shared else None
    approximation = IncrementalEvaluator(graph, settings_dict, params['evaluation']) if shared else None
    cost_model = OfficialCostModel(graph) if shared else None
    index = GraphIndex(graph, settings)
    seeds = {}
    for cores in request['cores']:
        active_core[0] = cores
        plan, meta = structural_plan(graph, cores, settings)
        if request['case'] == 'tiny_independent':
            # A deliberately coarse legal fixture guarantees opportunities for improvement.
            plan = official.make_plan([graph.order], cores)
        seeds[cores] = plan
        event('seed', **plan_view(plan), plan=plan, metadata=meta)
        if shared and request['mode'] == 'adaptive':
            approximation.result(plan)  # Match case-wide seed preparation/cache warming.

    for position, cores in enumerate(request['cores']):
        active_core[0] = cores
        service = ObservedService(graph, settings,
            checkpoint=lambda p, r: event('checkpoint', **plan_view(p), objective=list(objective(r)),
                                         source=r.get('evaluation_source', 'official'), result_sha256=digest(r)),
            rejected=lambda p: event('reject', **plan_view(p)))
        service.graph_index = index
        if shared:
            service.approximation, service.cost_model = approximation, cost_model
            budget = CaseSliceBudget(parent, len(request['cores']) - position)
        else:
            budget = SearchBudget(600, started=clock())
        seed_plan = seeds[cores]
        if shared and request['mode'] == 'adaptive':
            seed_result = approximation.result(seed_plan)
        else:
            # Seed scoring is a test fixture cost, outside both compared searches.
            seed_result = official.evaluate(raw, seed_plan, settings)
        constructor = ObservedConstruction(graph, cores, settings, service)
        start = clock()
        plan, result, history = search(graph, cores, settings_dict, deepcopy(params), budget.target,
                                      service, seed=(seed_plan, seed_result), started=start,
                                      constructor=constructor, budget=budget)
        outputs.append(dict(cores=cores, plan=plan, result=result, history=history,
                            construction=constructor.records, logical_seconds=clock() - start))
    return dict(identity=dict(source=str(source), engine_module=search.__module__,
                              engine_file=importlib.import_module(search.__module__).__file__,
                              python_executable=sys.executable, python_version=platform.python_version()),
                behavior=dict(events=events, outputs=outputs),
                settings=settings_dict, parameters=params)


def run_worker(source, graph, config, scenario, *, timeout=300):
    request = dict(scenario, source=str(Path(source).resolve()), graph=str(Path(graph).resolve()),
                   config=str(Path(config).resolve()))
    with tempfile.TemporaryDirectory(prefix='q1-equivalence-worker-') as folder:
        request_path, result_path = Path(folder) / 'request.json', Path(folder) / 'result.json'
        write(request_path, request)
        # -I would ignore PYTHONHASHSEED too. Remove PYTHONPATH explicitly and use -s.
        env = dict(os.environ, PYTHONHASHSEED='0')
        env.pop('PYTHONPATH', None)
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-s', str(Path(__file__).resolve()),
                                 '--worker', str(request_path), '--worker-result', str(result_path)],
                                cwd=folder, env=env, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f'isolated worker failed ({result.returncode}):\n{result.stderr[-8000:]}')
        return read(result_path)


def coverage(results):
    events = [event for result in results for event in result['behavior']['events']]
    outputs = [out for result in results for out in result['behavior']['outputs']]
    records = [record for out in outputs for record in out['history']['records']]
    return dict(solves=len(outputs), official_records=sum(r['evaluation_source'] == 'official' for r in records),
                approximate_records=sum(r['evaluation_source'] == 'approximate' for r in records),
                approximate_acceptances=sum(r['evaluation_source'] == 'approximate' and r.get('accepted', False)
                                             for r in records),
                calibration_events=sum(e['kind'] == 'calibration' for e in events),
                proposal_batches=sum(e['kind'] == 'candidates' for e in events),
                scheduled_proposals=sum(e['kind'] == 'scheduled_proposal' for e in events),
                shared_case_solves=sum(o['history'].get('budget_scope') == 'case_slice' for o in outputs),
                final_sources=sorted({o['result'].get('evaluation_source', 'official') for o in outputs}))


def compare_suite(before_archive, after, output, *, cases=SYNTHETIC_CASES, cores=(2, 3, 4, 5),
                  modes=('official', 'adaptive'), scopes=('standalone', 'shared_case'),
                  graphs=None, config=None, rounds=1, official_cost=8., timeout=300, manifest=None):
    after, output = Path(after).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = Path(config or after / 'official/data/config.txt').resolve()
    before_manifest = read(manifest) if manifest else None
    current_manifest = source_manifest(after)
    write(output / 'after_manifest.json', current_manifest)
    summary = dict(schema_version=1, before_archive_sha256=file_digest(before_archive),
                   after_source_sha256=digest(current_manifest), config_sha256=file_digest(config),
                   driver_sha256=file_digest(__file__), python_executable=sys.executable,
                   python_version=platform.python_version(),
                   clock_policy='generation/approximation: zero; official call: fixed logical cost',
                   excluded_additive_metadata=['problem_id', 'backend_version', 'requested_params', 'effective_params'],
                   official_cost_seconds=official_cost, max_rounds=rounds,
                   scope_note='Equal bounded work experiment, not unrestricted production quality or wall-time.',
                   scenarios=[], passed=False)
    after_results = []
    with tempfile.TemporaryDirectory(prefix='q1-before-') as folder:
        before = extract_snapshot(before_archive, folder, before_manifest)
        write(output / 'before_manifest.json', source_manifest(before))
        for case in cases:
            graph_path = output / 'inputs' / f'{case}.json'
            if case in SYNTHETIC_CASES:
                write(graph_path, synthetic_graph(case))
            else:
                number = int(case.removeprefix('case').replace('_', ''))
                candidate = Path(graphs or after / 'official/data') / f'case_{number:03d}.json'
                write(graph_path, read(candidate))
            for mode in modes:
                for scope in scopes:
                    name = f'{case}_{mode}_{scope}'
                    scenario = dict(case=case, cores=list(cores), mode=mode, scope=scope,
                                    rounds=rounds, official_cost=official_cost)
                    row = dict(name=name, input_sha256=file_digest(graph_path), **scenario)
                    try:
                        old = run_worker(before, graph_path, config, scenario, timeout=timeout)
                        new = run_worker(after, graph_path, config, scenario, timeout=timeout)
                        write(output / name / 'before.json', old)
                        write(output / name / 'after.json', new)
                        old_behavior, new_behavior = behavior_projection(old), behavior_projection(new)
                        difference = first_difference(old_behavior, new_behavior)
                        metadata_error = check_added_metadata(old, new)
                        row.update(passed=difference is None, first_difference=difference,
                                   before_behavior_sha256=digest(old_behavior),
                                   after_behavior_sha256=digest(new_behavior), coverage=coverage([new]),
                                   metadata_error=metadata_error)
                        row['passed'] = row['passed'] and metadata_error is None
                        after_results.append(new)
                    except (RuntimeError, subprocess.TimeoutExpired) as error:
                        row.update(passed=False, error=str(error))
                    summary['scenarios'].append(row)
                    write(output / 'summary.json', summary)
                    print(f"{name}: {'PASS' if row['passed'] else 'FAIL'}", flush=True)
    summary['source_unchanged'] = current_manifest == source_manifest(after)
    summary['driver_unchanged'] = summary['driver_sha256'] == file_digest(__file__)
    summary['coverage'] = coverage(after_results)
    summary['passed'] = (summary['source_unchanged'] and summary['driver_unchanged'] and
                         all(r['passed'] for r in summary['scenarios']))
    write(output / 'summary.json', summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--before', type=Path, default=DEFAULT_BEFORE)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--after', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/modular_equivalence')
    parser.add_argument('--cases', nargs='+', default=list(SYNTHETIC_CASES))
    parser.add_argument('--cores', nargs='+', type=int, default=[2, 3, 4, 5])
    parser.add_argument('--modes', nargs='+', choices=['official', 'adaptive'], default=['official', 'adaptive'])
    parser.add_argument('--scopes', nargs='+', choices=['standalone', 'shared_case'], default=['standalone', 'shared_case'])
    parser.add_argument('--graphs', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--official-cost', type=float, default=8.)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--worker-result', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        # Strip inherited package search paths, including caller PYTHONPATH.
        os.environ.pop('PYTHONPATH', None)
        script_dir = str(Path(__file__).resolve().parent)
        sys.path[:] = [p for p in sys.path if p and p != script_dir and
                       not p.replace('\\', '/').endswith('/src')]
        captured = io.StringIO()
        with redirect_stdout(captured):
            result = worker(read(args.worker))
        result['stdout'] = captured.getvalue()
        write(args.worker_result, result)
        return 0
    if any(n not in (2, 3, 4, 5) for n in args.cores) or len(set(args.cores)) != len(args.cores):
        parser.error('cores must be distinct values from 2, 3, 4, 5')
    if args.rounds < 0 or args.official_cost <= 0:
        parser.error('rounds must be nonnegative and official-cost must be positive')
    manifest = args.manifest
    if manifest is None and args.before == DEFAULT_BEFORE:
        candidate = args.before.with_name('before_manifest.json')
        manifest = candidate if candidate.is_file() else None
    result = compare_suite(args.before, args.after, args.output, cases=args.cases, cores=args.cores,
                           modes=args.modes, scopes=args.scopes, graphs=args.graphs, config=args.config,
                           rounds=args.rounds, official_cost=args.official_cost, timeout=args.timeout,
                           manifest=manifest)
    print(json.dumps(dict(passed=result['passed'], coverage=result['coverage']), ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
