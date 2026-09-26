"""Read-only frozen Q1 solver; paired beta sweep with independent official replays."""
from __future__ import annotations
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import argparse, csv, gc, gzip, hashlib, json, os, platform, subprocess, sys, time, traceback
import random

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC / 'src'))
from q1 import official, solver
from q1.graph import GraphIndex

def now():
    return datetime.now(timezone.utc).isoformat()

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix(p.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, p)

def packed(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, 'wt', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))

def unpack(p):
    with gzip.open(p, 'rt', encoding='utf-8') as f:
        return json.load(f)

def normalized(data):
    return json.loads(json.dumps(data))

def verify_sources(manifest):
    for name, expected in manifest['source_sha256'].items():
        assert digest(SRC / name) == expected, f'Source changed: {name}'
    assert digest(Path(manifest['official_config'])) == manifest['official_config_sha256'], 'Official config changed'
    for item in manifest['selected']:
        assert digest(Path(item['graph_path'])) == item['input_sha256'], f"Input changed: {item['case']}"

def measure_row(case, n, variant, result, selected, solve_seconds, cpu_seconds, verify_seconds):
    return {'case':case, 'num_cores':n, 'variant':variant,
            'makespan_cycles':result['makespan'], **result['data_movement_bytes'],
            'selected_mode':selected['mode'], 'selected_beta':selected['beta'],
            'task_count':sum(len(c['tasks']) for c in result['per_core_timeline']),
            'used_cores':sum(bool(c['tasks']) for c in result['per_core_timeline']),
            'solve_seconds':solve_seconds, 'solve_cpu_seconds':cpu_seconds,
            'timing_method': 'native_independent' if variant=='adaptive' else 'shared_candidate_sum',
            'verification_seconds':verify_seconds, 'status':'SUCCESS_VERIFIED'}

