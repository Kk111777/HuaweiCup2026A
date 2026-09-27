"""Audit a frozen 20-case traffic run, separate inherited gains and report weights."""
from collections import Counter
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import argparse
import json
import statistics
import time

from q3v3.official import ROOT, metrics, sha256, write_json
from q3v3.traffic_objective import TrafficPolicy, select
from q3v3.traffic_model import cache_costs
from q3v3.traffic_search import cached_seed
from q3v3.official import read_settings
from report_q3v3 import report


def read(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def from_metrics(m):
    return dict(makespan=m['makespan_cycles'],
        data_movement_bytes={k:m[k] for k in ('original_graph_copy_bytes','scheduled_copy_bytes',
            'added_copy_bytes','partition_added_copy_bytes','spill_added_copy_bytes')},
        cache_stats=dict(hit_bytes=m['cache_hit_bytes'],miss_bytes=m['cache_miss_bytes']))


def totals(rows):
    keys=('makespan_cycles','original_graph_copy_bytes','scheduled_copy_bytes','added_copy_bytes',
          'partition_added_copy_bytes','spill_added_copy_bytes','physical_ddr_bytes',
          'cache_hit_bytes','cache_miss_bytes','cache_hits','cache_misses')
    out={k:sum(r.get(k,0) for r in rows) for k in keys}
    out['byte_hit_rate']=out['cache_hit_bytes']/max(1,out['cache_hit_bytes']+out['cache_miss_bytes'])
    out['request_hit_rate']=out['cache_hits']/max(1,out['cache_hits']+out['cache_misses'])
    return out


def compare(rows,old,new='selected'):
    pairs=[(r[old]['makespan_cycles'],r[new]['makespan_cycles']) for r in rows]
    return dict(faster=sum(b<a for a,b in pairs),equal=sum(b==a for a,b in pairs),
        slower=sum(b>a for a,b in pairs),mean_ratio=statistics.mean(a/b for a,b in pairs),
        mean_cycle_reduction_percent=statistics.mean(100*(1-b/a) for a,b in pairs),
        median_cycle_reduction_percent=statistics.median(100*(1-b/a) for a,b in pairs),
        max_cycle_increase_percent=max(100*(b/a-1) for a,b in pairs))


def build(output,q2_comparison):
    output=Path(output).resolve();t=time.perf_counter()
    audit=report(output)
    info=read(output/'run_info.json');summaries=read(output/'summary.json')
    published=read(q2_comparison)
    if published['status']!='complete' or published['count']!=20:
        raise ValueError('Q2 reference comparison is incomplete')
    references={(r['case'],r['num_cores']):r for r in published['rows']}
    prepared=read(output/'prepared_seeds.json')
    jobs={(r['case'],r['cores']):r for r in prepared['jobs']}
    if set(jobs)!=set(references) or len(summaries)!=20:
        raise ValueError('frozen sample changed')
    settings=read_settings(info['config'])
    rows=[];families=Counter();accepted=Counter();failed=Counter();sensitivity={}
    for s in sorted(summaries,key=lambda x:(x['num_cores'],x['case'])):
        pair=(s['case'],s['num_cores']);job=jobs[pair];ref=references[pair]
        folder=output/s['case']/f"N{s['num_cores']}"
        policy=TrafficPolicy.from_record(s['objective'])
        seeds={};seed_costs={}
        for role,record in job['seeds'].items():
            r=cached_seed(record,job['graph'],record['plan'],job['config'],settings)
            seeds[role]=metrics(r);seed_costs[role]=cache_costs(r)['bytes']
            if seeds[role]!=s['seed_results'][role] or sha256(record['plan'])!=s['seed_hashes'][role]:
                raise ValueError('seed metric or hash changed')
        if (seeds['previous_q3']!=ref['q3'] or seeds['q2']!=ref['q2_plan_in_q3'] or
                sha256(job['graph'])!=ref['graph_sha256'] or
                sha256(job['seeds']['q2']['plan'])!=ref['q2_plan_sha256']):
            raise ValueError('frozen Q2/Q3 comparison does not match supplied complete plans')
        final=metrics(read(folder/'verified/result.json'));fast=s['fastest_metrics']
        if not policy.eligible(from_metrics(final),fast['makespan_cycles']):
            raise ValueError('cycle guard exceeded')
        history=read(folder/'history.json')
        for h in history:
            (families if h['status']=='ok' else failed)[h['family']]+=1
            if h.get('accepted'): accepted[h['family']]+=1
        cost=read(folder/'cache_analysis.json')
        row=dict(case=s['case'],num_cores=s['num_cores'],q1=seeds['q1'],q2=ref['q2'],
            q2_in_q3=seeds['q2'],previous_q3=seeds['previous_q3'],portfolio_start=s['seed_metrics'],
            selected=final,fastest=fast,winner=s['winner'],fastest_name=s['fastest_name'],
            seed_name=s['seed_name'],objective=s['objective'],
            slowdown_from_fastest_percent=s['slowdown_from_fastest_percent'],
            fixed_T2_over_selected_T3=ref['q2']['makespan_cycles']/final['makespan_cycles'],
            fixed_T2_over_fastest_T3=ref['q2']['makespan_cycles']/fast['makespan_cycles'],
            fifo_before=seed_costs['previous_q3'].get('fifo',0),fifo_after=cost['final']['bytes'].get('fifo',0),
            cache_costs_before=seed_costs['previous_q3'],cache_costs_after=cost['final']['bytes'],
            preparation_seconds=s['preparation_seconds'],search_seconds=s['search_seconds'],
            final_replay_seconds=s['final_replay_seconds'],total_seconds=s['total_seconds'],
            official_calls=s['official_calls'],rejected_calls=s['rejected_calls'],
            host_process=read(folder/'host_process.json'),plan_sha256=s['plan_sha256'])
        rows.append(row)
        candidates=[dict(digest=h['state_digest'],result=from_metrics(h),name=h['name'])
                    for h in history if h['status']=='ok']
        for weight in ('0','.01','.025','.05','.1'):
            changed=replace(policy,weight=Fraction(weight));chosen,_=select(candidates,changed)
            selected=next(h for h in history if h.get('state_digest')==chosen['digest'] and h['status']=='ok')
            sensitivity.setdefault(weight,[]).append(dict(case=s['case'],
                name=chosen['name'],**{k:selected[k] for k in final}))
    events=[]
    for r in rows:
        h=r['host_process'];events.extend([(h['started_ns'],1),(h['finished_ns'],-1)])
    active=peak=0
    for _,delta in sorted(events): active+=delta;peak=max(peak,active)
    aggregate=dict(status='passed',count=len(rows),algorithm_revision=summaries[0]['algorithm_revision'],
        sources_unchanged=info['sources_unchanged'],workers=info['workers'],
        verified_distinct_worker_pids=len({r['host_process']['pid'] for r in rows}),
        peak_simultaneous_case_workers=peak,
        affinity_masks=sorted({r['host_process']['affinity_mask'] for r in rows}),
        elapsed_seconds=info['elapsed_seconds'],seed_preparation_seconds=info['seed_preparation_wall_seconds'],
        vs_previous_q3=compare(rows,'previous_q3'),fastest_vs_previous_q3=compare(rows,'previous_q3','fastest'),
        vs_portfolio_start=compare(rows,'portfolio_start'),vs_q1=compare(rows,'q1'),
        vs_q2_fixed_T2=compare(rows,'q2'),vs_q2_direct_q3=compare(rows,'q2_in_q3'),
        max_slowdown_from_fastest_percent=max(r['slowdown_from_fastest_percent'] for r in rows),
        totals={name:totals([r[name] for r in rows]) for name in
                ('q1','q2','q2_in_q3','previous_q3','portfolio_start','selected','fastest')},
        fifo_reread_before=sum(r['fifo_before'] for r in rows),fifo_reread_after=sum(r['fifo_after'] for r in rows),
        excluded_case14_totals={name:totals([r[name] for r in rows if r['case']!='case_014'])
                               for name in ('previous_q3','selected')},
        valid_candidate_families=dict(families),accepted_families=dict(accepted),
        rejected_families=dict(failed),
        sensitivity={w:dict(totals=totals(rs),rows=rs) for w,rs in sensitivity.items()},
        source_reference=str(Path(q2_comparison).resolve()),
        report_script_sha256=sha256(__file__),rows=rows,
        notes=['T2 is fixed published Q2 performance; all selected/fastest T3 are independent official replays.',
               'Q1/Q2 search was not rerun. Three complete plans are inherited; portfolio_start is their initial weighted selection.',
               'Sensitivity reselects only already evaluated candidates, with unchanged normalizers and cycle guard.',
               'Per-case solver seconds include contention from 20 host workers; simulated NPU core counts remain 2/3/4/5.',
               'Added COPY is partition+spill; FIFO rereads overlap these bytes and must not be added to the physical total.'])
    aggregate['audit_seconds']=time.perf_counter()-t
    write_json(output/'comparison.json',aggregate)
    write_result(output,aggregate)
    return aggregate


def write_result(output,a):
    s=a['vs_previous_q3'];t=a['totals'];old=t['previous_q3'];new=t['selected']
    pct=lambda x,y:100*(y/x-1) if x else 0
    lines=['# Q3v3 Q1/Q2 起点、容量重排与搬运折衷：20 组结果','',
        f"修订 `{a['algorithm_revision']}`。完成并审计 {a['count']}/20 组；每组交付折衷方案及纯最快方案，共 40 份独立官方最终重放。",'',
        f"主机进程 {a['workers']}；实测 {a['verified_distinct_worker_pids']} 个不同 worker PID、峰值 {a['peak_simultaneous_case_workers']} 组并行，亲和掩码 {a['affinity_masks']}。NPU 核数仍为原样本中的 2/3/4/5。",
        f"有效批次墙钟 {a['elapsed_seconds']:.2f} 秒；60 份种子官方重放另计 {a['seed_preparation_seconds']:.2f} 秒。逐例 120 秒为搜索上限，准备和最终复核另外计时。",'',
        f"相对上一版 Q3：{s['faster']} 快、{s['equal']} 平、{s['slower']} 稍慢；等权逐例平均 cycles 降低 {s['mean_cycle_reduction_percent']:.4f}%。最终相对各自纯最快方案最多慢 {a['max_slowdown_from_fastest_percent']:.4f}%，全部处于 1% 上限内。",'',
        '## 汇总搬运与命中','',
        '字节按 20 组求和；命中率为合并后的字节命中率，不能把逐例百分比直接平均。总 cycles 的求和仅用于诊断，并非官方总分。','',
        '|指标|旧 Q3|新折衷方案|变化|','|---|---:|---:|---:|']
    for title,key in [('cycles 合计','makespan_cycles'),('实际 DDR 字节','physical_ddr_bytes'),
        ('额外 COPY 字节','added_copy_bytes'),('切分搬运字节','partition_added_copy_bytes'),('spill 字节','spill_added_copy_bytes')]:
        lines.append(f'|{title}|{old[key]:,}|{new[key]:,}|{pct(old[key],new[key]):+.3f}%|')
    lines += [f"|L2 字节命中率|{old['byte_hit_rate']*100:.3f}%|{new['byte_hit_rate']*100:.3f}%|{(new['byte_hit_rate']-old['byte_hit_rate'])*100:+.3f} 个百分点|",
        f"|FIFO 驱逐后重读字节|{a['fifo_reread_before']:,}|{a['fifo_reread_after']:,}|{pct(a['fifo_reread_before'],a['fifo_reread_after']):+.3f}%|",'',
        'FIFO 重读是未命中原因分类，和 COPY 字节存在重合，不能再加到总搬运量中。', '',
        '## 与 Q2 的题目3指标对照','',
        f"固定已发布 Q2 周期 T2 / 新折衷方案 T3 的逐例等权均值为 **{a['vs_q2_fixed_T2']['mean_ratio']:.6f}×**；{a['vs_q2_fixed_T2']['faster']} 快、{a['vs_q2_fixed_T2']['equal']} 平、{a['vs_q2_fixed_T2']['slower']} 慢。",
        f"同 Q2 方案直接在 Q3 场景重评后再比较：新方案平均 cycles 降低 {a['vs_q2_direct_q3']['mean_cycle_reduction_percent']:.3f}%。Q2 的原搜索预算不同，不作为等预算算法胜负试验。",'',
        '|方案|DDR 字节|额外 COPY|spill|合并字节命中率|','|---|---:|---:|---:|---:|']
    for title,key in [('Q2 原场景','q2'),('Q2 方案直接在 Q3 执行','q2_in_q3'),('旧 Q3','previous_q3'),('新 Q3 纯最快','fastest'),('新 Q3 折衷','selected')]:
        v=t[key];hit='不适用' if key=='q2' else f"{100*v['byte_hit_rate']:.3f}%"
        lines.append(f"|{title}|{v['physical_ddr_bytes']:,}|{v['added_copy_bytes']:,}|{v['spill_added_copy_bytes']:,}|{hit}|")
    lines += ['', '## 逐例周期与继承关系','',
        '“起点”是 Q1、Q2、旧 Q3 三个完整 JSON 在官方 Q3 下重评后的加权选择；“最快”是本轮所有已评估候选的最小周期。新效果不能全部归功于 L2 搜索，也包含 Q2 起点带来的继承收益。', '',
        '|case|N|旧 Q3|起点|新最快|新折衷|较旧周期变化|T2/T3|胜出动作|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---|']
    for r in a['rows']:
        lines.append(f"|{r['case']}|{r['num_cores']}|{r['previous_q3']['makespan_cycles']:,}|{r['portfolio_start']['makespan_cycles']:,}|{r['fastest']['makespan_cycles']:,}|{r['selected']['makespan_cycles']:,}|{pct(r['previous_q3']['makespan_cycles'],r['selected']['makespan_cycles']):+.3f}%|{r['fixed_T2_over_selected_T3']:.5f}|{r['winner']}|")
    lines += ['', '## 逐例搬运、命中与用时','',
        '|case|N|旧 DDR|新 DDR|旧额外 COPY|新额外 COPY|旧 spill|新 spill|旧字节命中率|新字节命中率|准备秒|搜索秒|复核秒|合计秒|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in a['rows']:
        o,n=r['previous_q3'],r['selected']
        lines.append(f"|{r['case']}|{r['num_cores']}|{o['physical_ddr_bytes']:,}|{n['physical_ddr_bytes']:,}|{o['added_copy_bytes']:,}|{n['added_copy_bytes']:,}|{o['spill_added_copy_bytes']:,}|{n['spill_added_copy_bytes']:,}|{100*o['cache_hit_rate_bytes']:.2f}%|{100*n['cache_hit_rate_bytes']:.2f}%|{r['preparation_seconds']:.2f}|{r['search_seconds']:.2f}|{r['final_replay_seconds']:.2f}|{r['total_seconds']:.2f}|")
    lines += ['', '## 权重敏感性','',
        '固定公式 `T/Tref + λ × (0.75 × DDR/Dref + 0.25 × added_COPY/Aref)`，固定 1% 周期上限；仅对本轮已评估集合重新选择。λ=0 代表同一已搜索集合的时间优先结果，并非重新运行一套搜索。', '',
        '|λ|cycles 合计|DDR 字节|额外 COPY|字节命中率|','|---|---:|---:|---:|---:|']
    for w,v in a['sensitivity'].items():
        m=v['totals'];lines.append(f"|{w}|{m['makespan_cycles']:,}|{m['physical_ddr_bytes']:,}|{m['added_copy_bytes']:,}|{m['byte_hit_rate']*100:.3f}%|")
    x=a['excluded_case14_totals'];o,n=x['previous_q3'],x['selected']
    lines += ['', '## 解释与边界','',
        f"- 排除 case14 后，其余 19 组 DDR 合计变化 {pct(o['physical_ddr_bytes'],n['physical_ddr_bytes']):+.3f}%，spill 变化 {pct(o['spill_added_copy_bytes'],n['spill_added_copy_bytes']):+.3f}%。这是汇总敏感性分析，算法中没有 case 特判。",
        f"- 有效候选家族计数：`{a['valid_candidate_families']}`；接受动作：`{a['accepted_families']}`。动作计数不等于独立因果收益。",
        '- 1% 是相对本轮最快已评估可行解的上限，不是相对未知最优解。探索队列可暂存更慢的低搬运中间解，最终交付仍受上限保护。',
        '- 容量 DP 仅在固定分核、固定窗口、最多 8 个宏块范围内精确；窗口外和宏块内部不搜索。无可行零 spill 排序时的压力 DP 只是提案启发式。',
        '- Q1/Q2 原算法未重跑，只读取完整方案；旧 Q3 也作为完整保底输入。若要判断从零构造能力，需要另做相同预算冷启动实验。',
        '- 新权重优化是工程偏好，不是官方定义的新评分。命中率可能因消除重复读取而下降，应连同 DDR 字节和周期解释。',
        '- 第一轮目录保留预算超时的故障记录；本 r2 目录是修复后完整重跑结果，未把首轮候选混入种子。',
        '- 所有源码只在本地修改，官方程序未修改。JSON 中保留全部指标、哈希、用时与实际进程信息。','']
    (output/'RESULT.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('output',type=Path)
    p.add_argument('--q2-comparison',type=Path,default=ROOT/'outputs/q3v3_vs_q2optfull20_20260927/comparison.json')
    args=p.parse_args();a=build(args.output,args.q2_comparison)
    print(json.dumps({k:a[k] for k in ('status','count','workers','peak_simultaneous_case_workers',
        'elapsed_seconds','vs_previous_q3','vs_q2_fixed_T2','totals','audit_seconds')},ensure_ascii=False,indent=2))
