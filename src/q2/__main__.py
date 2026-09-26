"""单例、批量、已给定方案复核；模拟核数与 worker 数分开。"""
import argparse
from pathlib import Path
import sys
from .official import ROOT, Settings, Q2Error, read_json
from .pipeline import load_options, validate_options, batch, run_official
from .beta import POLICIES


def main(argv=None):
    parser = argparse.ArgumentParser(description='问题二新基座：联动生长 + 精确窗口缓存排序')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('solve', 'batch', 'verify'):
        p = commands.add_parser(name)
        if name != 'batch': p.add_argument('graph', type=Path)
        if name == 'verify': p.add_argument('plan', type=Path)
        p.add_argument('--reference-root', type=Path, default=ROOT/'official')
        p.add_argument('--config', type=Path)
        p.add_argument('--output-dir', type=Path, required=True)
        if name != 'verify':
            p.add_argument('--algorithm-config', type=Path, default=ROOT/'configs/q2/baseline.json')
            p.add_argument('--beta-search', choices=POLICIES,
                           help='adaptive: 官方周期持平或下降继续、严格增加停止（2.0默认）；fixed: 仅 β=1；grid: 全枚举')
            p.add_argument('--resume', action='store_true')
            p.add_argument('--wall-seconds', type=float)
            p.add_argument('--verification-reserve', type=float)
            baseline = p.add_mutually_exclusive_group()
            baseline.add_argument('--baseline-archive', type=Path, default=ROOT/'results/problem_1_singlecore')
            baseline.add_argument('--without-baseline', action='store_true', help='自定义图：不报告加速比，不重算或替换固定单核基准')
        else: p.add_argument('--timeout', type=float, default=600)
        if name == 'solve': p.add_argument('-n', '--cores', type=int, choices=range(1, 6), required=True)
        if name == 'batch':
            p.add_argument('--data-dir', type=Path, required=True)
            p.add_argument('--cases', nargs='+', help='限定 case stem；省略时枚举目录中全部 case_*.json')
            p.add_argument('--cores', nargs='+', type=int, choices=range(2, 6), default=[2, 3, 4, 5])
            p.add_argument('--workers', type=int, default=1)
    args = parser.parse_args(argv)
    try:
        if args.command == 'verify':
            settings = Settings.read(args.reference_root, args.config); calls = []
            result = run_official(args.graph.resolve(), read_json(args.plan), settings,
                                  args.output_dir.resolve(), args.timeout, calls, 'verify')
            print(calls[-1]); return 0 if result is not None else 1
        options = load_options(args.algorithm_config)
        if args.beta_search is not None: options['construction']['beta_search'] = args.beta_search
        if args.wall_seconds is not None: options['runtime']['wall_budget_seconds'] = args.wall_seconds
        if args.verification_reserve is not None: options['runtime']['final_verification_reserve_seconds'] = args.verification_reserve
        import math
        total, reserve = options['runtime']['wall_budget_seconds'], options['runtime']['final_verification_reserve_seconds']
        if not (math.isfinite(total) and math.isfinite(reserve) and 0 < reserve < total):
            raise Q2Error('INPUT_INVALID', 'require 0 < verification reserve < wall seconds')
        if args.command == 'solve': graphs, cores, workers = [args.graph], [args.cores], 1
        else:
            graphs = [args.data_dir/(name+'.json') for name in args.cases] if args.cases else sorted(args.data_dir.glob('case_*.json'))
            cores, workers = args.cores, args.workers
            if len(set(cores)) != len(cores) or not 1 <= workers <= 128:
                raise Q2Error('INPUT_INVALID', 'distinct core counts and positive workers required')
        options['runtime']['workers'] = workers
        validate_options(options)
        result = batch(graphs, cores, args.output_dir, options, args.reference_root, args.config,
                       workers, args.resume, None if args.without_baseline else args.baseline_archive)
        print(f'{result["successful"]}/{result["requested"]} SUCCESS_VERIFIED; {args.output_dir / "summary.json"}')
        return 0 if result['complete'] else 1
    except (Q2Error, FileNotFoundError) as error:
        print(f'[{getattr(error, "status", "INPUT_INVALID")}] {error}', file=sys.stderr)
        return 2


if __name__ == '__main__': raise SystemExit(main())
