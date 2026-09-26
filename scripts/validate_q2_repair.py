"""Bounded acceptance/ablation driver; reference answers are read only by reporting.

Default: one repaired case (086/N5). The 32-pair acceptance, ablations and
100x4 full batch each require an explicit --suite option.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import statistics
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CASES = ('001', '002', '003', '048', '059', '064', '067', '086')
TARGETS = (('048', 2), ('059', 5), ('067', 5), ('086', 5))
PROFILES = ('order', 'affinity', 'beta', 'capacity')


def load_runs(directory):
    result = {}
    for path in directory.glob('case_*/N*/run.json'):
        row = json.loads(path.read_text(encoding='utf-8'))
        result[row['case'], row['num_cores']] = row
    return result


def write_csv(path, rows):
    if not rows: return
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def report(root, destination, reference_zip=None):
    """This offline step alone may inspect archived multi-core measurements."""
    destination.mkdir(parents=True, exist_ok=True)
    old, new = load_runs(root/'legacy'), load_runs(root/'repair')
    reference = {}; reference_hash = None
    if reference_zip:
        reference_hash = hashlib.sha256(reference_zip.read_bytes()).hexdigest()
        with zipfile.ZipFile(reference_zip) as archive:
            text = archive.read('q2_repair_handoff/results/paired_comparison.csv').decode('utf-8-sig')
            reference = {(r['case'], int(r['num_cores'])): r for r in csv.DictReader(io.StringIO(text))}
    paired = []
    for key in sorted(old.keys() & new.keys()):
        a,b = old[key],new[key]
        paired.append(dict(case=key[0], cores=key[1], old_cycles=a['final_objective'][0],
                           new_cycles=b['final_objective'][0], old_b0_cycles=a['baseline_objective'][0],
                           old_speedup=a['speedup'], new_speedup=b['speedup'],
                           reduction=1-b['final_objective'][0]/a['final_objective'][0],
                           old_added_bytes=a['data_movement_bytes']['added_copy_bytes'],
                           new_added_bytes=b['data_movement_bytes']['added_copy_bytes'],
                           new_spill_bytes=b['data_movement_bytes']['spill_added_copy_bytes'],
                           old_seconds=a['wall_seconds'], new_seconds=b['wall_seconds'],
                           old_official_calls=a['official_evaluations'], new_official_calls=b['official_evaluations'],
                           old_peak_rss=a['peak_rss_bytes'], new_peak_rss=b['peak_rss_bytes'],
                           old_official_peak_rss=a['official_peak_rss_bytes'], new_official_peak_rss=b['official_peak_rss_bytes'],
                           family=b['selected_candidate']['family'], encoding=b['selected_candidate']['encoding'],
                           beta=b['selected_candidate']['beta'],
                           search_complete=b.get('search_complete',False),
                           reference_new_cycles=int(reference[key]['new_makespan']) if key in reference else None,
                           legacy_b0_matches_reference=(a['baseline_objective'][0] == int(reference[key]['old_B0_makespan'])) if key in reference else None,
                           final_matches_reference=(b['final_objective'][0] == int(reference[key]['new_makespan'])) if key in reference else None))
    by_core = []
    for n in (2,3,4,5):
        rows = [r for r in paired if r['cores'] == n]
        if not rows: continue
        by_core.append(dict(cores=n, cases=len(rows),
                            old_mean_speedup=statistics.mean(r['old_speedup'] for r in rows),
                            new_mean_speedup=statistics.mean(r['new_speedup'] for r in rows),
                            old_mean_seconds=statistics.mean(r['old_seconds'] for r in rows),
                            new_mean_seconds=statistics.mean(r['new_seconds'] for r in rows),
                            old_mean_added_mib=statistics.mean(r['old_added_bytes']/1048576 for r in rows),
                            new_mean_added_mib=statistics.mean(r['new_added_bytes']/1048576 for r in rows),
                            better=sum(r['new_cycles']<r['old_cycles'] for r in rows),
                            tie=sum(r['new_cycles']==r['old_cycles'] for r in rows),
                            worse=sum(r['new_cycles']>r['old_cycles'] for r in rows)))
    ablation_profiles = [name for name in PROFILES if load_runs(root/name)]
    ablations=[]
    for name in (('legacy', *PROFILES, 'repair') if ablation_profiles else ()):
        for key,row in sorted(load_runs(root/name).items()):
            if (key[0].split('_')[1],key[1]) not in TARGETS: continue
            ablations.append(dict(profile=name,case=key[0],cores=key[1],
                                  cycles=row['final_objective'][0], added_bytes=row['final_objective'][1],
                                  spill_bytes=row['data_movement_bytes']['spill_added_copy_bytes'],
                                  seconds=row['wall_seconds'], official_calls=row['official_evaluations'],
                                  search_complete=row.get('search_complete',False)))
    stop_path = root/'USER_STOP.json'
    user_stop = json.loads(stop_path.read_text(encoding='utf-8')) if stop_path.exists() else None
    summary=dict(scope='partial paired intersection; no claim of full benchmark or ablation completion',
                 paired=len(paired), new_verified=len(new), old_verified=len(old),
                 user_stop=user_stop, ablation_profiles_executed=ablation_profiles,
                 by_core=by_core, reference_zip_sha256=reference_hash,
                 mean_relative_reduction=statistics.mean(r['reduction'] for r in paired) if paired else None,
                 legacy_b0_matches_reference=sum(r['legacy_b0_matches_reference'] is True for r in paired),
                 final_matches_reference=sum(r['final_matches_reference'] is True for r in paired),
                 search_complete=sum(r.get('search_complete',False) for r in new.values()),
                 official_calls=sum(r['official_evaluations'] for r in new.values()),
                 candidate_official_calls=sum(c.get('launched',False) for r in new.values()
                                              for c in r['official_calls'] if c['phase']!='final_verification'),
                 independent_verifications=sum(c.get('status')=='OK' for r in new.values()
                                               for c in r['official_calls'] if c['phase']=='final_verification'),
                 capacity_windows=sum(len(r['windows']) for r in new.values()),
                 capacity_feasible_windows=sum(w.get('status')=='OPTIMAL_IN_FIXED_WINDOW'
                                                for r in new.values() for w in r['windows']),
                 accepted_capacity_windows=sum(r['accepted_windows'] for r in new.values()),
                 dp_states=sum(w.get('states',0) for r in new.values() for w in r['windows']),
                 dp_transitions=sum(w.get('transitions',0) for r in new.values() for w in r['windows']),
                 dp_events_scanned=sum(w.get('events_scanned',0) for r in new.values() for w in r['windows']),
                 regressions=[r for r in paired if r['new_cycles']>r['old_cycles']],
                 incomplete=[dict(case=r['case'],cores=r['num_cores'],reason=r['stop_reason'])
                             for r in new.values() if not r.get('search_complete',False)],
                 runtime_note='wall time through final verification, before final metadata/snapshot export; process lifetime peak RSS may span cases; not exclusive-host measurements')
    (destination/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    write_csv(destination/'paired_comparison.csv',paired)
    write_csv(destination/'ablations.csv',ablations)
    identities={}
    for name in ('legacy', *PROFILES, 'repair'):
        rows=load_runs(root/name)
        if rows:
            key=next(iter(rows));p=root/name/key[0]/f'N{key[1]}'/'provenance.json'
            identities[name]=json.loads(p.read_text(encoding='utf-8'))
    (destination/'provenance.json').write_text(json.dumps(identities,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--output-root',type=Path,required=True)
    parser.add_argument('--suite',choices=('smoke','acceptance','ablations','full'),default='smoke')
    parser.add_argument('--workers',type=int,default=1)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--report-only',action='store_true')
    parser.add_argument('--report-dir',type=Path)
    parser.add_argument('--reference-zip',type=Path)
    args=parser.parse_args();root=args.output_root.resolve();root.mkdir(parents=True,exist_ok=True)
    if not args.report_only:
        if args.data_dir is None:parser.error('--data-dir required when running')
        jobs=[]
        if args.suite=='smoke':
            jobs=[('repair','baseline',('086',),(5,))]
        elif args.suite=='acceptance':
            jobs=[('legacy','legacy_1_1',CASES,(2,3,4,5)),('repair','baseline',CASES,(2,3,4,5))]
        elif args.suite=='ablations':
            jobs=[(p,'ablation_'+p,(case,),(n,)) for p in PROFILES for case,n in TARGETS]
        else:
            jobs=[('repair','baseline',tuple(f'{i:03d}' for i in range(1,101)),(2,3,4,5))]
        for name,config,cases,cores in jobs:
            command=[sys.executable,'-X','utf8',str(ROOT/'src/solve_problem2.py'),'batch',
                     '--data-dir',str(args.data_dir.resolve()),'--cases',*('case_'+s for s in cases),
                     '--cores',*map(str,cores),'--workers',str(args.workers),
                     '--algorithm-config',str(ROOT/f'configs/q2/{config}.json'),
                     '--output-dir',str(root/name)]
            if args.resume:command.append('--resume')
            print(subprocess.list2cmdline(command),flush=True)
            with (root/f'{name}_{cases[0]}.log').open('w',encoding='utf-8') as log:
                subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
    report(root,args.report_dir or root/'comparison',args.reference_zip)


if __name__=='__main__':main()
