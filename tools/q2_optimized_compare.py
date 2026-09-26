"""Offline-only comparison of frozen Q2 pilot outputs and archived Q1/Q2 scores."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from common.artifacts import write_json, file_hash
from q2.official import read_json
from q2.optimized.api import frozen_output

ARCHIVES = {
    'q1_model1': 'results/q1_adaptive_100cases_20260925/summary.csv',
    'q1_model2': 'results/q1_stage2_100cases_20260926/summary.csv',
    'q1_model3': 'results/q1_structure_100cases_20260926/official_summary.csv',
    'q2_baseline': 'results/q2_repair_full_20260926/details.csv',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pilot', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive-revision', default='fd0c97834d14cbba0cda8a1ee73b541086f229ae')
    args = parser.parse_args()
    archives, sources = {}, {}
    for name, path in ARCHIVES.items():
        raw = subprocess.check_output(['git', 'show', f'{args.archive_revision}:{path}'], cwd=ROOT)
        data = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
        archives[name] = {(r['case'], int(r.get('cores') or r['num_cores'])): r for r in data}
        sources[name] = dict(path=path, sha256=hashlib.sha256(raw).hexdigest(), rows=len(data))
    selected, rows, evidence = {}, [], []
    for pilot in read_json(args.pilot/'pilot.json'):
        case, cores = pilot['case'], pilot['cores']; key = (case,cores)
        directory = args.pilot/f'{case}_N{cores}'
        snapshot = directory/'search'/f'N{cores}'
        manifest, state = frozen_output(snapshot)
        verified = read_json(directory/'replay/verified.json')
        if verified['frozen_plan_sha256'] != file_hash(snapshot/'plan.json'): raise ValueError('replay plan hash differs')
        if verified['result_sha256'] != file_hash(directory/'replay/result.json'): raise ValueError('replay result was modified')
        reference = {name: table[key] for name, table in archives.items()}
        selected[f'{case}/N{cores}'] = reference
        denominator = int(reference['q1_model3']['baseline_cycles'])
        if denominator != int(reference['q2_baseline']['singlecore_cycles']): raise ValueError('single-core denominators differ')
        checkpoint = read_json(snapshot/'checkpoint.json')
        logs = [json.loads(line) for line in (snapshot/'candidate_log.jsonl').read_text(encoding='utf-8').splitlines()]
        raw_predictions = [e['raw_refined_cycles'] for e in logs if e.get('state_signature') == state.state.state_signature
                           and e.get('raw_refined_cycles') is not None]
        cycles = verified['official_makespan']; movement = verified['data_movement_bytes']
        row = dict(case=case, cores=cores, singlecore_cycles=denominator)
        for name, history in reference.items():
            row[name+'_cycles'] = int(history.get('official_makespan_cycles') or history['makespan_cycles'])
            row[name+'_speedup'] = denominator/row[name+'_cycles']
            for field in ('added_copy_bytes','spill_added_copy_bytes'):
                row[name+'_'+field] = int(history[field]) if history.get(field) else None
        row.update(q2_optimized_cycles=cycles, q2_optimized_speedup=denominator/cycles,
            q2_optimized_added_copy_bytes=movement['added_copy_bytes'],
            q2_optimized_spill_added_copy_bytes=movement['spill_added_copy_bytes'],
            reduction_vs_q2_percent=100*(1-cycles/row['q2_baseline_cycles']),
            reduction_vs_q1v3_percent=100*(1-cycles/row['q1_model3_cycles']),
            search_seconds=pilot['search']['wall_seconds'], batches=pilot['search']['rows'][0]['batches'],
            final_source=state.score.source, selected_family=state.state.origin.family,
            selected_encoding=state.state.origin.encoding,
            raw_refined_cycles=raw_predictions[-1] if raw_predictions else None,
            raw_refined_error_cycles=raw_predictions[-1]-cycles if raw_predictions else None,
            official_search_calls=len(checkpoint['official_calls']),
            official_search_seconds=sum(c['wall_seconds'] for c in checkpoint['official_calls']),
            replay_seconds=sum(c['wall_seconds'] for c in verified['calls']),
            step1_builds=checkpoint['cache'].get('prefix_builds',0),
            coarse_evaluations=checkpoint['model_statistics'].get('coarse_evaluations',0),
            refined_evaluations=checkpoint['model_statistics'].get('refined_evaluations',0),
            baseline_guard_complete=checkpoint.get('baseline_guard',{}).get('complete',False),
            current_baseline_cycles=checkpoint.get('baseline_guard',{}).get('baseline_makespan'),
            plan_sha256=verified['frozen_plan_sha256'], source_signature=pilot['search']['source_signature'])
        rows.append(row)
        evidence.append(dict(case=case, cores=cores, manifest=manifest, verified=verified,
                             baseline_guard=checkpoint.get('baseline_guard'), cache=checkpoint['cache']))
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output/'comparison.csv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    write_json(args.output/'comparison.json',dict(archive_revision=args.archive_revision, sources=sources,
        historical_rows=selected, rows=rows, evidence=evidence,
        scope='four finite pairs; different machines and budgets; not a full benchmark',
        historical_scores_used_by_solver=False))
    print(json.dumps(rows,ensure_ascii=False,indent=2))


if __name__ == '__main__': main()
