"""Supervised serial solve for arbitrary graph paths; no multicore result inputs."""
from pathlib import Path
from time import perf_counter, process_time
import json
import math
import traceback

from .. import official
from ..model import Graph
from ..plan import validate_plan, objective
from .budget import run_bounded
from .construction import Construction
from .engine import search
from .io import read, write, digest, ROOT
from .seed import extend_empty_cores
from .service import SearchServices
from .features import recognize


def default_parameters():
    return read(ROOT/'configs/unified.json')


def source_signature():
    import hashlib
    paths = sorted(list((ROOT/'src/q1').rglob('*.py')) +
                   list((ROOT/'official/code').glob('*.py')) + [ROOT/'configs/unified.json'])
    return hashlib.sha256('\n'.join(f'{p.relative_to(ROOT).as_posix()}:{digest(p)}' for p in paths).encode()).hexdigest()


def _solve_worker(request, output, started):
    output = Path(output)
    cpu = process_time()
    timing, baseline_meta = {}, {}
    active_phase = 'input'
    def phase(name):
        nonlocal active_phase
        active_phase = name
        write(output/'progress.json', dict(phase=name, wall_seconds=perf_counter()-started,
                                         cpu_seconds=process_time()-cpu, timing=timing))
    try:
        if source_signature() != request['source_signature']:
            raise ValueError('solver source changed before worker startup')
        phase('input')
        t = perf_counter()
        raw = read(request['graph'])
        if digest(request['graph']) != request['graph_sha256'] or digest(request['config']) != request['config_sha256']:
            raise ValueError('input/config changed after request creation')
        settings = official.Settings.read(request['config'])
        timing['input_seconds'] = perf_counter()-t
        phase('model_and_features')
        t = perf_counter()
        model = Graph(raw)
        graph_features = recognize(model, dict(capacity=settings.capacity, bandwidth=settings.bandwidth))
        write(output/'features.json', graph_features)
        timing['model_seconds'] = perf_counter()-t
        phase('singlecore')
        t = perf_counter()
        cache = request.get('test_baseline')
        if cache is None:
            baseline = official.singlecore(raw, settings)
            baseline_meta = dict(mode='fresh', source='official.singlecore', omitted_seconds=0)
        else:
            # Only solve_file can prepare this record after full manifest/input/config/code validation.
            if digest(cache['result_path']) != cache['result_sha256']:
                raise ValueError('singlecore test cache changed after validation')
            baseline = read(cache['result_path'])
            if baseline['makespan'] != cache['makespan_cycles']:
                raise ValueError('singlecore cached cycles mismatch')
            baseline_meta = dict(mode='explicit_local_test_cache', reference=cache,
                                 omitted_seconds=cache.get('historical_seconds'))
        timing['singlecore_seconds'] = perf_counter()-t
        baseline_meta['cycles'] = baseline['makespan']
        write(output/'baseline.json', baseline_meta)
        plan = {'node_to_subgraph': {str(v): 0 for v in model.ops},
                'core_schedules': [[0] if model.ops else []]}
        plan, seed = extend_empty_cores(plan, baseline, request['cores'])
        official_best = [None]
        def verified(plan, result):
            if official_best[0] is None or objective(result)<official_best[0]:
                official_best[0]=objective(result)
                write(output/'official_checkpoint.json',dict(plan=plan,result=result,
                    wall_seconds=perf_counter()-started,cpu_seconds=process_time()-cpu))
        def checkpoint(plan, result):
            # Atomic bundle prevents a new plan being paired with an old score after termination.
            validate_plan(model, plan, request['cores'])
            write(output/'checkpoint.json', dict(plan=plan, result=result,
                wall_seconds=perf_counter()-started, cpu_seconds=process_time()-cpu))
        service = SearchServices(model, settings, checkpoint=checkpoint, verified=verified,
            save=lambda h: write(output/'search.json', h),
              progress=lambda e: phase(e.get('phase') or e.get('action') or e['event']))
        service.baseline_seconds = (baseline_meta.get('omitted_seconds') if cache is not None
                                    else timing['singlecore_seconds'])
        service.checkpoint(plan, seed)
        verified(plan,seed)
        phase('construction_and_improvement')
        t = perf_counter()
        if model.ops:
            constructor = Construction(model, request['cores'], settings, service)
            plan, result, history = search(model, request['cores'], service.settings_dict,
                request['parameters'], request['seconds'], service, seed=(plan, seed),
                started=started-request.get('cached_baseline_charge_seconds',0), constructor=constructor)
            write(output/'construction.json', constructor.records)
        else:
            result, history = seed, dict(stop_reason='empty_graph')
            write(output/'search.json',history)
        timing['search_seconds'] = perf_counter()-t
        phase('output')
        t = perf_counter()
        write(output/'plan.json', plan)
        write(output/'evaluation.json', result)
        timing['output_seconds'] = perf_counter()-t
        if source_signature() != request['source_signature']:
            raise ValueError('solver source changed during execution; result is not reusable')
        write(output/'worker.json', dict(status='ok', makespan=result['makespan'],
            score_source=result.get('evaluation_source','official'),
            baseline=baseline_meta, speedup=baseline['makespan']/result['makespan'] if result['makespan'] else None,
            timing=timing, service_stats=dict(service.stats), history_stop=history.get('stop_reason'),
            cpu_seconds=process_time()-cpu, worker_wall_seconds=perf_counter()-started))
    except Exception as error:
        write(output/'worker.json', dict(status='failed', phase=active_phase, error=str(error),
            traceback=traceback.format_exc(), timing=timing, baseline=baseline_meta,
            cpu_seconds=process_time()-cpu, worker_wall_seconds=perf_counter()-started))


