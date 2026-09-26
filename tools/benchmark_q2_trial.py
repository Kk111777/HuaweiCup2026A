"""同一冻结状态、同一试放列表的微基准；不执行官方全局调度。"""
import argparse
import copy
import json
from pathlib import Path
import platform
import statistics
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]
from q2.construct import State, try_place, commit, close
from q2.graph import Graph
from q2.official import Settings, read_json, file_hash
from common.artifacts import write_json
from test_q2 import slow_trial  # 只用于正确性与成本对照的独立整状态复制实现


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('graph', type=Path)
    parser.add_argument('--cores', type=int, choices=range(2, 6), default=5)
    parser.add_argument('--trials', type=int, default=128)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.trials <= 0 or args.rounds <= 0: parser.error('positive trials/rounds required')
    if args.output.exists(): parser.error('use a new output path')
    g = Graph(read_json(args.graph), Settings.read()); state = State(g, args.cores)
    for i, v in enumerate(g.order[:len(g.order)//2]):
        commit(g, state, try_place(g, state, v, i % args.cores), seed=True, limit=max(g.duration[v], 1))
        close(state)
    eligible = [(v, k) for v in sorted(state.ready) for k in range(args.cores)]
    if not eligible: parser.error('graph needs unassigned compute operations')
    trials = [eligible[i % len(eligible)] for i in range(args.trials)]
    before = copy.deepcopy(state.__dict__)
    for v, k in trials:
        fast = try_place(g, state, v, k); slow, end, work, size = slow_trial(g, state, v, k)
        assert (fast.score, fast.end, fast.added_work, fast.added_bytes) == (slow.score, end, work, size)
        for field in ('clocks', 'live', 'peaks'):
            assert getattr(fast, field) == tuple(map(tuple, getattr(slow, field)))
    assert state.__dict__ == before
    timings = {'incremental': [], 'deepcopy_reference': []}
    functions = {'incremental': try_place, 'deepcopy_reference': slow_trial}
    # 交替测量顺序；不把 tracemalloc 的开销混入计时。
    for i in range(args.rounds):
        for name in (list(functions) if i % 2 == 0 else list(reversed(functions))):
            start = time.perf_counter()
            for v, k in trials: functions[name](g, state, v, k)
            timings[name].append(time.perf_counter()-start)
    peaks = {}
    for name, f in functions.items():
        tracemalloc.start()
        for v, k in trials: f(g, state, v, k)
        peaks[name] = tracemalloc.get_traced_memory()[1]; tracemalloc.stop()
    result = dict(graph=str(args.graph.resolve()), graph_sha256=file_hash(args.graph),
                  python=platform.python_version(), platform=platform.platform(),
                  compute_ops=len(g.ids), tensors=len(g.tensors), accepted_ops=state.version,
                  ready_ops=len(state.ready), num_cores=args.cores, trials=len(trials), rounds=args.rounds,
                  trial_list=[(g.ids[v], k) for v, k in trials], reference_equal=True,
                  timings_seconds=timings, median_seconds={k: statistics.median(v) for k, v in timings.items()},
                  traced_peak_bytes=peaks,
                  scope='fixed-state try_place only; excludes graph parsing, shortlist, commit, DP and official evaluation',
                  source_sha256={p.relative_to(ROOT).as_posix(): file_hash(p) for p in
                                 [Path(__file__), ROOT/'tests/test_q2.py', ROOT/'src/q2/construct.py', ROOT/'src/q2/graph.py']})
    write_json(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in ('trial_list', 'source_sha256')}, indent=2))


if __name__ == '__main__': main()