def job(case, n, output):
    ROOT = Path(output)
    manifest = json.loads((ROOT / 'experiment.json').read_text(encoding='utf-8'))
    verify_sources(manifest)
    signature = digest(ROOT / 'experiment.json')
    out = ROOT / 'runs' / case / f'N{n}'
    done = out / 'complete.json'
    if done.exists():
        marker = json.loads(done.read_text(encoding='utf-8'))
        assert marker['experiment_sha256'] == signature
        for name, expected in marker['files'].items():
            assert digest(out / name) == expected, f'Changed artifact: {name}'
        return marker['rows']
    if out.exists():
        raise ValueError(f'Incomplete published directory: {out}; preserve it and use another output')
    final = out
    out = out.parent / ('.' + out.name + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
    out.mkdir(parents=True, exist_ok=False)
    done = out / 'complete.json'
    started = time.perf_counter()
    write(out / 'progress.json', {'phase':'load', 'started_at':now(), 'pid':os.getpid()})
    input_path = Path(next(r['graph_path'] for r in manifest['selected'] if r['case']==case))
    config = Path(manifest['official_config'])
    t0, c0 = time.perf_counter(), time.process_time()
    graph = official.read_json(input_path)
    settings = official.Settings.read(config)
    index = GraphIndex(graph, settings)
    prep_seconds, prep_cpu = time.perf_counter()-t0, time.process_time()-c0
    features = {'compute_operations':len(index.ops), 'weak_components':len(index.components),
                'pipe_work':index.work, 'tensor_bytes':sum(t.size for t in index.tensors.values())}
    write(out / 'features.json', features)
    # Observe the existing grid entrypoint; candidates and solver decisions are unmodified.
    original = solver._evaluate_candidate
    candidate_cpu = {}
    last_progress = 0.0
    def progress(event):
        nonlocal last_progress
        tick = time.perf_counter()
        if tick-last_progress >= 2 or event.get('event') in ('candidate_start','candidate_end','mode_search_end'):
            write(out/'progress.json', {'phase':'solve','updated_at':now(),'elapsed_seconds':tick-started, **event})
            last_progress = tick
    def observed(index, num_cores, mode, beta, scopes, progress=None):
        cpu0 = time.process_time()
        record, plan, result = original(index, num_cores, mode, beta, scopes, progress)
        candidate_cpu[(mode,beta)] = time.process_time()-cpu0
        directory = out/'candidates'/f'{mode}_beta{beta}'
        packed(directory/'record.json.gz', record)
        if result is not None:
            packed(directory/'result.json.gz', result)
        return record, plan, result
    solver._evaluate_candidate = observed
    try:
        grid = solver.initialize(graph, n, settings, index=index, beta_search='grid', progress=progress)
    finally:
        solver._evaluate_candidate = original
    packed(out/'grid_construction.json.gz', {'selected':grid.selected,'search':grid.search,'candidates':grid.candidates})
    assert len(grid.candidates) == 4*len(index.scopes(n))
    assert all(r['status']=='success' for r in grid.candidates), 'Failed grid candidate requires separate investigation'
    modes = {mode:i for i,(mode,_) in enumerate(index.scopes(n))}
    def key(record):
        return (record['makespan'], record['data_movement_bytes']['added_copy_bytes'], modes[record['mode']], record['beta'])
    fixed = {}
    for beta in (1,2,3,4):
        records = [r for r in grid.candidates if r['beta']==beta]
        best = min(records,key=key)
        fixed[f'beta{beta}'] = (best['plan'], {'mode':best['mode'],'beta':beta},
                               out/'candidates'/f"{best['mode']}_beta{beta}"/'result.json.gz',
                               prep_seconds+sum(r['elapsed_seconds'] for r in records),
                               prep_cpu+sum(candidate_cpu[(r['mode'],beta)] for r in records))
    # Reconstruct the eligible adaptive prefix for exact comparison with a fresh native run.
    visited = []
    for mode in modes:
        records = sorted((r for r in grid.candidates if r['mode']==mode),key=lambda r:r['beta'])
        previous = None
        for record in records:
            visited.append(record)
            if previous is not None and record['makespan'] >= previous:
                break
            previous = record['makespan']
    predicted = min(visited,key=key)
    predicted_plan = predicted['plan']
    predicted_key = key(predicted)
    grid_best = min(grid.candidates,key=key)
    grid_best_time = grid_best['makespan']
    candidate_summary = [{k:r[k] for k in ('mode','beta','status','makespan','data_movement_bytes','elapsed_seconds')}
                         for r in grid.candidates]
    del grid, index
    gc.collect()
    write(out/'progress.json', {'phase':'adaptive_native','updated_at':now()})
    t0, c0 = time.perf_counter(), time.process_time()
    adaptive = solver.initialize(graph,n,settings,beta_search='adaptive',progress=progress)
    adaptive_seconds, adaptive_cpu = time.perf_counter()-t0, time.process_time()-c0
    assert normalized(adaptive.plan)==normalized(predicted_plan), 'Native adaptive differs from grid-prefix reconstruction'
    assert (adaptive.evaluation['makespan'],adaptive.evaluation['data_movement_bytes']['added_copy_bytes'],
            modes[adaptive.selected['mode']],adaptive.selected['beta']) == predicted_key
    packed(out/'adaptive_construction.json.gz', {'selected':adaptive.selected,'search':adaptive.search,'candidates':adaptive.candidates})
    packed(out/'adaptive_expected.json.gz',adaptive.evaluation)
    fixed['adaptive'] = (adaptive.plan, adaptive.selected, out/'adaptive_expected.json.gz',adaptive_seconds,adaptive_cpu)
    write(out/'candidate_summary.json',{'candidates':candidate_summary,'grid_best_makespan':grid_best_time,
                                       'adaptive_native_equals_grid_prefix':True,
                                       'adaptive_evaluated_candidates':len(adaptive.candidates)})
    del adaptive, graph
    gc.collect()
    rows = []
    for variant,(plan, selected, expected_path, solve_seconds,cpu_seconds) in fixed.items():
        target = out/variant
        target.mkdir(exist_ok=True)
        write(target/'plan.json', plan)
        write(out/'progress.json',{'phase':'independent_official_replay','variant':variant,'updated_at':now()})
        command = [sys.executable,'-B',str(SRC/'official/code/multicore_cut_evaluate_problem_1.py'),
                   str(input_path),str(target/'plan.json'),'--config',str(config),
                   '-o',str(target/'result.json'),'--trace-output',str(target/'trace.json'),
                   '--log-output',str(target/'result.log')]
        t0 = time.perf_counter()
        completed = subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',
                                   timeout=manifest['parameters']['official_timeout_seconds'],
                                   env={**os.environ,'PYTHONUTF8':'1'})
        verify_seconds = time.perf_counter()-t0
        (target/'cli.log').write_text(completed.stdout+'\n'+completed.stderr,encoding='utf-8')
        assert completed.returncode==0, completed.stderr
        result = official.read_json(target/'result.json')
        assert result['input_graph']==input_path.name and result['input_plan']=='plan.json'
        actual = dict(result); actual.pop('input_plan'); actual.pop('input_graph')
        assert actual==unpack(expected_path), f'Full official result mismatch: {case} N{n} {variant}'
        row = measure_row(case,n,variant,result,selected,solve_seconds,cpu_seconds,verify_seconds)
        row.update(plan_sha256=digest(target/'plan.json'),official_result_full_equal=True,
                   size_stratum=next(r['size_stratum'] for r in manifest['selected'] if r['case']==case))
        rows.append(row)
        write(target/'metrics.json',row)
        # Preserve large official artifacts losslessly in compressed form.
        for name in ('result.json','trace.json'):
            p = target/name
            raw = p.read_bytes()
            compressed = p.with_suffix(p.suffix+'.gz')
            with gzip.open(compressed,'wb') as f: f.write(raw)
            with gzip.open(compressed,'rb') as f: assert f.read()==raw
            p.unlink()
    verify_sources(manifest)
    write(out/'progress.json',{'phase':'complete','updated_at':now(),'elapsed_seconds':time.perf_counter()-started})
    files = {str(p.relative_to(out)).replace('\\','/'):digest(p) for p in sorted(out.rglob('*'))
             if p.is_file() and p.name not in ('complete.json','failure.json')}
    write(done,{'status':'SUCCESS_VERIFIED','experiment_sha256':signature,'completed_at':now(),
                'files':files,'rows':rows,'wall_seconds':time.perf_counter()-started})
    out.rename(final)
    return rows


