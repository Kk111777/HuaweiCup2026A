"""Local-only independent-case parallel benchmark; every case/core solve stays serial."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import shutil
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from common.baseline import load_baseline_catalog
from q1 import official
from q1.unified.api import solve_file, source_signature, default_parameters
from q1.unified.io import read, write, digest
from problem_1_baseline import output_lock


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,required=True)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--cases',type=int,nargs='+',default=list(range(1,101)))
    p.add_argument('--cores',type=int,nargs='+',default=[2,3,4,5])
    p.add_argument('--local-test-workers',type=int,default=1)
    p.add_argument('--local-test-baseline-archive',type=Path)
    p.add_argument('--archive-repo-root',type=Path)
    p.add_argument('--seconds',type=float,default=600)
    p.add_argument('--resume',action='store_true')
    p.add_argument('--reuse-verified-run',type=Path,nargs='*',default=[])
    a=p.parse_args()
    if not 1<=a.local_test_workers<=24:p.error('workers must be 1..24')
    if len(set(a.cases))!=len(a.cases):p.error('duplicate cases')
    a.output.mkdir(parents=True,exist_ok=True)
    with output_lock(a.output):
        return run(a)


def run(a):
    paths=[(a.data/f'case_{i:03d}.json').resolve() for i in a.cases]
    # Local batch scheduling only: start large independent inputs early to
    # reduce the tail. This does not affect a solver's candidate decisions.
    paths.sort(key=lambda path:(-path.stat().st_size,path.name))
    a.output.mkdir(parents=True,exist_ok=True)
    signature=source_signature()
    baseline={}
    if a.local_test_baseline_archive:
        archive=a.local_test_baseline_archive.resolve()
        catalog=load_baseline_catalog(archive,paths,a.config,official.CODE,repo_root=a.archive_repo_root or archive.parent.parent)
        with (archive/'summary.csv').open(encoding='utf-8-sig',newline='') as stream:
            times={r['case']:float(r['runtime_seconds']) for r in csv.DictReader(stream)}
        for path in paths:
            entry=catalog[path.stem]
            baseline[path.stem]=dict(entry,result_path=str(archive/entry['baseline_ref']['result']),
                result_sha256=entry['baseline_ref']['result_sha256'],historical_seconds=times[path.stem],
                graph_sha256=digest(path),config_sha256=digest(a.config))
    started=time.perf_counter()
    manifest=dict(source_signature=signature,cases=a.cases,cores=a.cores,
        local_test_workers=a.local_test_workers,within_case_parallel=False,
        job_order=[path.stem for path in paths],job_order_policy='largest_input_first',
        test_baseline_cache=bool(baseline),target_seconds=a.seconds,
        note='Historical singlecore wall time is charged to the search budget when cached; production recomputes it.')
    write(a.output/'benchmark.json',manifest)
    def reusable(folder,path):
        required=('summary.json','request.json','verified.json','plan.json','evaluation.json')
        if not all((folder/name).is_file() for name in required):return False
        result=read(folder/'summary.json');req=read(folder/'request.json');marker=read(folder/'verified.json')
        cached=req.get('test_baseline',{})
        expected=baseline.get(path.stem,{})
        return (result['status']=='ok' and result['source_signature']==signature
            and req['graph_sha256']==digest(path) and req['config_sha256']==digest(a.config)
            and req['seconds']==a.seconds and req['parameters']==default_parameters()
            and result['local_test_cache']==bool(baseline)
            and cached.get('historical_seconds')==expected.get('historical_seconds')
            and cached.get('baseline_ref',{}).get('manifest_sha256')==expected.get('baseline_ref',{}).get('manifest_sha256')
            and marker.get('independent_official_replay') is True
            and marker.get('source_signature')==signature
            and (result.get('score_source')!='approximate' or marker.get('official_replay_sha256'))
            and (not marker.get('official_replay_sha256') or ((folder/'official_replay.json').exists()
                 and marker['official_replay_sha256']==digest(folder/'official_replay.json')))
            and marker.get('plan_sha256')==digest(folder/'plan.json')
            and marker.get('result_sha256')==digest(folder/'evaluation.json'))
    def run_case(path):
        rows=[]
        for cores in a.cores:
            folder=a.output/path.stem/f'N{cores}'
            reused=False
            if not folder.exists():
                for previous in a.reuse_verified_run:
                    old=previous/path.stem/f'N{cores}'
                    if reusable(old,path):
                        folder.parent.mkdir(parents=True,exist_ok=True)
                        shutil.copytree(old,folder)
                        write(folder/'reuse.json',dict(source=str(old.resolve()),
                            source_signature=signature,reason='same version and independently replayed'))
                        reused=True
                        break
            if a.resume and (folder/'summary.json').exists():
                reused=reusable(folder,path)
            if reused:
                result=read(folder/'summary.json')
            if not reused:
                if folder.exists() and a.resume:
                    # Preserve failed, incomplete, changed-version or tampered attempts.
                    # Both resolved paths must stay under this explicit output root.
                    root=a.output.resolve()
                    archived=root/'attempts'/path.stem/f'N{cores}'/str(time.time_ns())
                    if not folder.resolve().is_relative_to(root) or not archived.resolve().is_relative_to(root):
                        raise ValueError('attempt archival escaped the output directory')
                    archived.parent.mkdir(parents=True,exist_ok=True)
                    shutil.move(str(folder.resolve()),str(archived))
                result=solve_file(path,a.config,cores,folder,seconds=a.seconds,
                    local_test_baseline_archive=a.local_test_baseline_archive,
                    archive_repo_root=a.archive_repo_root,_verified_test_record=baseline.get(path.stem))
            row=dict(case=path.stem,cores=cores,reused=reused,**result)
            rows.append(row)
            print(json.dumps({k:row.get(k) for k in ('case','cores','status','makespan','score_source','wall_seconds','accounted_wall_seconds','cpu_seconds','hard_timeout','reused')}),flush=True)
        return rows
    rows=[];errors=[]
    with ThreadPoolExecutor(max_workers=a.local_test_workers) as pool:
        jobs={pool.submit(run_case,path):path for path in paths}
        for future in as_completed(jobs):
            try:rows.extend(future.result())
            except Exception as e:errors.append(dict(case=jobs[future].stem,error=str(e)))
            write(a.output/'results.json',dict(status='running',rows=rows,errors=errors))
    rows.sort(key=lambda r:(r['case'],r['cores']))
    fields=['case','cores','status','makespan','score_source','speedup','wall_seconds','accounted_wall_seconds',
        'cpu_seconds','cached_baseline_charge_seconds','exceeded_target','extended','exceeded_extension',
        'hard_timeout','timed_out_phase','history_stop','local_test_cache','reused']
    with (a.output/'timings.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        w=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
    expected=len(paths)*len(a.cores)
    successful=sum(r['status']=='ok' for r in rows)
    write(a.output/'results.json',dict(status='complete' if not errors and len(rows)==expected else 'incomplete',rows=rows,
        coverage_complete=len(rows)==expected,expected_results=expected,successful_results=successful,
        hard_timeouts=sum(r['hard_timeout'] for r in rows),
        failed_or_timed_out=[dict(case=r['case'],cores=r['cores'],status=r['status']) for r in rows if r['status']!='ok'],
        errors=errors,elapsed_seconds=time.perf_counter()-started,manifest=manifest))
    return bool(errors or successful!=expected)


if __name__=='__main__':raise SystemExit(main())
