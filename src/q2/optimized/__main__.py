"""Q2 optimization CLI; the existing q2 baseline CLI remains independent."""
import argparse
import json
from pathlib import Path
import sys

from q2.official import ROOT, read_json
from .api import solve_case, batch, replay
from .control import parameters


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ('solve', 'batch', 'replay', '-h', '--help'): argv.insert(0, 'solve')
    parser = argparse.ArgumentParser(description='Scene B joint optimization; one case budget across all requested core counts')
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('solve', 'batch', 'replay'):
        p = commands.add_parser(command)
        p.add_argument('--config', type=Path, default=ROOT/'official/data/config.txt')
        p.add_argument('--output', type=Path, required=True)
        if command != 'batch': p.add_argument('graph', type=Path)
        if command == 'replay':
            p.add_argument('--snapshot', type=Path, required=True)
            p.add_argument('--timeout', type=float, default=600)
            continue
        p.add_argument('--cores', type=int, nargs='+', default=[2,3,4,5])
        p.add_argument('--seconds', type=float, default=600)
        p.add_argument('--parameters', type=Path)
        p.add_argument('--calibration', choices=['periodic', 'off'])
        p.add_argument('--baseline-guard', choices=['on', 'off'], help='Default on: complete current baseline and publish only exact non-regressing choices')
        p.add_argument('--output-policy', choices=['search', 'best-exact'])
        p.add_argument('--max-batches', type=int, help='Finite smoke run; omitted for normal stagnation/budget termination')
        if command == 'batch':
            p.add_argument('--data-dir', type=Path, required=True)
            p.add_argument('--cases', nargs='+')
            p.add_argument('--workers', type=int, default=1)
            p.add_argument('--resume', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'replay':
            result = replay(args.graph, args.config, args.snapshot, args.output, timeout=args.timeout)
            ok = result['status'] == 'OFFICIAL_REPLAY_OK'
        else:
            options = parameters(read_json(args.parameters) if args.parameters else None)
            if args.baseline_guard is not None: options['baseline_guard'] = args.baseline_guard == 'on'
            for name in ('calibration', 'output_policy', 'max_batches'):
                if getattr(args, name) is not None: options[name] = getattr(args, name)
            options = parameters(options)
            if args.command == 'solve':
                result = solve_case(args.graph, args.config, args.cores, args.output, seconds=args.seconds, options=options)
                ok = result['status'] == 'ok'
            else:
                graphs = [args.data_dir/(name+'.json') for name in args.cases] if args.cases else sorted(args.data_dir.glob('case_*.json'))
                result = batch(graphs, args.config, args.cores, args.output, seconds=args.seconds, options=options,
                               workers=args.workers, resume=args.resume)
                ok = result['successful'] == result['requested']
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    except (ValueError, OSError) as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
