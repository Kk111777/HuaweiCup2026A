"""Command-line assembly of a registered problem and the shared case runner."""
import argparse
import json

from .api import solve_case
from .io import read
from .registry import BACKENDS


def main():
    parser = argparse.ArgumentParser(description='One case budget across all requested core counts')
    parser.add_argument('graph')
    parser.add_argument('--problem', default='q1', choices=sorted(BACKENDS))
    parser.add_argument('--config', required=True, help='Official hardware configuration')
    parser.add_argument('--parameters', help='Search parameters JSON; defaults come from the backend')
    parser.add_argument('--cores', type=int, nargs='+', required=True)
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    parameters = read(args.parameters) if args.parameters else None
    result = solve_case(args.graph, args.config, args.cores, args.output,
                        seconds=args.seconds, parameters=parameters, problem=args.problem)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())