def solve_file(graph, config, cores, output, *, seconds=600, parameters=None,
               local_test_baseline_archive=None, archive_repo_root=None, _verified_test_record=None):
    """One graph/one target core count, serial, 600/900/1200 tiered TOTAL wall clock.

    Fresh baseline is the default. A test archive must be explicit; no previous
    multicore plans/results or case-number-specific solver decisions are accepted.
    This blocking API uses one computing child and one idle supervising parent.
    """
    started = perf_counter()
    if type(cores) is not int or cores not in (2, 3, 4, 5):
        raise ValueError('cores must be 2, 3, 4 or 5')
    if not math.isfinite(seconds) or not 0 < seconds <= 600:
        raise ValueError('target seconds must be in (0,600]')
    graph, config, output = Path(graph).resolve(), Path(config).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('output directory must be empty; use batch resume for verified reuse')
    request = dict(graph=str(graph), config=str(config), cores=cores, seconds=seconds,
        parameters=default_parameters() if parameters is None else parameters,
        graph_sha256=digest(graph), config_sha256=digest(config), source_signature=source_signature())
    if _verified_test_record is not None:
        if local_test_baseline_archive is None:
            raise ValueError('verified test records require explicit local-test archive mode')
        request['test_baseline'] = _verified_test_record
        if (_verified_test_record.get('graph_sha256') != request['graph_sha256'] or
                _verified_test_record.get('config_sha256') != request['config_sha256']):
            raise ValueError('verified test record does not belong to this input/config')
    elif local_test_baseline_archive is not None:
        from common.baseline import load_baseline_catalog
        archive = Path(local_test_baseline_archive).resolve()
        catalog = load_baseline_catalog(archive, [graph], config, official.CODE,
            repo_root=archive_repo_root or archive.parent.parent)
        entry = catalog[graph.stem]
        import csv
        with (archive/'summary.csv').open(encoding='utf-8-sig', newline='') as stream:
            row = next(r for r in csv.DictReader(stream) if r['case']==graph.stem)
        request['test_baseline'] = dict(entry, result_path=str(archive/entry['baseline_ref']['result']),
            result_sha256=entry['baseline_ref']['result_sha256'],
            historical_seconds=float(row['runtime_seconds']) if row.get('runtime_seconds') else None)
    charge = request.get('test_baseline',{}).get('historical_seconds') or 0
    request['cached_baseline_charge_seconds'] = charge
    write(output/'request.json', request)
    telemetry = {}
    expired = run_bounded(_solve_worker, (request, str(output), started), started+seconds*2-charge,
                          telemetry=telemetry)
    outcome = read(output/'worker.json') if (output/'worker.json').exists() else {}
    if expired:
        checkpoint = read(output/'checkpoint.json') if (output/'checkpoint.json').exists() else None
        progress = read(output/'progress.json') if (output/'progress.json').exists() else {}
        outcome = dict(status='hard_timeout_with_checkpoint' if checkpoint else 'hard_timeout_no_solution',
            timed_out_phase=progress.get('phase'), timing=progress.get('timing',{}),
            cpu_seconds=checkpoint['cpu_seconds'] if checkpoint else progress.get('cpu_seconds'),
            cpu_is_lower_bound=True)
        if checkpoint:
            write(output/'plan.json', checkpoint['plan'])
            write(output/'evaluation.json', checkpoint['result'])
            outcome['makespan'] = checkpoint['result']['makespan']
            outcome['score_source'] = checkpoint['result'].get('evaluation_source','official')
        if (output/'baseline.json').exists():
            outcome['baseline'] = read(output/'baseline.json')
            if checkpoint and outcome['makespan']:
                outcome['speedup'] = outcome['baseline']['cycles']/outcome['makespan']
    elapsed = perf_counter()-started
    history = read(output/'search.json') if (output/'search.json').exists() else {}
    accounted = elapsed+charge
    outcome.update(wall_seconds=elapsed, accounted_wall_seconds=accounted,
        cached_baseline_charge_seconds=charge, target_seconds=seconds, extension_seconds=seconds*1.5,
        hard_limit_seconds=seconds*2, exceeded_target=accounted>seconds,
        extended=history.get('extended',False), exceeded_extension=accounted>seconds*1.5,
        hard_timeout=expired, source_signature=request['source_signature'],
        time_policy='tiered_total_including_singlecore',
        local_test_cache=local_test_baseline_archive is not None, parallel_solver=False)
    if 'cpu_seconds' in telemetry:
        outcome.update(cpu_seconds=telemetry['cpu_seconds'],cpu_is_lower_bound=False)
    outcome['officially_evaluated_final'] = outcome.get('score_source') == 'official'
    outcome['speedup_source'] = outcome.get('score_source')
    write(output/'summary.json', outcome)
    return outcome
