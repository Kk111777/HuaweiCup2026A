import argparse
import json
from .api import solve_file


def main():
    p = argparse.ArgumentParser(description='Unified serial solver, one arbitrary graph and core count')
    p.add_argument('graph')
    p.add_argument('--config', required=True)
    p.add_argument('--cores', type=int, required=True, choices=[2,3,4,5])
    p.add_argument('--output', required=True)
    p.add_argument('--seconds', type=float, default=600)
    p.add_argument('--local-test-baseline-archive')
    p.add_argument('--archive-repo-root')
    a = p.parse_args()
    result = solve_file(a.graph,a.config,a.cores,a.output,seconds=a.seconds,
        local_test_baseline_archive=a.local_test_baseline_archive, archive_repo_root=a.archive_repo_root)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['status']=='ok' else 2


if __name__ == '__main__':
    raise SystemExit(main())
