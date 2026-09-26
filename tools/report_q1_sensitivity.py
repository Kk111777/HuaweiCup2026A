"""Audit and summarize Q1 sensitivity outputs; matplotlib is optional."""
from pathlib import Path
from statistics import mean, median
import argparse
import csv
import json
import math

from q1_sensitivity import audit, write, digest, official


def quantile(values, q):
    values=sorted(values); position=(len(values)-1)*q
    lo,hi=math.floor(position),math.ceil(position)
    return values[lo]+(values[hi]-values[lo])*(position-lo)


def summarize(rows):
    """Pair first, aggregate second; the mean of ratios is not a ratio of means."""
    lookup={(r['case'],r['num_cores'],r['variant']):r for r in rows}
    if len(lookup)!=len(rows): raise ValueError('Duplicate result keys')
    paired=[]
    for row in rows:
        case,n=row['case'],row['num_cores']
        adaptive=lookup[(case,n,'adaptive')]
        best=min(lookup[(case,n,f'beta{b}')]['makespan_cycles'] for b in (1,2,3,4))
        if best<=0 or adaptive['makespan_cycles']<=0: raise ValueError('Nonpositive official makespan')
        paired.append({**row,'relative_to_adaptive_pct':100*(row['makespan_cycles']/adaptive['makespan_cycles']-1),
                       'regret_vs_grid_best_pct':100*(row['makespan_cycles']/best-1),
                       'grid_best_makespan':best})
    summary=[]
    for n in sorted({r['num_cores'] for r in paired}):
        for variant in ('beta1','beta2','beta3','beta4','adaptive'):
            selected=[r for r in paired if r['num_cores']==n and r['variant']==variant]
            d=[r['relative_to_adaptive_pct'] for r in selected]
            summary.append({'num_cores':n,'variant':variant,'cases':len(selected),
                            'mean_relative_pct':mean(d),'median_relative_pct':median(d),
                            'q25_relative_pct':quantile(d,.25),'q75_relative_pct':quantile(d,.75),
                            'min_relative_pct':min(d),'max_relative_pct':max(d),
                            'better':sum(x<0 for x in d),'equal':sum(x==0 for x in d),'worse':sum(x>0 for x in d),
                            'mean_task_count':mean(r['task_count'] for r in selected),
                            'mean_added_copy_MiB':mean(r['added_copy_bytes']/1048576 for r in selected),
                            'mean_spill_MiB':mean(r['spill_added_copy_bytes']/1048576 for r in selected),
                            'mean_solve_seconds':mean(r['solve_seconds'] for r in selected),
                            'mean_verification_seconds':mean(r['verification_seconds'] for r in selected),
                            'mean_regret_vs_grid_pct':mean(r['regret_vs_grid_best_pct'] for r in selected),
                            'max_regret_vs_grid_pct':max(r['regret_vs_grid_best_pct'] for r in selected)})
    return paired,summary


def csv_write(path,rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def plot(output,summary):
    try:
        import matplotlib
    except ImportError as error:
        raise ValueError('Plotting requires matplotlib. Install it in your experiment environment, or use --no-plots.') from error
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter
    plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'svg.fonttype':'none'})
    cores=sorted({r['num_cores'] for r in summary})
    fig,axes=plt.subplots(1,len(cores),figsize=(5.7*len(cores),4.9),sharey=True,squeeze=False)
    for ax,n in zip(axes[0],cores):
        s=[r for r in summary if r['num_cores']==n and r['variant']!='adaptive']
        x=[1,2,3,4]; y=[r['mean_relative_pct'] for r in s]
        ax.fill_between(x,[r['q25_relative_pct'] for r in s],[r['q75_relative_pct'] for r in s],color='#2375AA',alpha=.16,label='Case-level interquartile range')
        ax.plot(x,y,'o-',color='#17628C',linewidth=2,label='Mean paired change')
        ax.plot(x,[r['median_relative_pct'] for r in s],'s--',color='#A85B22',linewidth=1.6,label='Median paired change')
        ax.axhline(0,color='#555555',linewidth=1,label='Adaptive baseline')
        for b,v in zip(x,y):ax.annotate(f'{v:+.2f}%',(b,v),xytext=(0,9),textcoords='offset points',ha='center',fontsize=10)
        ax.set_title(f'{n} cores | {s[0]["cases"]} cases');ax.set_xticks(x);ax.set_xlabel('Fixed granularity parameter beta')
        ax.yaxis.set_major_formatter(PercentFormatter(xmax=100));ax.grid(axis='y',alpha=.18);ax.margins(x=.12,y=.24)
    axes[0][0].set_ylabel('Official cycles relative to adaptive (%)')
    handles,labels=axes[0][0].get_legend_handles_labels()
    fig.legend(handles,labels,loc='lower center',bbox_to_anchor=(.5,.05),ncol=2,frameon=False,fontsize=10)
    fig.suptitle('Q1 baseline: sensitivity to beta',fontsize=16,y=.98)
    fig.text(.5,.01,'Positive = slower. Shading describes the case distribution, not a confidence interval.',ha='center',fontsize=9,color='#555555')
    fig.tight_layout(rect=(0,.21,1,.93))
    directory=output/'figures';directory.mkdir(exist_ok=True)
    paths=[]
    for ext in ('png','svg'):
        p=directory/f'beta_sensitivity.{ext}';fig.savefig(p,dpi=220,bbox_inches='tight');paths.append(p)
    plt.close(fig)
    return paths


