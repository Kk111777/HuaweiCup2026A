"""Bounded four-pair acceptance. Historical results are not solver inputs."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from common.artifacts import write_json
from q2.optimized.api import solve_case, replay

PAIRS = (('004', 2), ('010', 5), ('059', 5), ('086', 5))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, default=ROOT/'official/data')
    parser.add_argument('--config', type=Path, default=ROOT/'official/data/config.txt')
    parser.add_argument('--pairs', nargs='+', help='Subset of the four accepted pairs, e.g. 086/N5')
    args = parser.parse_args()
    pairs = PAIRS if args.pairs is None else tuple((s.split('/N')[0], int(s.split('/N')[1])) for s in args.pairs)
    if any(p not in PAIRS for p in pairs) or len(set(pairs)) != len(pairs): parser.error('distinct approved pilot pairs required')
    if args.output.exists(): parser.error('use a fresh output directory; prior evidence must be retained')
    args.output.mkdir(parents=True)
    rows = []
    for case, cores in pairs:
        directory = args.output/f'case_{case}_N{cores}'
        graph = args.data_dir/f'case_{case}.json'
        print(f'RUN case_{case}/N{cores}', flush=True)
        result = solve_case(graph, args.config, [cores], directory/'search', seconds=600.,
                            options={'max_batches':2})
        record = dict(case=f'case_{case}', cores=cores, search=result)
        if result['rows'][0].get('makespan') is not None:
            record['replay'] = replay(graph, args.config, directory/'search'/f'N{cores}', directory/'replay')
        rows.append(record)
        write_json(args.output/'pilot.json', rows)
        print(json.dumps(dict(case=case, cores=cores, status=result['status'],
            predicted=result['rows'][0]['makespan'], official=record.get('replay',{}).get('official_makespan'),
            seconds=result['wall_seconds'], error=result.get('error')), ensure_ascii=False), flush=True)
    return int(any(r['search']['status'] != 'ok' or r.get('replay',{}).get('status') != 'OFFICIAL_REPLAY_OK' for r in rows))


if __name__ == '__main__': raise SystemExit(main())