def source_hashes():
    paths = list((SRC/'src/q1').glob('*.py')) + list((SRC/'official/code').glob('*.py'))
    paths += [SRC/'tools/q1_sensitivity.py', SRC/'tools/report_q1_sensitivity.py']
    return {p.relative_to(SRC).as_posix(): digest(p) for p in sorted(paths)}


def select_cases(population, parameters, explicit=None):
    """Sampling uses input sizes only; never sees solver outcomes."""
    population = sorted(population, key=lambda r:(r['compute_operations'],r['case']))
    if explicit is not None:
        if len(explicit)!=len(set(explicit)):
            raise ValueError('Duplicate explicit case IDs')
        missing=set(explicit)-{r['case'] for r in population}
        if missing:
            raise ValueError(f'Missing cases: {sorted(missing)}')
        return [{**r,'size_stratum':0} for r in population if r['case'] in explicit]
    if len(population)!=parameters['expected_population']:
        raise ValueError(f"Expected {parameters['expected_population']} graphs, found {len(population)}; use --cases for an explicit smoke subset")
    strata=parameters['strata']; count=parameters['cases_per_stratum']
    rng=random.Random(parameters['sampling_seed']); selected=[]
    for i in range(strata):
        group=population[len(population)*i//strata:len(population)*(i+1)//strata]
        if len(group)<count:
            raise ValueError('Each stratum must contain cases_per_stratum graphs')
        selected.extend({**r,'size_stratum':i+1} for r in rng.sample(group,count))
    return sorted(selected,key=lambda r:(r['compute_operations'],r['case']))


def prepare(output, data_dir, config_path=None, cases=None, cores=None):
    output=Path(output).resolve(); data_dir=Path(data_dir).resolve()
    config_path=Path(config_path or SRC/'configs/q1/sensitivity.json').resolve()
    parameters=official.read_json(config_path)
    required={'sampling_seed','strata','cases_per_stratum','expected_population','cores','betas','official_timeout_seconds'}
    if set(parameters)!=required:
        raise ValueError(f'Configuration keys must be {sorted(required)}')
    if parameters['betas']!=[1,2,3,4]:
        raise ValueError('This baseline supports the complete beta sweep [1, 2, 3, 4]')
    for key in ('strata','cases_per_stratum','expected_population'):
        if type(parameters[key]) is not int or parameters[key]<1:
            raise ValueError(f'{key} must be a positive integer')
    if type(parameters['sampling_seed']) is not int:
        raise ValueError('sampling_seed must be an integer')
    if cores is not None: parameters['cores']=cores
    if not parameters['cores'] or len(set(parameters['cores']))!=len(parameters['cores']) or any(type(n) is not int or n not in (2,3,4,5) for n in parameters['cores']):
        raise ValueError('cores must be distinct integers from 2, 3, 4, 5')
    timeout=parameters['official_timeout_seconds']
    if timeout is not None and (type(timeout) not in (int,float) or not 0<timeout<float('inf')):
        raise ValueError('official_timeout_seconds must be null or finite and positive')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output must be empty for prepare; use run --resume for an existing experiment')
    population=[]
    for p in sorted(data_dir.glob('case_*.json')):
        graph=official.read_json(p)
        ops=[o for o in graph['ops'] if o['op'] not in ('COPY_IN','COPY_OUT')]
        population.append({'case':p.stem,'compute_operations':len(ops),
                           'compute_cycles':sum(o.get('cycles',1) for o in ops),
                           'tensor_bytes':sum(t['size'] for t in graph['tensors']),
                           'graph_path':str(p.resolve()),'input_sha256':digest(p)})
    selected=select_cases(population,parameters,cases)
    if not selected: raise ValueError('No cases selected')
    fixed_config=SRC/'official/data/config.txt'
    official.Settings.read(fixed_config)
    try:
        commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=SRC,text=True,stderr=subprocess.DEVNULL).strip()
    except (OSError,subprocess.CalledProcessError): commit=None
    manifest={'schema_version':1,'algorithm':'Q1 V1 TaskGrowth baseline; grid sweep plus independent native adaptive',
              'parameters':parameters,'selection_method':'explicit' if cases else 'size_stratified_random',
              'population':population,'selected':selected,'cores':parameters['cores'],
              'variants':['beta1','beta2','beta3','beta4','adaptive'],
              'expected_rows':len(selected)*len(parameters['cores'])*5,
              'official_config':str(fixed_config),'official_config_sha256':digest(fixed_config),
              'source_commit':commit,'source_sha256':source_hashes(),
              'python_version':platform.python_version(),'python_executable':sys.executable,
              'created_at':now()}
    write(output/'experiment.json',manifest)
    return manifest