def report(output,no_plots=False,allow_partial=False):
    output=Path(output).resolve()
    status,rows=audit(output,allow_partial)
    if not rows: raise ValueError('No complete pairs available to report')
    rows.sort(key=lambda r:(r['case'],r['num_cores'],r['variant']))
    paired,summary=summarize(rows)
    for name,data in [('details.csv',rows),('paired_details.csv',paired),('summary.csv',summary)]:csv_write(output/name,data)
    write(output/'summary.json',summary);write(output/'audit.json',status)
    images=[] if no_plots else plot(output,summary)
    manifest=official.read_json(output/'experiment.json')
    lines=['# 第一问基座 β 灵敏度分析','',
           f"状态：**{status['status']}**；独立官方复评 **{status['verified_rows']}/{status['requested_rows']}** 份。",'',
           '固定 β 的基座在原有 global/components 模式间择优；默认自适应策略另外原生运行。全部完成组合均检查自适应方案与完整扫描中按原早停规则重建的方案一致。', '',
           '| 核数 | β | 样本数 | 平均周期变化 | 中位数 | 最大变化 | 优/平/劣（对自适应） |',
           '|---:|---:|---:|---:|---:|---:|:---:|']
    for r in summary:
        if r['variant']=='adaptive':continue
        lines.append(f"| {r['num_cores']} | {r['variant'][-1]} | {r['cases']} | {r['mean_relative_pct']:+.3f}% | {r['median_relative_pct']:+.3f}% | {r['max_relative_pct']:+.3f}% | {r['better']}/{r['equal']}/{r['worse']} |")
    lines+=['','逐例计算 `100 × (T_fixed / T_adaptive − 1)` 后取算术平均。正值表示变慢；阴影是逐例变化的四分位区间，不是置信区间。', '']
    if images:lines+=['![Sensitivity](figures/beta_sensitivity.png)','']
    lines+=['## 自适应早停与完整扫描','',
            '| 核数 | 周期达到四档最优 | 对四档最优的平均差距 | 最大差距 |','|---:|---:|---:|---:|']
    for n in sorted({r['num_cores'] for r in rows}):
        group=[r for r in paired if r['num_cores']==n and r['variant']=='adaptive'];d=[r['regret_vs_grid_best_pct'] for r in group]
        lines.append(f'| {n} | {sum(x==0 for x in d)}/{len(d)} | {mean(d):.3f}% | {max(d):.3f}% |')
    lines+=['','早停只在每种模式下根据相邻 β 的周期严格下降继续；不保证找到四个档位的全局最优。','',
            '## 实验口径','',
            f"- 抽样方式：`{manifest['selection_method']}`；种子：`{manifest['parameters']['sampling_seed']}`。显式小样本运行仅用于验证，不能代替分层正式结果。",
            '- 官方硬件配置和评价程序冻结，基座源码没有修改。每份方案由独立官方 CLI 重读复评，比较除输入文件名元数据外的全部结果字段。',
            '- 固定 β 求解时间为共享图读取/索引时间加该 β 各模式候选时间之和；自适应为另一次原生运行，图已读取。这两种计时不是完全相同的端到端计时，不据此声称运行加速。',
            '- worker 是本机并发进程数；num_cores 是模拟 NPU 核数。官方周期不使用墙钟时间。未因搜索墙钟预算提前截断；如配置官方复评超时，失败组合不会发布。',
            '- `experiment.json` 保存样本、参数、源码/输入/官方指纹和 Python 版本。Git 提交号只是定位信息，逐文件哈希覆盖未提交实现。',
            '- `paired_details.csv` 保存逐例配对指标；`summary.csv` 同时列 Task 数、额外搬运、spill 和计时；`runs/` 保存候选、方案、独立复评结果及完成清单。',
            '- 部分报告仅含完整的图/核数组合；必须使用相同完成集合比较各 β。缺失组合不会作为零填充，也不作百例结论。','',
            f"缺失组合：`{json.dumps(status['missing_pairs'])}`。"]
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    products=[output/p for p in ('details.csv','paired_details.csv','summary.csv','summary.json','audit.json','REPORT.md')]+images
    write(output/'report_manifest.json',{p.relative_to(output).as_posix():digest(p) for p in products})
    return status


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--no-plots',action='store_true');p.add_argument('--allow-partial',action='store_true')
    args=p.parse_args()
    try: print(json.dumps(report(args.output_dir,args.no_plots,args.allow_partial),ensure_ascii=False,indent=2))
    except (ValueError,AssertionError,OSError) as error:p.exit(2,f'ERROR: {error}\n')


if __name__=='__main__':main()
