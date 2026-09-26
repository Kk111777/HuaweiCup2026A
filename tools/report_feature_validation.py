"""Historical per-core report. Current case-budget runs use report_unified.py."""
import argparse
import csv
from pathlib import Path
import statistics
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from q1.unified.io import read,write,digest
from q1.unified.api import source_signature


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runs',type=Path,nargs='+',required=True)
    p.add_argument('--paused',type=Path,default=ROOT/'outputs/unified_100cases_final')
    p.add_argument('--personal',type=Path,default=ROOT.parent/'q1-modular/outputs/refined_100cases_verified')
    p.add_argument('--base',type=Path,default=ROOT.parents[1]/'collaborators/Zysishuiyears/q1-basic-working/outputs/q1_100cases_20260925_cpu20')
    p.add_argument('--output',type=Path,default=ROOT/'outputs/feature_validation_report')
    p.add_argument('--expected',type=int,default=32)
    a=p.parse_args();rows=[];seen=set();source=source_signature()
    for run in a.runs:
        for f in sorted(run.glob('case_*/N*/summary.json')):
            r=read(f);d=f.parent;case=d.parent.name;n=int(d.name[1:])
            if r.get('budget_scope') == 'case_all_cores':
                raise ValueError('historical per-core reporter: use tools/report_unified.py for current case budgets')
            assert r['source_signature']==source, 'different version in staged results'
            assert (case,n) not in seen,'duplicate result'
            seen.add((case,n))
            old=a.paused/case/d.name/'summary.json'
            prior=read(old).get('makespan') if old.exists() else None
            personal=read(a.personal/case/d.name/'metrics.json')['personal_makespan_cycles']
            base=read(a.base/case/d.name/'result.json')['makespan']
            m=read(d/'verified.json') if (d/'verified.json').exists() else {}
            verified=(m.get('independent_official_replay') is True and m.get('source_signature')==source
                      and m.get('plan_sha256')==digest(d/'plan.json') and m.get('result_sha256')==digest(d/'evaluation.json'))
            solver_cycles=r.get('makespan');cycles=solver_cycles;single=r.get('baseline',{}).get('cycles')
            source_kind=r.get('score_source','official')
            if verified and m.get('official_replay_sha256'):
                assert digest(d/'official_replay.json')==m['official_replay_sha256']
                cycles=read(d/'official_replay.json')['makespan'];source_kind='official_replay'
            row=dict(case=case,cores=n,cycles=cycles,singlecore_cycles=single,source_signature=source,
                status=r['status'],wall_seconds=r['wall_seconds'],accounted_wall_seconds=r['accounted_wall_seconds'],
                score_source=source_kind,solver_makespan=solver_cycles,
                cpu_seconds=r.get('cpu_seconds'),hard_timeout=r['hard_timeout'],timed_out_phase=r.get('timed_out_phase'),
                independent_replay=verified,path=str(d.resolve()))
            for name,value in [('paused',prior),('personal',personal),('base',base)]:
                row[name+'_cycles']=value
                row['change_vs_'+name]=cycles/value-1 if cycles and value and source_kind!='approximate' else None
            rows.append(row)
    rows.sort(key=lambda r:(r['case'],r['cores']))
    a.output.mkdir(parents=True,exist_ok=True)
    write(a.output/'results.json',dict(expected=a.expected,completed=len(rows),rows=rows,
        independent_replays=sum(r['independent_replay'] for r in rows),source_signature=source))
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with (a.output/'case_times.csv').open('w',encoding='utf-8-sig',newline='') as s:
        w=csv.DictWriter(s,fieldnames=fields);w.writeheader();w.writerows(rows)
    lines=['# 图特征版本分批复测','',
        '> 历史每核预算记录，不代表当前共享整case预算版本。当前流程见 [unified.md](unified.md)。','',
        f'已完成 {len(rows)}/{a.expected} 项；独立重放通过 {sum(r["independent_replay"] for r in rows)} 项；保护超时 {sum(r["hard_timeout"] for r in rows)} 项。',
        f'源代码指纹：`{source}`。','',
        '8 张代表性图：006、014、049、058、072、076、091、092。另用随机种子 20260926 从 86 张留出图抽取 004、008、013、030、036、075、088、095。均测试 2、5 核。',
        '每项保留实际墙钟、CPU 和补计历史单核后的总秒数；跨 case 并行，单个求解串行。原个人高核数归档包含历史热启动差异。', '',
        '| 核数 | 对照 | 匹配数 | 平均加速比相对变化 | 更好/相同/更差 |','|---|---|---:|---:|---|']
    for n in sorted({r['cores'] for r in rows}):
        for name,label in [('paused','暂停前统一版'),('personal','原个人'),('base','基座')]:
            rr=[r for r in rows if r['cores']==n and r.get('change_vs_'+name) is not None]
            if not rr:continue
            newer=statistics.mean(r['singlecore_cycles']/r['cycles'] for r in rr)
            older=statistics.mean(r['singlecore_cycles']/r[name+'_cycles'] for r in rr)
            dd=[r['change_vs_'+name] for r in rr]
            lines.append(f'| {n} | {label} | {len(rr)} | {newer/older-1:+.2%} | {sum(v<0 for v in dd)}/{sum(v==0 for v in dd)}/{sum(v>0 for v in dd)} |')
    lines+=['','下表 cycles 变化为新/旧−1，负值更好。未完成项不计入均值，超时但有保底结果的项计入。','',
        '| case | 核数 | 新 cycles | 评分来源 | 对暂停版 | 对个人 | 实际秒 | 补计总秒 | 状态 | 重放 |',
        '|---|---:|---:|---|---:|---:|---:|---:|---|---|']
    pct=lambda v:'—' if v is None else f'{v:+.2%}'
    for r in rows:
        lines.append(f"| {r['case']} | {r['cores']} | {r['cycles']} | {r['score_source']} | {pct(r['change_vs_paused'])} | {pct(r['change_vs_personal'])} | {r['wall_seconds']:.2f} | {r['accounted_wall_seconds']:.2f} | {r['status']} | {'通过' if r['independent_replay'] else '待重放'} |")
    (ROOT/'docs/q1/feature_validation.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(f'{len(rows)}/{a.expected} completed')


if __name__=='__main__':main()
