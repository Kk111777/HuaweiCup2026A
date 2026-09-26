"""Score frozen final plans outside the search clock; never feed results back."""
import argparse
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from q1 import official
from q1.plan import normalized, objective, plan_digest
from scheduling.io import digest, read, write


def score_case(run, graph, config, cores, output):
    run, output = Path(run), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('score output directory must be empty')
    raw, settings = read(graph), official.Settings.read(config)
    rows = []
    # Freeze every submitted input before any evaluation. No winner selection occurs here.
    inputs = []
    for n in cores:
        folder = run/f'N{n}' if (run/f'N{n}').exists() else run
        plan = read(folder/'plan.json')
        if len(plan['core_schedules']) != n:
            raise ValueError(f'wrong core count for {folder}')
        inputs.append((n, folder, plan, digest(folder/'plan.json')))
    for n, folder, plan, plan_hash in inputs:
        started = perf_counter()
        actual = official.evaluate(raw, plan, settings)
        seconds = perf_counter()-started
        assert digest(folder/'plan.json') == plan_hash, 'final plan changed during scoring'
        write(output/f'N{n}'/'official_score.json', actual)
        saved, summary = read(folder/'evaluation.json'), read(folder/'summary.json')
        exact_match = normalized(saved) == normalized(actual)
        selection = summary.get('final_selection', {})
        history = read(folder/'search.json') if (folder/'search.json').exists() else None
        measured = [r for r in history['records'] if r.get('official_objective')] if history else []
        best = min((r['official_objective'] for r in measured), default=None)
        best_match = list(objective(actual)) == best if best else None
        baseline = summary.get('baseline', {}).get('cycles')
        row = dict(cores=n, official_cycles=actual['makespan'],
            official_added_copy_bytes=objective(actual)[1],
            speedup=baseline/actual['makespan'] if baseline and actual['makespan'] else None,
            search_seconds=summary['wall_seconds'], scoring_seconds=seconds,
            scoring_in_search_budget=False, plan_sha256=plan_hash,
            plan_digest=plan_digest(plan), exact_result_match=exact_match,
            selected_best_official=selection.get('objective') == list(objective(actual)),
            matches_search_history_best=best_match,
            search_official_calls=sum(r.get('evaluation_source') == 'official' and not r.get('cache_hit')
                                      for r in history['records']) if history else None)
        rows.append(row)
        write(output/'scores.json', dict(rows=rows, graph_sha256=digest(graph), config_sha256=digest(config),
            independent_official_scoring=True, scoring_in_search_budget=False))
        if saved.get('evaluation_source') != 'approximate' and not exact_match:
            raise ValueError(f'official scoring disagrees with saved result: N{n}')
        if selection.get('policy') == 'best_official' and (not row['selected_best_official'] or best_match is False):
            raise ValueError(f'final output is not the recorded official best: N{n}')
        print(f'N{n}: {actual["makespan"]} cycles; score time {seconds:.3f}s; full match={exact_match}', flush=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'graph', 'config', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--cores', type=int, nargs='+', default=[2, 3, 4, 5])
    args = parser.parse_args()
    score_case(args.run, args.graph, args.config, args.cores, args.output)


if __name__ == '__main__':
    main()