def audit(output, allow_partial=False):
    output=Path(output).resolve(); manifest=official.read_json(output/'experiment.json')
    verify_sources(manifest)
    rows=[]; missing=[]; checked=0
    for item in manifest['selected']:
        for n in manifest['cores']:
            pair=output/'runs'/item['case']/f'N{n}'
            if not (pair/'complete.json').exists():
                missing.append([item['case'],n]);continue
            marker=official.read_json(pair/'complete.json')
            if marker['experiment_sha256']!=digest(output/'experiment.json'):
                raise ValueError(f'Fingerprint mismatch: {pair}')
            for name,h in marker['files'].items():
                p=(pair/name).resolve()
                if not p.is_relative_to(pair.resolve()) or digest(p)!=h:
                    raise ValueError(f'Invalid/changed artifact: {p}')
                checked+=1
            if {r['variant'] for r in marker['rows']}!=set(manifest['variants']) or len(marker['rows'])!=5:
                raise ValueError(f'Incomplete variants: {pair}')
            for row in marker['rows']:
                if row['case']!=item['case'] or row['num_cores']!=n:
                    raise ValueError('Mismatched row identity')
                result=unpack(pair/row['variant']/'result.json.gz')
                if result['makespan']!=row['makespan_cycles'] or result['data_movement_bytes']['added_copy_bytes']!=row['added_copy_bytes']:
                    raise ValueError('Metrics differ from saved official result')
                if digest(pair/row['variant']/'plan.json')!=row['plan_sha256']:
                    raise ValueError('Plan hash mismatch')
            rows.extend(marker['rows'])
    if missing and not allow_partial:
        raise ValueError(f'Incomplete experiment: {len(missing)} missing pairs; use --allow-partial to inspect')
    return {'status':'PARTIAL_VERIFIED' if missing else 'SUCCESS_VERIFIED',
            'verified_rows':len(rows),'requested_rows':manifest['expected_rows'],
            'missing_pairs':missing,'artifact_hashes_verified':checked}, rows


