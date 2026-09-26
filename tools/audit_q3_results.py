"""Read-only cross-check of published Q3 evidence, coverage and exported plans."""
import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
from statistics import fmean
import sys
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from q2.official import digest, read_json
from q3.artifacts import file_hash, write_json
from q3.official import Settings, objective
from q3.pipeline import source_hashes, verified_directory
from q3.report import METRICS


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit(output, data_dir, allow_partial=False):
    output, data_dir = Path(output).resolve(), Path(data_dir).resolve()
    info = read_json(output / 'run_info.json')
    settings = Settings.read()
    require(info['source_hashes'] == source_hashes(), 'implementation differs from recorded run')
    for name, expected_hash in info['q2_input_hashes'].items():
        path = Path(name)
        if not path.is_file() and '/results/' in name.replace('\\', '/'):
            # Bundled baseline archives can be checked after moving the project.
            path = ROOT / 'results' / name.replace('\\', '/').split('/results/', 1)[1]
        require(file_hash(path) == expected_hash, f'Q2 prerequisite source changed: {name}')
    require(len(set(info['cores'])) == len(info['cores']), 'duplicate requested core count')
    expected = {(Path(name).stem, n) for name in info['input_sha256'] for n in info['cores']}
    require(info['expected'] == len(expected), 'requested coverage is inconsistent')
    rows, markers, case_times = {}, {}, {}
    for filename, expected_hash in info['input_sha256'].items():
        graph = data_dir / filename
        require(file_hash(graph) == expected_hash, f'input changed: {filename}')
        case_name = Path(filename).stem
        directory = output / case_name
        if not directory.exists():
            continue
        require(verified_directory(directory), f'case manifest invalid: {case_name}')
        case = read_json(directory / 'case.json')
        identity = case['identity']
        require(case['fingerprint'] == digest(identity), f'identity digest: {case_name}')
        require(identity['input_sha256'] == expected_hash, f'case input: {case_name}')
        require(identity['source_hashes'] == info['source_hashes'], f'case source: {case_name}')
        require(identity['q2_input_hashes'] == info['q2_input_hashes'], f'case Q2 source: {case_name}')
        require(identity['options'] == info['options'], f'case search options: {case_name}')
        require(identity['official'] == settings.scene.hashes, f'official environment: {case_name}')
        require(identity['q3_environment'] == settings.namespace, f'Q3 environment: {case_name}')
        require(sorted(identity['cores']) == sorted(info['cores']), f'case core requests: {case_name}')
        require(case['q3_seconds'] <= info['options']['seconds'], f'case budget exceeded: {case_name}')
        require(case['status'] == 'SUCCESS_VERIFIED', f'case status: {case_name}')
        case_times[case_name] = case['q3_seconds']
        markers[case_name] = file_hash(directory / 'complete.json')
        for row in case['rows']:
            key = row['case'], row['num_cores']
            require(key in expected and key not in rows and key[0] == case_name, f'coverage duplicate/mismatch: {key}')
            require(row['status'] == 'SUCCESS_VERIFIED', f'row status: {key}')
            core_dir = directory / f'N{key[1]}'
            plan_path = core_dir / f'{case_name}_multicore_res.json'
            plan = read_json(plan_path)
            require(set(plan) == {'node_to_subgraph', 'core_schedules'}, f'plan fields: {key}')
            require(len(plan['core_schedules']) == key[1], f'plan cores: {key}')
            require(file_hash(plan_path) == row['plan_sha256'], f'plan hash: {key}')
            result_path = core_dir / 'verified/result.json'
            require(file_hash(result_path) == row['result_sha256'], f'result hash: {key}')
            result = read_json(result_path)
            seed = read_json(core_dir / 'seed_l2/result.json')
            q2_source = read_json(core_dir / 'q2_source.json')
            require(file_hash(core_dir / 'q2_plan.json') == q2_source['imported_plan_sha256'], f'imported Q2 plan hash: {key}')
            if q2_source.get('frozen_objective'):
                reference = read_json(core_dir / 'no_l2/reference.json')
                q2_objective = tuple(reference['objective'])
                require(q2_objective == tuple(q2_source['expected_objective']), f'Q2 reference: {key}')
            else:
                q2_objective = objective(read_json(core_dir / 'no_l2/result.json'))
            require(q2_objective == (row['t2_cycles'], row['no_l2_added_copy_bytes']), f'Q2 metric: {key}')
            require(objective(seed)[0] == row['t2_to_3_cycles'], f'seed metric: {key}')
            require(objective(seed)[1] == q2_objective[1], f'L2 changed official copy bytes: {key}')
            require(objective(result) == (row['t3_cycles'], row['added_copy_bytes']), f'final metric: {key}')
            require(objective(result) <= objective(seed), f'lexicographic regression: {key}')
            for name in ('scheduled_copy_bytes', 'spill_added_copy_bytes'):
                require(row[name] == result['data_movement_bytes'][name], f'copy metric {name}: {key}')
            for column, field in (('cache_hit_bytes', 'hit_bytes'), ('cache_miss_bytes', 'miss_bytes'),
                                  ('cache_hit_rate_bytes', 'hit_rate')):
                require(row[column] == result['cache_stats'][field], f'cache metric {column}: {key}')
            physical = read_json(core_dir / 'audit.json')
            require(physical['status'] == 'PASSED', f'physical audit: {key}')
            require(physical['actual_ddr_bytes'] == row['actual_ddr_bytes'], f'DDR metric: {key}')
            for name, numerator, denominator in (
                ('hardware_speedup', row['t2_cycles'], row['t2_to_3_cycles']),
                ('algorithm_speedup', row['t2_to_3_cycles'], row['t3_cycles']),
                ('cache_speedup', row['t2_cycles'], row['t3_cycles']),
            ):
                ratio = numerator / denominator if denominator else (1.0 if numerator == 0 else None)
                require(row[name] == ratio, f'ratio {name}: {key}')
            rows[key] = row
        selected = sorted((k[1], r['t3_cycles']) for k, r in rows.items() if k[0] == case_name)
        require([n for n, _ in selected] == sorted(info['cores']), f'incomplete published case: {case_name}')
        require(all(a[1] >= b[1] for a, b in zip(selected, selected[1:])), f'core monotonicity: {case_name}')
    missing = sorted(expected - rows.keys())
    summary = read_json(output / 'summary.json')
    if not allow_partial:
        require(not missing and not summary['failed'], 'batch is incomplete or failed')
        require(info['status'] == 'COMPLETE' and info['sources_unchanged'], 'batch status/source guard not complete')
        require(summary['complete'] and summary['successful'] == len(expected), 'summary coverage mismatch')
        for n in info['cores']:
            group = [r for (_, cores), r in rows.items() if cores == n]
            stats = summary['by_core'][str(n)]
            require(stats['successful'] == len(group) and stats['complete'], f'summary core coverage: {n}')
            require(stats['improved'] == sum(r['t3_cycles'] < r['t2_to_3_cycles'] for r in group), f'improvement count: {n}')
            for metric in METRICS:
                require(math.isclose(stats['mean_' + metric], fmean(r[metric] for r in group), rel_tol=1e-12),
                        f'mean mismatch: {n}/{metric}')
        with (output / 'details.csv').open(encoding='utf-8', newline='') as stream:
            exported = list(csv.DictReader(stream))
        require(len(exported) == len(rows), 'CSV row count mismatch')
        seen = set()
        for exported_row in exported:
            key = exported_row['case'], int(exported_row['num_cores'])
            require(key in rows and key not in seen, f'CSV coverage mismatch: {key}')
            seen.add(key)
            for name, value in rows[key].items():
                require(exported_row[name] == ('' if value is None else str(value)), f'CSV metric mismatch: {key}/{name}')
        manifest = read_json(output / 'report_artifacts.json')
        for name, expected_hash in manifest.items():
            path = (output / name).resolve()
            require(path.is_relative_to(output) and file_hash(path) == expected_hash, f'report artifact hash: {name}')
        with ZipFile(output / 'plans.zip') as archive:
            names = {f'N{n}/{case}_multicore_res.json': row for (case, n), row in rows.items()}
            require(len(archive.namelist()) == len(names) and set(archive.namelist()) == names.keys(), 'plan ZIP coverage mismatch')
            require(archive.testzip() is None, 'plan ZIP CRC failed')
            for name, row in names.items():
                require(hashlib.sha256(archive.read(name)).hexdigest() == row['plan_sha256'], f'plan ZIP hash: {name}')
    return dict(status='PARTIAL_VERIFIED' if allow_partial else 'PASSED', requested=len(expected),
                verified=len(rows), published_cases=len(markers), missing=missing, summary_failures=summary['failed'],
                input_source_official_hashes_match=True, all_published_manifest_hashes_match=True,
                final_plan_result_metrics_match=True, lexicographic_nonregression=True, core_time_nonincreasing=True,
                max_case_q3_seconds=max(case_times.values(), default=0),
                improved_groups=sum(r['t3_cycles'] < r['t2_to_3_cycles'] for r in rows.values()),
                stop_reasons=dict(Counter(r['stop_reason'] for r in rows.values())),
                q2_source_kinds=dict(Counter(r['q2_source_kind'] for r in rows.values())),
                case_completion_sha256=markers, run_info_sha256=file_hash(output / 'run_info.json'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'official/data')
    parser.add_argument('--allow-partial', action='store_true')
    parser.add_argument('--audit-output', type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.output_dir, args.data_dir, args.allow_partial)
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({'status': 'FAILED', 'error': str(error)}, ensure_ascii=False))
        return 1
    if args.audit_output:
        require(not args.audit_output.exists(), 'audit output already exists; choose a new file')
        write_json(args.audit_output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in ('missing', 'case_completion_sha256')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
