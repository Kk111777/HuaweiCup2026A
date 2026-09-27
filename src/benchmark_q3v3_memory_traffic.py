"""Frozen stratified-20 regression, explicit full seeds and host concurrency."""
from concurrent.futures import ProcessPoolExecutor,as_completed
from datetime import datetime,timezone
from pathlib import Path
import argparse
import ctypes
import json
import os
import shutil
import time
import traceback

from q3v3 import ALGORITHM_NAME,ALGORITHM_REVISION
from q3v3.official import ROOT,sha256,write_json
from q3v3.search import solve_one


def read(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def worker(job):
    seeds=job['seeds']
    host=dict(pid=os.getpid(),started_ns=time.time_ns(),affinity_mask=None)
    if os.name=='nt':
        process_mask=ctypes.c_size_t();system_mask=ctypes.c_size_t()
        if ctypes.windll.kernel32.GetProcessAffinityMask(ctypes.c_void_p(-1),
                ctypes.byref(process_mask),ctypes.byref(system_mask)):
            host['affinity_mask']=hex(process_mask.value)
    try:
        row,_=solve_one(job['graph'],job['cores'],job['config'],job['output'],
            warm_plan=seeds['q1']['plan'],q2_warm_plan=seeds['q2']['plan'],
            reference_plan=seeds['previous_q3']['plan'],prepared_seeds=seeds,
            objective_policy='cycles-traffic',seconds=job['seconds'],max_evaluations=job['max_evaluations'],
            evaluation_timeout=600,cycle_tolerance=job['cycle_tolerance'],
            traffic_weight=job['traffic_weight'],added_share=job['added_share'])
    finally:
        host['finished_ns']=time.time_ns()
        write_json(Path(job['output'])/'host_process.json',host)
    row['host_process']=host
    return row


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--prepared-seeds',type=Path,
        help='Previously completed 20-case seed replay manifest; exact hashes are revalidated.')
    parser.add_argument('--workers',type=int,default=20)
    parser.add_argument('--seconds',type=float,default=120)
    parser.add_argument('--max-evaluations',type=int,default=64)
    parser.add_argument('--cycle-tolerance',type=float,default=.01)
    parser.add_argument('--traffic-weight',type=float,default=.05)
    parser.add_argument('--added-share',type=float,default=.25)
    args=parser.parse_args()
    if args.workers<1: parser.error('workers must be positive')
    out=args.output_dir.resolve()
    if (out/'run_info.json').exists(): raise ValueError('refuse overwriting an earlier batch')
    prepared_path=(args.prepared_seeds or out/'prepared_seeds.json').resolve()
    prepared=read(prepared_path)
    if prepared['status']!='complete' or prepared['completed']!=20:
        raise ValueError('all 20 seed groups must finish independent replay first')
    out.mkdir(parents=True,exist_ok=True)
    if prepared_path!=out/'prepared_seeds.json':
        shutil.copyfile(prepared_path,out/'prepared_seeds.json')
    jobs=[]
    for source in prepared['jobs']:
        job=dict(source,output=str(out/source['case']/f"N{source['cores']}"),
            seconds=args.seconds,max_evaluations=args.max_evaluations,
            cycle_tolerance=args.cycle_tolerance,traffic_weight=args.traffic_weight,added_share=args.added_share)
        if Path(job['output']).exists(): raise ValueError('refuse overwriting an earlier run')
        jobs.append(job)
    sources=sorted((ROOT/'src/q3v3').glob('*.py'))+sorted((ROOT/'official/code').glob('*.py'))+[
        Path(__file__).resolve(),ROOT/'src/solve_q3v3.py',ROOT/'src/report_q3v3.py']
    hashes={str(p.relative_to(ROOT)):sha256(p) for p in sources}
    for p in sources:
        target=out/'source_snapshot'/p.relative_to(ROOT)
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,target)
    t=time.perf_counter()
    info=dict(status='running',algorithm=ALGORITHM_NAME,revision=ALGORITHM_REVISION,
        started_at=datetime.now(timezone.utc).isoformat(),workers=args.workers,logical_cpus=os.cpu_count(),
        expected=20,completed=0,source_hashes=hashes,jobs=jobs,
        graphs=[j['graph'] for j in jobs],config=jobs[0]['config'],
        previous=str(ROOT/'outputs/q3v3_q1struct_l2_20_20260927'),
        seed_preparation_wall_seconds=prepared['wall_seconds'],
        prepared_seeds_source=str(prepared_path),
        settings=dict(seconds=args.seconds,max_evaluations=args.max_evaluations,
            cycle_tolerance=args.cycle_tolerance,traffic_weight=args.traffic_weight,added_share=args.added_share),
        seed_policy='Q1 + published Q2v3 + previous Q3 complete plans; all freshly replayed in Q3',
        no_q1_or_q2_search_rerun=True)
    write_json(out/'run_info.json',info)
    rows=[]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(worker,j):j for j in jobs}
        for future in as_completed(futures):
            job=futures[future]
            try: row=future.result()
            except Exception as exc:
                row=dict(case=job['case'],num_cores=job['cores'],status='failed',error=str(exc),traceback=traceback.format_exc())
                write_json(Path(job['output'])/'failure.json',row)
            rows.append(row)
            info.update(completed=sum(r['status']=='ok' for r in rows),finished=len(rows),elapsed_seconds=time.perf_counter()-t)
            write_json(out/'summary.json',rows);write_json(out/'run_info.json',info)
            print(json.dumps(dict(case=row['case'],N=row['num_cores'],status=row['status'],
                cycles=row.get('makespan_cycles'),ddr=row.get('physical_ddr_bytes'),winner=row.get('winner'),
                error=row.get('error'),finished=len(rows)),ensure_ascii=False),flush=True)
    info['sources_unchanged']=all(sha256(ROOT/name)==h for name,h in hashes.items())
    info['status']='complete' if info['completed']==20 and info['sources_unchanged'] else 'failed'
    info['elapsed_seconds']=time.perf_counter()-t
    write_json(out/'run_info.json',info)
    return 0 if info['status']=='complete' else 1


if __name__=='__main__': raise SystemExit(main())
