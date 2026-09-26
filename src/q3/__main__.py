"""Problem 3 CLI. Workers are host processes; cores are simulated NPU cores."""
import argparse
import json
from pathlib import Path
import sys
import time

from .artifacts import file_hash, write_json
from problem_1_baseline import output_lock
from q2.official import ROOT, Q2Error, read_json
from .config import parameters
from .official import Settings, replay
from .pipeline import batch
from .worker import Worker


def main(argv=None):
    parser = argparse.ArgumentParser(description='问题三：Q2 新基座 + 共享 FIFO L2 + 官方独立复评')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('solve', 'batch', 'verify', 'report'):
        p = commands.add_parser(name)
        p.add_argument('--output-dir', type=Path, required=True)
        if name in ('solve', 'batch', 'report'):
            p.add_argument('--no-plots', action='store_true')
        if name == 'report':
            continue
        p.add_argument('--reference-root', type=Path, default=ROOT / 'official')
        p.add_argument('--config', type=Path)
        if name in ('solve', 'verify'):
            p.add_argument('graph', type=Path)
        if name == 'verify':
            p.add_argument('plan', type=Path)
            p.add_argument('--timeout', type=float, default=600)
            continue
        p.add_argument('--cores', nargs='+', type=int, choices=range(1, 6), default=[1, 2, 3, 4, 5])
        p.add_argument('--q2-source', type=Path, help='Q2 计划 JSON、结果根目录或 plans.zip；缺少某组时自动生成 Q2')
        p.add_argument('--algorithm-config', type=Path, default=ROOT / 'configs/q3/default.json')
        p.add_argument('--wall-seconds', type=float, help='每张原图全部请求核数共享的 Q3 预算，不含 Q2 生成')
        p.add_argument('--q2-seconds', type=float, help='缺少 Q2 时，每个核数的 Q2 预算')
        p.add_argument('--verification-reserve', type=float)
        p.add_argument('--evaluation-timeout', type=float)
        p.add_argument('--max-candidates', type=int)
        p.add_argument('--without-bounds', action='store_true')
        p.add_argument('--resume', action='store_true', help='仅复用输入、配置、源码及全部产物哈希一致的完成案例')
        if name == 'batch':
            p.add_argument('--data-dir', type=Path, required=True)
            p.add_argument('--cases', nargs='+', help='例如 case_001 case_016；省略时使用全部 case_*.json')
            p.add_argument('--workers', type=int, default=4, help='主机并行进程数，与 --cores 无关')
    args = parser.parse_args(argv)
    try:
        if args.command == 'report':
            from .report import from_directory
            with output_lock(args.output_dir):
                result = from_directory(args.output_dir, plots=not args.no_plots)
        elif args.command == 'verify':
            import math
            if not math.isfinite(args.timeout) or args.timeout <= 0:
                raise ValueError('--timeout must be finite and positive')
            if args.output_dir.exists() and any(args.output_dir.iterdir()):
                raise ValueError('verify requires a new or empty output directory')
            started = time.monotonic()
            settings = Settings.read(args.reference_root, args.config)
            args.output_dir.mkdir(parents=True, exist_ok=True)
            with output_lock(args.output_dir):
                value = replay(args.graph, args.plan, settings, args.output_dir / 'official', args.timeout)
                worker = Worker(args.graph, settings, parameters(), args.output_dir)
                try:
                    audit = worker.call(dict(action='audit', plan=str(args.plan.resolve()),
                        result=str((args.output_dir / 'official/result.json').resolve())), args.timeout - (time.monotonic() - started))
                finally:
                    worker.close()
                if audit['status'] != 'AUDITED':
                    raise ValueError(f'physical audit failed: {audit}')
                write_json(args.output_dir / 'audit.json', audit['audit'])
                write_json(args.output_dir / 'verification.json', dict(status='SUCCESS_VERIFIED',
                    input_sha256=file_hash(args.graph), plan_sha256=file_hash(args.plan),
                    result_sha256=file_hash(args.output_dir / 'official/result.json'),
                    makespan=value['makespan'], official=settings.scene.hashes))
            print(f'SUCCESS_VERIFIED makespan={value["makespan"]}; {args.output_dir}')
            return 0
        else:
            options = read_json(args.algorithm_config)
            for attribute, key in [('wall_seconds', 'seconds'), ('q2_seconds', 'q2_seconds'),
                                   ('verification_reserve', 'verification_reserve'),
                                   ('evaluation_timeout', 'evaluation_timeout'), ('max_candidates', 'max_candidates')]:
                if getattr(args, attribute) is not None:
                    options[key] = getattr(args, attribute)
            if args.without_bounds:
                options['use_bounds'] = False
            options = parameters(options)
            cores = sorted(set(args.cores))
            if len(cores) != len(args.cores):
                raise ValueError('duplicate core counts')
            if args.command == 'solve':
                graphs, workers = [args.graph.resolve()], 1
            else:
                graphs = ([args.data_dir / (name + '.json') for name in args.cases] if args.cases
                          else sorted(args.data_dir.glob('case_*.json')))
                graphs, workers = [p.resolve() for p in graphs], args.workers
                if not 1 <= workers <= 128:
                    raise ValueError('--workers must be in 1..128')
            if not graphs or len({g.stem for g in graphs}) != len(graphs) or any(not g.is_file() for g in graphs):
                raise ValueError('require existing input graphs with unique case names')
            Settings.read(args.reference_root, args.config)
            result = batch(graphs, cores, args.output_dir, reference=args.reference_root, config=args.config,
                           q2_source=args.q2_source, options=options, workers=workers, resume=args.resume, plots=not args.no_plots)
        print(f'{result["successful"]}/{result["requested"]} SUCCESS_VERIFIED; {args.output_dir / "report.md"}')
        return 0 if result['complete'] else 1
    except (Q2Error, ValueError, FileNotFoundError, TimeoutError, RuntimeError) as error:
        print(f'[{getattr(error, "status", "FAILED")}] {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
