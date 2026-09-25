"""python src/solve_problem1.py；跨平台单例与按用例并行批处理入口。"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .official import ROOT, REFERENCE
from .run import run_cases


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="问题一初始化：切图、列表分配与官方评估")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("solve", "batch"):
        command = commands.add_parser(name)
        if name == "solve":
            command.add_argument("graph", type=Path)
        else:
            command.add_argument("--data-dir", type=Path, default=REFERENCE / "data")
            command.add_argument("--cases", nargs="+", default=["all"])
            command.add_argument("--jobs", type=int, default=1)
        command.add_argument("--cores", nargs="+", type=int, choices=(2, 3, 4, 5), default=[2, 3, 4, 5])
        command.add_argument("--config", type=Path)
        command.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "q1" / "initialization")
        command.add_argument("--resume", action="store_true")
        baseline = command.add_mutually_exclusive_group()
        baseline.add_argument("--baseline-archive", type=Path,
                              default=ROOT / "results" / "problem_1_singlecore",
                              help="校验并复用已发布单核归档（默认）")
        baseline.add_argument("--recompute-baseline", action="store_true",
                              help="显式重新计算单核基准，适用于自定义小图或新配置")
        command.add_argument("--beta-search", choices=("adaptive", "fixed", "grid"), default="adaptive",
                             help="adaptive: 各模式独立递增早停（默认）；fixed: 关闭组件，双模式固定 beta=1；grid: 全枚举 1–4")
        command.add_argument("--candidate-jobs", type=int, default=1,
                             help="初始化工作进程上限，默认 2；fixed/adaptive 最多同时处理两个模式")
    args = parser.parse_args(argv)
    cores = sorted(set(args.cores))
    if args.candidate_jobs < 1:
        parser.error("--candidate-jobs must be positive")
    if args.command == "solve":
        graphs = [args.graph.resolve()]
        config = args.config or graphs[0].parent / "config.txt"
        jobs = 1
    else:
        if args.jobs < 1:
            parser.error("--jobs must be positive")
        directory = args.data_dir.resolve()
        if args.cases == ["all"]:
            graphs = sorted(directory.glob("case_*.json"))
        else:
            if "all" in args.cases:
                parser.error("all cannot be combined with individual cases")
            if any(Path(c).name != c for c in args.cases):
                parser.error("--cases accepts case names; use --data-dir to specify their directory")
            graphs = sorted({directory / (c if c.endswith(".json") else c + ".json") for c in args.cases})
        config = args.config or directory / "config.txt"
        jobs = args.jobs
    if not graphs:
        parser.error("no case files found")
    try:
        return run_cases(graphs, config.resolve(), args.output_root.resolve(), cores, jobs, args.resume,
                         args.candidate_jobs,
                         None if args.recompute_baseline else args.baseline_archive.resolve(),
                         beta_search=args.beta_search)
    except (OSError, ValueError) as error:
        print(f"[INITIALIZATION ERROR] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
