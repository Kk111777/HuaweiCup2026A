import argparse
import json
from .api import solve_case, solve_file
from .io import read


def main():
    p = argparse.ArgumentParser(description='Serial solver: all requested core counts share one case budget; no singlecore baseline is required')
    p.add_argument('graph')
    p.add_argument('--config', required=True)
    p.add_argument('--cores', type=int, nargs='+', required=True, choices=[2,3,4,5])
    p.add_argument('--output', required=True)
    p.add_argument('--parameters', help='Optional search parameter JSON; official config remains separate')
    p.add_argument('--seconds', type=float, default=600,
        help='Total target seconds for this graph and all requested core counts combined')
    a = p.parse_args()
    if len(set(a.cores)) != len(a.cores):
        p.error('duplicate core counts')
    parameters = read(a.parameters) if a.parameters else None
    if len(a.cores) == 1:
        result = solve_file(a.graph,a.config,a.cores[0],a.output,seconds=a.seconds,parameters=parameters)
    else:
        result = solve_case(a.graph,a.config,a.cores,a.output,seconds=a.seconds,parameters=parameters)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['status']=='ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())
