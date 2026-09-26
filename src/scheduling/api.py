"""One graph, shared serial searches, and an optional reporting-only baseline."""
from collections import defaultdict
from pathlib import Path
from time import perf_counter, process_time
import traceback
import tempfile
import shutil

from .budget import run_bounded, SearchBudget, CaseSliceBudget
from .engine import search
from .io import read, write, digest, ROOT
from .service import SearchServices
from .registry import resolve_problem
from .configuration import intermediate_artifacts, resolve_time_budget


def default_parameters(problem='q1'):
    return resolve_problem(problem).default_parameters()


def source_signature(problem='q1'):
    """Fingerprint common code, registered adapters, official code and config."""
    import hashlib
    definition = resolve_problem(problem)
    paths = sorted(set(list((ROOT/'src').rglob('*.py')) +
                   list((ROOT/'official/code').glob('*.py')) + list((ROOT/'configs').glob('*.json')) +
                   [ROOT/path for path in definition.artifact_files]))
    return hashlib.sha256('\n'.join(f'{p.relative_to(ROOT).as_posix()}:{digest(p)}' for p in paths).encode()).hexdigest()


def _folder(output, cores, single):
    return output if single else output/f'N{cores}'


def _base_row(cores, request):
    return dict(cores=cores, status='not_started', makespan=None, score_source=None,
        baseline=dict(mode='not_computed', cycles=None), speedup=None,
        wall_seconds=0., accounted_wall_seconds=0., cpu_seconds=0.,
        cached_baseline_charge_seconds=0., hard_timeout=False, exceeded_target=False,
        extended=False, exceeded_extension=False, target_seconds=None,
        extension_seconds=None, hard_limit_seconds=None,
        case_target_seconds=request['seconds'], budget_scope='case_all_cores',
        source_signature=request['source_signature'], problem_id=request.get('problem_id','q1'),
        backend_version=request.get('backend_version'), time_policy='case_shared_no_baseline',
        local_test_cache=False, parallel_solver=False, cpu_is_lower_bound=False,
        officially_evaluated_final=False, speedup_source=None)