def run(output, workers, resume=False):
    output=Path(output).resolve(); manifest=official.read_json(output/'experiment.json')
    verify_sources(manifest)
    if platform.python_version()!=manifest['python_version']:
        raise ValueError('Python version changed; use a new output directory')
    if (output/'runs').exists() and not resume:
        raise ValueError('Existing run artifacts; pass --resume after checking the experiment')
    if type(workers) is not int or workers<1: raise ValueError('workers must be positive')
    lock=output/'run.lock'
    try:
        with lock.open('x',encoding='utf-8') as f: json.dump({'pid':os.getpid(),'host':platform.node(),'started_at':now()},f)
    except FileExistsError as error:
        raise ValueError('run.lock exists; verify its host/PID is no longer running before removing a stale lock') from error
    jobs=[(r['case'],n) for r in manifest['selected'] for n in manifest['cores']]
    failures=[]; rows=[]; started=time.perf_counter()
    try:
        write(output/'run_status.json',{'status':'RUNNING','started_at':now(),'workers':workers,'requested_rows':manifest['expected_rows']})
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures={pool.submit(job,case,n,str(output)):(case,n) for case,n in jobs}
            for future in as_completed(futures):
                case,n=futures[future]
                try:
                    rows.extend(future.result())
                    print(f'VERIFIED {case} N{n}: {len(rows)}/{manifest["expected_rows"]}',flush=True)
                except Exception as error:
                    failure={'case':case,'num_cores':n,'error':repr(error),'traceback':traceback.format_exc()}
                    failures.append(failure)
                    write(output/'failures'/f'{case}_N{n}_{time.time_ns()}.json',failure)
                    print(f'FAILED {case} N{n}: {error}',flush=True)
                write(output/'run_status.json',{'status':'RUNNING','verified_rows':len(rows),
                                                'requested_rows':manifest['expected_rows'],'failures':failures,
                                                'updated_at':now(),'elapsed_seconds':time.perf_counter()-started})
        status,rows=audit(output,allow_partial=True)
        rows.sort(key=lambda r:(r['case'],r['num_cores'],r['variant']))
        if rows:
            with (output/'details.csv').open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        write(output/'run_status.json',{**status,'failures':failures,'workers':workers,
                                       'elapsed_seconds':time.perf_counter()-started,'completed_at':now()})
        return status['status']=='SUCCESS_VERIFIED'
    finally:
        lock.unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('prepare','run'):
        p=sub.add_parser(command)
        p.add_argument('--output-dir',type=Path,required=True)
        p.add_argument('--data-dir',type=Path)
        p.add_argument('--config',type=Path)
        p.add_argument('--cases',nargs='+')
        p.add_argument('--cores',nargs='+',type=int)
        if command=='run':
            p.add_argument('--workers',type=int,default=2)
            p.add_argument('--resume',action='store_true')
    p=sub.add_parser('audit');p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--allow-partial',action='store_true')
    args=parser.parse_args(argv)
    if args.command=='audit':
        status,_=audit(args.output_dir,args.allow_partial);print(json.dumps(status,ensure_ascii=False,indent=2));return 0
    existing=(args.output_dir/'experiment.json').exists()
    if args.command=='prepare' or not existing:
        manifest=prepare(args.output_dir,args.data_dir or SRC/'official/data',args.config,args.cases,args.cores)
        print(json.dumps({'selected':[r['case'] for r in manifest['selected']],'expected_rows':manifest['expected_rows']},ensure_ascii=False))
    elif any(v is not None for v in (args.data_dir,args.config,args.cases,args.cores)):
        raise ValueError('Prepared experiment parameters are frozen; omit data/config/cases/cores on run, or choose a new output directory')
    if args.command=='prepare': return 0
    return 0 if run(args.output_dir,args.workers,args.resume) else 1


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (ValueError,AssertionError,OSError) as error:
        print(f'ERROR: {error}',file=sys.stderr)
        raise SystemExit(2)
