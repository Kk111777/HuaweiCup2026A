"""Coverage-aware reports: mean individual ratios, never ratios of totals."""
import csv
import io
from pathlib import Path
from statistics import fmean
from zipfile import ZIP_DEFLATED, ZipFile

from .artifacts import atomic_text, file_hash, write_json
from q2.official import read_json


METRICS = ('t2_cycles', 't2_to_3_cycles', 't3_cycles', 'hardware_speedup',
           'algorithm_speedup', 'cache_speedup', 'added_copy_bytes',
           'cache_hit_rate_bytes', 'actual_ddr_bytes')


def summarize(rows, expected, output, *, plots=True, export=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    expected = set(map(tuple, expected))
    indexed = {}
    for row in rows:
        key = row['case'], row['num_cores']
        if key in indexed or key not in expected:
            raise ValueError(f'duplicate or unexpected result: {key}')
        indexed[key] = row
    good = [r for r in indexed.values() if r['status'] == 'SUCCESS_VERIFIED']
    missing = sorted(expected - indexed.keys())
    failed = sorted(k for k, r in indexed.items() if r['status'] != 'SUCCESS_VERIFIED')
    by_core = {}
    for n in sorted({n for _, n in expected}):
        selected = [r for r in good if r['num_cores'] == n]
        count = sum(k[1] == n for k in expected)
        by_core[n] = dict(successful=len(selected), expected=count, complete=len(selected) == count,
                          improved=sum(r['t3_cycles'] < r['t2_to_3_cycles'] for r in selected),
                          tied=sum(r['t3_cycles'] == r['t2_to_3_cycles'] for r in selected),
                          nonregression=all(r['t3_cycles'] <= r['t2_to_3_cycles'] for r in selected),
                          **{'mean_' + k: fmean(r[k] for r in selected) if selected else None for k in METRICS})
    summary = dict(complete=not missing and not failed, requested=len(expected), successful=len(good),
                   missing=missing, failed=failed, by_core=by_core,
                   ratio_aggregation='arithmetic mean of individual case ratios',
                   numerator='fixed Q2 final plan evaluated without L2; N1 official whole-graph fixed plan',
                   denominator='independently verified Q3 final plan at the same available core count')
    write_json(output / 'summary.json', summary)
    leading = ['case', 'num_cores', 'status', *METRICS]
    fields = leading + sorted(set().union(*(r.keys() for r in rows)) - set(leading)) if rows else leading
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    writer.writerows(indexed[k] for k in sorted(indexed))
    atomic_text(output / 'details.csv', stream.getvalue())
    lines = ['# 第三问复现结果', '', f'官方复评通过 **{len(good)}/{len(expected)}** 组。', '',
             'T2 为固定 Q2 方案无 L2 时间；T2→3 为该方案启用 L2 后的时间；T3 为第三问最终时间。',
             '各加速比先逐用例计算，再作算术平均。未完成时，下表仅表示已验证子集。', '',
             '| 可用核数 | 已验证/请求 | 严格提速例数 | 平均 T2 | 平均 T2→3 | 平均 T3 | 硬件加速 | 算法加速 | 总加速 |',
             '|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for n, row in by_core.items():
        value = lambda key: '—' if row['mean_' + key] is None else f'{row["mean_" + key]:.4f}'
        lines.append(f'| {n} | {row["successful"]}/{row["expected"]} | {row["improved"]} | ' +
                     ' | '.join(value(k) for k in METRICS[:6]) + ' |')
    cases = {}
    for row in good:
        cases.setdefault(row['case'], row)
    if cases:
        lines += ['', f'原图 Q3 用时（各核数共享预算）均值：{fmean(r.get("case_q3_seconds", 0) for r in cases.values()):.3f} 秒。',
                  f'自动生成 Q2 的前置用时合计：{sum(r.get("case_prerequisite_seconds", 0) for r in cases.values()):.3f} 秒。',
                  '', '运行到候选上限或时间预算的结果仍须通过最终官方复评；它们不代表搜索域已穷尽或全局最优。',
                  '`details.csv` 给出停止原因、实际使用核数、候选数、换出搬运量、命中率及每组计划和结果哈希。']
    if missing or failed:
        lines += ['', f'缺少 {len(missing)} 组，失败 {len(failed)} 组；完整清单见 `summary.json`。']
    if plots and good:
        draw(good, output)
        lines += ['', '对比图：`figures/mean_curves.png`、`figures/algorithm_scatter.png`、`figures/comparison_N*.png`。']
    atomic_text(output / 'report.md', '\n'.join(lines) + '\n')
    if export:
        with ZipFile(output / 'plans.zip', 'w', ZIP_DEFLATED) as archive:
            for row in sorted(good, key=lambda r: (r['num_cores'], r['case'])):
                name = f'{row["case"]}_multicore_res.json'
                path = output / row['case'] / f'N{row["num_cores"]}' / name
                if file_hash(path) != row['plan_sha256']:
                    raise ValueError(f'plan hash changed before export: {path}')
                archive.write(path, f'N{row["num_cores"]}/{name}')
        artifacts = [output / n for n in ('summary.json', 'details.csv', 'report.md', 'plans.zip')]
        artifacts.extend(sorted((output / 'figures').glob('*')))
        write_json(output / 'report_artifacts.json', {p.relative_to(output).as_posix(): file_hash(p) for p in artifacts})
    return summary


def draw(rows, output):
    try:
        import matplotlib
        matplotlib.use('Agg')
        from matplotlib import pyplot as plt
    except ImportError as error:
        raise RuntimeError('plotting requires matplotlib; install requirements-q3.txt or use --no-plots') from error
    plt.rcParams.update({'font.size': 10, 'figure.dpi': 120, 'savefig.dpi': 180})
    folder = output / 'figures'
    folder.mkdir(exist_ok=True)
    ns = sorted({r['num_cores'] for r in rows})
    groups = {n: sorted((r for r in rows if r['num_cores'] == n), key=lambda r: r['case']) for n in ns}
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    for key, label in [('t2_cycles', 'Q2 without L2'), ('t2_to_3_cycles', 'Q2 plan with L2'), ('t3_cycles', 'Q3 final')]:
        axes[0].plot(ns, [fmean(r[key] for r in groups[n]) for n in ns], 'o-', label=label)
    for key, label in [('hardware_speedup', 'Hardware'), ('algorithm_speedup', 'Algorithm'), ('cache_speedup', 'Total')]:
        axes[1].plot(ns, [fmean(r[key] for r in groups[n]) for n in ns], 'o-', label=label)
    for ax in axes:
        ax.set_xlabel('Available NPU cores')
        ax.set_xticks(ns)
        ax.grid(alpha=.25)
        ax.legend()
    axes[0].set_ylabel('Mean makespan (cycles)')
    axes[1].set_ylabel('Mean of per-case speedup ratios')
    fig.suptitle(f'Q3 verified groups: {len(rows)} (see summary for requested coverage)')
    fig.tight_layout()
    fig.savefig(folder / 'mean_curves.png')
    fig.savefig(folder / 'mean_curves.svg')
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(6, 5))
    for n in ns:
        ax.scatter([r['t2_to_3_cycles'] for r in groups[n]], [r['t3_cycles'] for r in groups[n]], s=12, alpha=.65, label=f'N={n}')
    lower = max(1, min(r['t3_cycles'] for r in rows))
    upper = max(r['t2_to_3_cycles'] for r in rows)
    ax.plot([lower, upper], [lower, upper], '--', color='grey', label='Unchanged')
    ax.set(xscale='log', yscale='log', xlabel='Q2 plan with L2 (cycles)', ylabel='Q3 final (cycles)')
    ax.legend()
    ax.grid(alpha=.25)
    fig.tight_layout()
    fig.savefig(folder / 'algorithm_scatter.png')
    plt.close(fig)
    for n, group in groups.items():
        fig, axes = plt.subplots(2, 1, figsize=(13, 6), sharex=True)
        x = list(range(1, len(group) + 1))
        for key, label in [('t2_cycles', 'Q2 without L2'), ('t2_to_3_cycles', 'Q2 plan with L2'), ('t3_cycles', 'Q3 final')]:
            axes[0].plot(x, [r[key] for r in group], '.-', linewidth=.8, label=label)
        axes[0].set(yscale='log', ylabel='Makespan (cycles)', title=f'N={n}; {len(group)} verified cases')
        axes[0].legend()
        axes[1].plot(x, [r['cache_speedup'] for r in group], '.-', label='Total speedup')
        axes[1].plot(x, [r['algorithm_speedup'] for r in group], '.-', label='Algorithm speedup')
        axes[1].axhline(1, color='grey', linestyle='--', linewidth=.8)
        axes[1].set(xlabel='Case index (sorted by case name; details.csv)', ylabel='Speedup ratio')
        axes[1].legend()
        for ax in axes:
            ax.grid(alpha=.25)
        fig.tight_layout()
        fig.savefig(folder / f'comparison_N{n}.png')
        plt.close(fig)


def from_directory(output, *, plots=True):
    from .pipeline import verified_directory
    output = Path(output)
    info = read_json(output / 'run_info.json')
    expected = [(Path(name).stem, n) for name in info['input_sha256'] for n in info['cores']]
    rows = []
    for case in sorted({case for case, _ in expected}):
        directory = output / case
        if directory.exists():
            if not verified_directory(directory):
                raise ValueError(f'published result failed manifest verification: {directory}')
            rows.extend(read_json(directory / 'case.json')['rows'])
    return summarize(rows, expected, output, plots=plots, export=True)