def _case_worker(request, output, started, single=False, problem=None):
    problem = resolve_problem(problem or request.get("problem_id", "q1"))
    semantics, execution, prediction = problem.semantics, problem.execution, problem.prediction
    validate_plan, objective, plan_digest = semantics.validate, semantics.objective, semantics.key
    output = Path(output)
    cpu_started = process_time()
    timing, rows = {}, {}
    active_core, active_phase = None, 'input'
    limits = request['time_budget']
    case_budget = SearchBudget(request['seconds'], started,
        extension=limits['extension_seconds'], hard=limits['hard_limit_seconds'])
    diagnostics = intermediate_artifacts(request['parameters'])

    def diagnostic(path, value):
        if diagnostics:
            write(path, value)

    def save_search(folder, history):
        # The supervisor only reads this field for timeout accounting.
        write(folder/'search.json', history if diagnostics else {'extended': history.get('extended', False)})

    def phase(name, core=None):
        nonlocal active_phase, active_core
        active_phase, active_core = name, core
        write(output/'progress.json', dict(phase=name, cores=core,
            wall_seconds=perf_counter()-started, cpu_seconds=process_time()-cpu_started,
            timing=timing, case_budget=case_budget.metadata()))

    try:
        if source_signature(problem) != request['source_signature']:
            raise ValueError('solver source changed before worker startup')
        phase('input')
        t = perf_counter()
        raw = read(request['graph'])
        if (digest(request['graph']) != request['graph_sha256'] or
                digest(request['config']) != request['config_sha256']):
            raise ValueError('input/config changed after request creation')
        settings = execution.read_settings(request['config'])
        timing['input_seconds'] = perf_counter()-t
        phase('shared_model_and_features')
        t = perf_counter()
        model = execution.create_model(raw)
        settings_dict = execution.settings_dict(settings)
        features = prediction.recognize(model, settings_dict)
        index = execution.create_index(model, settings)
        approximation = prediction.create_estimator(model, settings_dict,
            request['parameters'].get('evaluation', {}))
        cost_model, generation_costs = prediction.create_cost_model(model), defaultdict(list)
        diagnostic(output/'features.json', features)
        timing.update(model_seconds=perf_counter()-t, singlecore_seconds=0.,
            shared_preparation_seconds=perf_counter()-started,
            shared_preparation_cpu_seconds=process_time()-cpu_started)

        # Give every requested count a checkpoint before any expensive search.
        for cores in request['cores']:
            folder = _folder(output, cores, single)
            folder.mkdir(parents=True, exist_ok=True)
            row = rows[cores] = _base_row(cores, request)
            diagnostic(folder/'request.json', dict(request, cores=cores, case_cores=request['cores']))
            diagnostic(folder/'baseline.json', row['baseline'])
            diagnostic(folder/'features.json', features)
            phase('structural_seed', cores)
            t, c = perf_counter(), process_time()
            plan, seed_meta = problem.proposals.seed(model, cores, settings)
            validate_plan(model, plan, cores)
            if not model.ops:
                result = execution.empty_result(cores)
            elif approximation is None:
                result = execution.evaluate(raw, plan, settings)
            else:
                result = approximation.result(plan)
            elapsed, cpu = perf_counter()-t, process_time()-c
            write(folder/'checkpoint.json', dict(plan=plan, result=result,
                wall_seconds=elapsed, cpu_seconds=cpu, case_elapsed_seconds=perf_counter()-started))
            diagnostic(folder/'plan.json', plan)
            diagnostic(folder/'evaluation.json', result)
            diagnostic(folder/'structural_seed.json', seed_meta)
            row.update(status='seed_ready', makespan=result['makespan'],
                score_source=result.get('evaluation_source', 'official'),
                wall_seconds=elapsed, accounted_wall_seconds=elapsed, cpu_seconds=cpu,
                timing=dict(seed_seconds=elapsed,search_seconds=0.,singlecore_seconds=0.),
                history_stop='initial_checkpoint')
            write(folder/'summary.json', row)

        for position, cores in enumerate(request['cores']):
            folder, row = _folder(output, cores, single), rows[cores]
            if not model.ops or not case_budget.can_start(.01):
                row.update(status='ok', history_stop='empty_graph' if not model.ops else 'case_budget_exhausted')
                write(folder/'summary.json', row)
                continue
            phase('core_search', cores)
            budget = CaseSliceBudget(case_budget, len(request['cores'])-position)
            row.update(target_seconds=budget.target, extension_seconds=budget.extension,
                       hard_limit_seconds=budget.hard)
            write(folder/'summary.json', row)
            search_start, search_cpu = perf_counter(), process_time()
            seed_seconds, seed_cpu = row['wall_seconds'], row['cpu_seconds']
            official_best = [None]

            def checkpoint(plan, value):
                validate_plan(model, plan, cores)
                write(folder/'checkpoint.json', dict(plan=plan, result=value,
                    wall_seconds=seed_seconds+perf_counter()-search_start,
                    cpu_seconds=seed_cpu+process_time()-search_cpu,
                    case_elapsed_seconds=perf_counter()-started))

            def verified(plan, value):
                if value.get('evaluation_source') == 'approximate':
                    raise ValueError('approximate checkpoint cannot be certified')
                if official_best[0] is None or objective(value)<official_best[0]:
                    official_best[0] = objective(value)
                    diagnostic(folder/'official_checkpoint.json', dict(plan=plan,result=value,
                        wall_seconds=seed_seconds+perf_counter()-search_start,
                        cpu_seconds=seed_cpu+process_time()-search_cpu))

            def rejected(plan):
                path = folder/'checkpoint.json'
                if path.exists():
                    saved = read(path)
                    if plan_digest(saved['plan']) == plan_digest(plan):
                        diagnostic(folder/'rejected_checkpoints'/f'{plan_digest(plan)}.json',
                              dict(saved, official_rejected=True))
                        path.unlink()
                        for name in ('plan.json', 'evaluation.json'):
                            (folder/name).unlink(missing_ok=True)

            service = SearchServices(model,settings,problem=problem,checkpoint=checkpoint,verified=verified,rejected=rejected,
                save=lambda h: save_search(folder,h),
                progress=lambda e: phase(e.get('phase') or e.get('action') or e['event'],cores))
            service.shared_case = True
            service.approximation, service.cost_model = approximation, cost_model
            service.graph_index, service.generation_costs = index, generation_costs
            service.can_continue = lambda: case_budget.elapsed()<case_budget.hard
            constructor = problem.proposals.create_constructor(model,cores,settings,service)
            seed = read(folder/'checkpoint.json')
            try:
                # Earlier core counts may have supplied official calibration.
                # Compare this core's seed and new proposals on that same scale.
                if seed['result'].get('evaluation_source') == 'approximate':
                    seed['result'] = approximation.result(seed['plan'])
                    checkpoint(seed['plan'], seed['result'])
                plan,value,history = search(model,cores,service.settings_dict,
                    request['parameters'],budget.target,service,seed=(seed['plan'],seed['result']),
                    started=budget.started,constructor=constructor,budget=budget,problem=problem)
                status = 'ok'
            except Exception as error:
                row.update(error=str(error),traceback=traceback.format_exc())
                if not (folder/'checkpoint.json').exists():
                    elapsed = perf_counter()-search_start
                    row.update(status='failed_no_solution', makespan=None, score_source=None,
                        wall_seconds=seed_seconds+elapsed, accounted_wall_seconds=seed_seconds+elapsed,
                        cpu_seconds=seed_cpu+process_time()-search_cpu,
                        history_stop='no_valid_checkpoint', officially_evaluated_final=False)
                    row['timing']['search_seconds'] = elapsed
                    diagnostic(folder/'construction.json',constructor.records)
                    write(folder/'summary.json',row)
                    continue
                saved = read(folder/'checkpoint.json')
                plan,value = saved['plan'],saved['result']
                history,status = dict(stop_reason='search_failed_with_checkpoint'),'failed_with_checkpoint'
            elapsed = perf_counter()-search_start
            row.update(status=status,makespan=value['makespan'],
                score_source=value.get('evaluation_source','official'),
                wall_seconds=seed_seconds+elapsed,accounted_wall_seconds=seed_seconds+elapsed,
                cpu_seconds=seed_cpu+process_time()-search_cpu,target_seconds=budget.target,
                extension_seconds=budget.extension,hard_limit_seconds=budget.hard,
                exceeded_target=elapsed>budget.target,extended=budget.extended,
                exceeded_extension=elapsed>budget.extension,service_stats=dict(service.stats),
                history_stop=history.get('stop_reason'),
                officially_evaluated_final=value.get('evaluation_source')!='approximate')
            row['timing']['search_seconds'] = elapsed
            row['speedup_source'] = row['score_source']
            diagnostic(folder/'plan.json',plan)
            diagnostic(folder/'evaluation.json',value)
            write(folder/'checkpoint.json',dict(plan=plan,result=value,wall_seconds=row['wall_seconds'],
                cpu_seconds=row['cpu_seconds'],case_elapsed_seconds=perf_counter()-started))
            diagnostic(folder/'construction.json',constructor.records)
            write(folder/'summary.json',row)

        if source_signature(problem)!=request['source_signature']:
            raise ValueError('solver source changed during execution; result is not reusable')
        phase('complete')
        write(output/'worker.json',dict(status='ok',rows=list(rows.values()),timing=timing,
            cpu_seconds=process_time()-cpu_started,worker_wall_seconds=perf_counter()-started,
            extended=case_budget.extended))
    except Exception as error:
        write(output/'worker.json',dict(status='failed',phase=active_phase,cores=active_core,
            error=str(error),traceback=traceback.format_exc(),rows=list(rows.values()),timing=timing,
            cpu_seconds=process_time()-cpu_started,worker_wall_seconds=perf_counter()-started))


def _run_case(graph,config,cores,output,seconds,parameters,single=False,problem="q1",_locked=False):
    problem = resolve_problem(problem)
    parameters = problem.default_parameters() if parameters is None else parameters
    limits = resolve_time_budget(parameters, seconds)
    if intermediate_artifacts(parameters):
        return _run_case_files(graph,config,cores,output,limits,parameters,single,problem,_locked)
    if problem.case_runtime is not None:
        raise ValueError('minimal artifacts require a backend using the shared case worker')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('output directory must be empty; use verified case-level resume')
    # Durable checkpoints still protect results if the supervisor kills a
    # worker. They remain private and are cleaned after final-plan recovery.
    with tempfile.TemporaryDirectory(prefix='q1-checkpoints-') as temporary:
        result = _run_case_files(graph,config,cores,temporary,limits,parameters,single,problem)
        for row in result['rows']:
            source = _folder(Path(temporary),row['cores'],single)/'plan.json'
            if source.exists() and row.get('makespan') is not None:
                target = _folder(output,row['cores'],single)/'plan.json'
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source,target)
        return result


def _run_case_files(graph,config,cores,output,limits,parameters,single=False,problem="q1",_locked=False):
    problem = resolve_problem(problem)
    started = perf_counter()
    seconds = limits['target_seconds']
    cores = list(cores)
    if not cores or len(set(cores))!=len(cores) or any(type(n)is not int or n not in problem.supported_cores for n in cores):
        raise ValueError(f'cores must be distinct members of {problem.supported_cores}')
    graph,config,output = Path(graph).resolve(),Path(config).resolve(),Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    if problem.case_runtime is not None and not _locked:
        from problem_1_baseline import output_lock
        with output_lock(output):
            return _run_case_files(graph,config,cores,output,limits,parameters,single,problem,True)
    if any(path.name != '.run.lock' or not _locked for path in output.iterdir()):
        raise ValueError('output directory must be empty; use verified case-level resume')
    request = dict(graph=str(graph),config=str(config),cores=cores,seconds=seconds,
        parameters=parameters, time_budget=limits,
        problem_id=problem.id,backend_version=problem.version,
        graph_sha256=digest(graph),config_sha256=digest(config),source_signature=source_signature(problem),
        budget_scope='case_all_cores',baseline_policy=(
            'backend_owned' if problem.case_runtime is not None else 'not_required_for_search'))
    write(output/('case_request.json' if single else 'request.json'),request)
    telemetry = {}
    worker = problem.case_runtime.worker if problem.case_runtime is not None else _case_worker
    expired = run_bounded(worker,(request,str(output),started,single,problem),
                          started+limits['hard_limit_seconds'],telemetry=telemetry)
    if problem.case_runtime is not None:
        return problem.case_runtime.recover(request, output, started, single, expired, telemetry)
    elapsed = perf_counter()-started
    outcome = read(output/'worker.json') if (output/'worker.json').exists() else {}
    progress = read(output/'progress.json') if (output/'progress.json').exists() else {}
    timing = outcome.get('timing',progress.get('timing',{}))
    rows = []
    for n in cores:
        folder = _folder(output,n,single)
        row = read(folder/'summary.json') if (folder/'summary.json').exists() else _base_row(n,request)
        checkpoint = read(folder/'checkpoint.json') if (folder/'checkpoint.json').exists() else None
        if expired and n==progress.get('cores'):
            row.update(status='hard_timeout_with_checkpoint' if checkpoint else 'hard_timeout_no_solution',
                hard_timeout=True,timed_out_phase=progress.get('phase'),cpu_is_lower_bound=True)
            if checkpoint:
                row.update(wall_seconds=checkpoint['wall_seconds'],accounted_wall_seconds=checkpoint['wall_seconds'],
                           cpu_seconds=checkpoint['cpu_seconds'])
        elif row['status'] in ('seed_ready','not_started'):
            row.update(status='budget_exhausted_with_checkpoint' if checkpoint else 'no_solution',
                       history_stop='case_hard_limit' if expired else 'case_worker_failed')
        if checkpoint:
            write(folder/'plan.json',checkpoint['plan'])
            write(folder/'evaluation.json',checkpoint['result'])
            row.update(makespan=checkpoint['result']['makespan'],
                       score_source=checkpoint['result'].get('evaluation_source','official'))
            row['officially_evaluated_final'] = row['score_source']=='official'
            row['speedup_source'] = row['score_source']
        rows.append(row)
    # Account for time spent in an interrupted operation, not just its last checkpoint.
    if expired and progress.get('cores') in cores:
        active = next(r for r in rows if r['cores']==progress['cores'])
        active['wall_seconds'] = max(active['wall_seconds'],elapsed-
            timing.get('shared_preparation_seconds',0)-sum(r['wall_seconds'] for r in rows if r is not active))
        active['accounted_wall_seconds'] = active['wall_seconds']
        history_path = _folder(output, active['cores'], single)/'search.json'
        history = read(history_path) if history_path.exists() else {}
        active['extended'] = history.get('extended', False)
        search_elapsed = active['wall_seconds']-active.get('timing',{}).get('seed_seconds',0)
        active['exceeded_target'] = active.get('target_seconds') is not None and search_elapsed>active['target_seconds']
        active['exceeded_extension'] = active.get('extension_seconds') is not None and search_elapsed>active['extension_seconds']
        if 'cpu_seconds' in telemetry:
            active['cpu_seconds'] = max(active['cpu_seconds'],telemetry['cpu_seconds']-
                timing.get('shared_preparation_cpu_seconds',0)-sum(r['cpu_seconds'] for r in rows if r is not active))
            active['cpu_is_lower_bound'] = False
    for row in rows:
        write(_folder(output,row['cores'],single)/'summary.json',row)
    coverage = all(r.get('makespan') is not None for r in rows)
    ok = not expired and outcome.get('status')=='ok' and all(r['status']=='ok' for r in rows)
    result = dict(status='ok' if ok else 'hard_timeout_with_checkpoint' if expired and coverage else 'failed',
        rows=rows,cores=cores,coverage_complete=coverage,wall_seconds=elapsed,accounted_wall_seconds=elapsed,
        cpu_seconds=telemetry.get('cpu_seconds',outcome.get('cpu_seconds')),
        shared_preparation_seconds=timing.get('shared_preparation_seconds'),timing=timing,
        target_seconds=seconds,extension_seconds=limits['extension_seconds'],hard_limit_seconds=limits['hard_limit_seconds'],
        exceeded_target=elapsed>seconds,extended=outcome.get('extended',progress.get('case_budget',{}).get('extended',False)),
        exceeded_extension=elapsed>limits['extension_seconds'],hard_timeout=expired,
        source_signature=request['source_signature'],problem_id=problem.id,backend_version=problem.version,budget_scope='case_all_cores',
        baseline_policy='not_required_for_search',baseline_seconds=0.,parallel_solver=False)
    if outcome.get('error'):
        result.update(error=outcome['error'],traceback=outcome.get('traceback'))
    if expired:
        result['timed_out_phase'] = progress.get('phase')
    write(output/('case_summary.json' if single else 'summary.json'),result)
    return result


def solve_case(graph,config,cores,output,*,seconds=None,parameters=None,problem="q1"):
    """All requested core counts share ONE case budget and one computing worker."""
    return _run_case(graph,config,cores,output,seconds,parameters,problem=problem)
