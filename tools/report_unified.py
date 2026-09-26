"""Generate the technical acceptance report from completed, independently replayed outputs."""
import argparse
import csv
import json
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from q1.unified.io import read,write,digest
from q1.unified.api import source_signature


def case_timing_rows(run, rows, manifest, source):
    """Read the case clock; per-core windows cannot reconstruct total runtime."""
    if manifest.get('budget_scope') != 'case_all_cores' or manifest.get('singlecore_in_solver') is not False:
        raise ValueError('requires case_all_cores outputs; legacy per-core runs are historical only')
    result = []
    for case in sorted({r['case'] for r in rows}):
        rr = sorted((r for r in rows if r['case'] == case), key=lambda r:r['cores'])
        summary = read(run/case/'summary.json')
        request = read(run/case/'request.json')
        if (summary.get('source_signature') != source or request.get('source_signature') != source
                or summary.get('budget_scope') != 'case_all_cores'
                or request.get('budget_scope') != 'case_all_cores'
                or summary.get('cores') != manifest['cores']
                or request.get('cores') != manifest['cores']
                or summary.get('target_seconds') != manifest['target_seconds']
                or request.get('seconds') != manifest['target_seconds']
                or summary.get('baseline_seconds') != 0):
            raise ValueError(f'{case}: inconsistent case budget or source')
        line = {name:summary[name] for name in (
            'wall_seconds', 'cpu_seconds', 'shared_preparation_seconds', 'budget_scope',
            'target_seconds', 'extension_seconds', 'hard_limit_seconds',
            'exceeded_target', 'extended', 'exceeded_extension', 'hard_timeout', 'status')}
        line.update(case=case, target_count=len(rr),
            sum_target_wall_seconds=sum(r['wall_seconds'] for r in rr),
            sum_target_cpu_seconds=sum(r.get('cpu_seconds') or 0 for r in rr),
            hard_timeout_cores=','.join(str(r['cores']) for r in rr if r.get('hard_timeout')))
        for r in rr:
            n=r['cores'];line[f'N{n}_seconds']=r['wall_seconds'];line[f'N{n}_cycles']=r.get('cycles')
            line[f'N{n}_accounted_seconds'] = r.get('accounted_wall_seconds')
            for name in ('cpu_seconds', 'exceeded_target', 'extended', 'status'):
                line[f'N{n}_{name}'] = r.get(name)
        result.append(line)
    return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--report',type=Path,default=ROOT/'docs/q1/unified_acceptance.md')
    p.add_argument('--probe','--fresh-probe',dest='probe',type=Path,nargs='*',default=[],
                   help='Optional independently replayed single-count probes; no singlecore prerequisite')
    a=p.parse_args();comparison=read(a.run/'comparison.json');execution=read(a.run/'results.json')
    rows=comparison['rows'];valid=[r for r in rows if r.get('cycles') is not None]
    manifest=execution['manifest']
    expected={(f'case_{i:03d}',n) for i in manifest['cases'] for n in manifest['cores']}
    observed=[(r['case'],r['cores']) for r in rows]
    if len(set(observed))!=len(observed):raise ValueError('duplicate case/core in report')
    if set(observed)-expected:raise ValueError('unexpected case/core in report')
    missing=sorted(expected-set(observed))
    source=source_signature()
    if any(r['source_signature']!=source for r in valid):raise ValueError('report requires the final solver source')
    case_rows=case_timing_rows(a.run,rows,manifest,source)
    verified=[]
    cooperation=[]
    for r in valid:
        marker=read(a.run/r['case']/f"N{r['cores']}"/'verified.json')
        assert marker['independent_official_replay'] is True
        assert marker['source_signature']==source
        folder=a.run/r['case']/f"N{r['cores']}"
        assert marker['plan_sha256']==digest(folder/'plan.json')
        assert marker['result_sha256']==digest(folder/'evaluation.json')
        realized=folder/('official_replay.json' if marker.get('official_replay_sha256') else 'evaluation.json')
        if marker.get('official_replay_sha256'):assert digest(realized)==marker['official_replay_sha256']
        assert read(realized)['makespan']==r['cycles']
        verified.append(marker)
        if (folder/'search.json').exists():
            history=read(folder/'search.json');origins={}
            accepted={item['index']:item for item in history.get('accepted',[])}
            for record in history.get('records',[]):
                pd=record.get('plan_digest');parent=record.get('parent_plan_digest')
                origin=record.get('action') if record.get('action','').startswith('growth_') else origins.get(parent)
                if pd and origin:origins.setdefault(pd,origin)
                if parent and origins.get(parent) and record.get('accepted') and record['index'] in accepted:
                    cooperation.append(dict(case=r['case'],cores=r['cores'],
                        base_origin=origins[parent],personal_action=record['action'],
                        evaluation_source=record.get('evaluation_source','official'),
                        parent_plan_digest=parent,plan_digest=pd,
                        accepted_objective=accepted[record['index']]['objective']))
    write(a.run/'module_cooperation.json',dict(accepted_base_descendants=cooperation,
        note='Search trace evidence, not an ablation or independent estimate of module contribution.'))
    fields=list(dict.fromkeys(k for r in case_rows for k in r))
    with (a.run/'case_times.csv').open('w',encoding='utf-8-sig',newline='') as s:
        w=csv.DictWriter(s,fieldnames=fields);w.writeheader();w.writerows(case_rows)
    lines=['# 统一架构验收报告','',
        f"执行覆盖：{len(rows)}/{len(expected)} 个请求的 case×核数；有效方案 {len(valid)} 个；独立官方重放 {len(verified)} 个。",
        f"缺失组合：{len(missing)}；批次状态：{execution.get('status')}；运行异常：{len(execution.get('errors',[]))}。",
        f"本次执行/续跑阶段历时：{execution.get('elapsed_seconds',0):.2f} 秒；跨 case 本地并发 {execution['manifest']['local_test_workers']}。单个求解串行。",
        '',f'代码指纹：`{source}`。本次来源以运行目录 request.json 和 benchmark.json 为准；unified_source.json 是旧版历史记录。',
        '', '## 结构与正确性','',
        '- 一份图模型、公共商图与队列校验、一个列表调度内核、一个官方评估服务和搜索状态池。',
        '- 协作者 Task 生长构造与个人结构/局部/区域算子在同一个控制器中调用，没有两套完整算法求解后择优。',
        '- 官方附件未修改；求解器允许近似结果成为最终输出，下表成绩使用结束后独立官方重放的 makespan。原始近似分数保留在 evaluation.json，不伪装成官方结果。',
        '- 当前接口与合并验证范围见 docs/q1/merge_review.md；历史近似审计不能冒充本次运行测试。',
        '',f"运行轨迹中，个人算子对协作者构造方案及其后继方案产生了 {len(cooperation)} 次接纳，其中近似接纳 {sum(r['evaluation_source']=='approximate' for r in cooperation)} 次；完整父方案指纹、评分来源与动作见 `module_cooperation.json`。近似接纳不等同于官方成绩提升，也不是消融实验。",
        '', '## 成绩','',
        '平均加速比按逐 case 的单核/多核比值取算术平均。下表 cycles 变化为相对原个人算法，负值表示更好；缺失方案不冒充完整 100 例平均。',
        '', '| 核数 | 有效数 | 平均加速比 | 平均 cycles 变化 | 最坏 cycles 变化 | 退化超过1%的项数 |',
        '|---|---:|---:|---:|---:|---:|']
    for n in manifest['cores']:
        rr=[r for r in valid if r['cores']==n];ratios=[r['change_vs_personal'] for r in rr if r['change_vs_personal'] is not None]
        speeds=[r['speedup'] for r in rr if r.get('speedup') is not None]
        speed=f"{statistics.mean(speeds):.6f}" if len(speeds)==len(manifest['cases']) else '未完整覆盖或缺少报告分母'
        mean=f'{statistics.mean(ratios)*100:+.3f}%' if ratios else '无对照'
        worst=f'{max(ratios)*100:+.3f}%' if ratios else '无对照'
        lines.append(f'| {n} | {len(rr)} | {speed} | {mean} | {worst} | {sum(x>.01 for x in ratios)} |')
    lines+=['','| 对照 | 新版更好 | 相同 | 新版更差 |','|---|---:|---:|---:|']
    for label,field in [('原个人算法','change_vs_personal'),('原基座算法','change_vs_base')]:
        values=[r[field] for r in valid if r.get(field) is not None]
        lines.append(f'| {label} | {sum(v<0 for v in values)} | {sum(v==0 for v in values)} | {sum(v>0 for v in values)} |')
    transition=a.run/'concurrency_transition.json'
    if transition.exists():
        segment=read(transition)
        lines+=['',f"本地并发按用户要求从 {segment['previous_workers']} 调整为 {segment['next_workers']}。切换前运行约 {segment['elapsed_seconds']:.2f} 秒，已有 {segment['completed_targets']} 项完成并通过官方重放后复用；{len(segment['interrupted_targets'])} 项未完成尝试已保留并重新排队。逐例表为最终成功或保护超时的单次尝试耗时，不把被中断尝试混入算法成绩；批次总开销还包括切换核验和中断重算。"]
    regressed=sorted((r for r in valid if (r.get('change_vs_personal') or 0)>.01),key=lambda r:-r['change_vs_personal'])
    lines+=['','当前求解从输入图生成结构种子，各核数共享本次图准备与校准，不读取历史多核方案。历史对照的预算与热启动条件须另行核对。',
        '', '### 退化超过 1% 的逐项记录','',
        '| case | 核数 | 原个人 cycles | 新 cycles | 变化 | 实测秒数 |',
        '|---|---:|---:|---:|---:|---:|']
    for r in regressed:lines.append(f"| {r['case']} | {r['cores']} | {r['personal_cycles']} | {r['cycles']} | {r['change_vs_personal']*100:+.3f}% | {r['wall_seconds']:.2f} |")
    if not regressed:lines.append('| 无 | | | | | |')
    lines+=['','## 时间与超时','',
        f"单个case的全部请求核数合计目标 {manifest['target_seconds']:g} 秒，有条件延至目标的1.5倍，已启动工作受2倍整case保护上限约束。",
        '求解不前置运行单核，不补计历史单核耗时；可选单核归档只在报告层提供加速比分母。独立官方重放属于离线验收，耗时另列。',
        '整case用时读取根 summary.json.wall_seconds，包含共享准备和监督开销；逐核用时之和仅作明细，不能替代整case时间。',
        '',f"超过整case目标：{sum(r['exceeded_target'] for r in case_rows)} 例；保护超时：{sum(r['hard_timeout'] for r in case_rows)} 例。",
        f"触发改进延长：{sum(r['extended'] for r in case_rows)} 例；超过延长窗口：{sum(r['exceeded_extension'] for r in case_rows)} 例。",
        '', '| case | 核数 | 状态 | 实测秒数 | 兼容计时秒数 | cycles |', '|---|---:|---|---:|---:|---:|']
    for r in rows:
        if r.get('hard_timeout') or r['status']!='ok':
            lines.append(f"| {r['case']} | {r['cores']} | {r['status']} | {r['wall_seconds']:.2f} | {r.get('accounted_wall_seconds',0):.2f} | {r.get('cycles','无方案')} |")
    if not any(r.get('hard_timeout') or r['status']!='ok' for r in rows):lines.append('| 无 | | | | | |')
    if a.probe:
        lines+=['','### 当前版本单目标调用检查','',
            '每项只请求一种目标核数，不调用单核基线。这些独立调用不能拼成共享预算下的四核数结果。',
            '', '| 输入 | 核数 | 单核前置秒数 | 总秒数 | 求解cycles | 评分来源 | 状态 |',
            '|---|---:|---:|---:|---:|---|---|']
        for folder in a.probe:
            result=read(folder/'summary.json');req=read(folder/'request.json');marker=read(folder/'verified.json')
            assert result['source_signature']==source and marker['source_signature']==source
            assert result['timing']['singlecore_seconds']==0 and marker['independent_official_replay'] is True
            assert marker['plan_sha256']==digest(folder/'plan.json')
            assert marker['result_sha256']==digest(folder/'evaluation.json')
            lines.append(f"| {Path(req['graph']).stem} | {req['cores']} | {result['timing']['singlecore_seconds']:.2f} | {result['wall_seconds']:.2f} | {result['makespan']} | {result['score_source']} | {result['status']} |")
    lines+=['','逐核结果见运行目录的 `timings.csv`、`comparison.csv`；整case实际耗时及逐核明细见 `case_times.csv`。覆盖范围以上面的请求与缺失数为准，部分集合不能冒充完整100case。复用行保留原实测时间，不代表本批重算。',
        '', '## 交付和基座改动','',
        '彩色架构、调用接口及逐项基座变动见 `unified.md`。默认入口改为统一串行求解；原串行 initialize 保留作基座对照。Task 生长公式未改，GraphIndex 改成共享模型视图，EFT 调度合并到公共内核；不同策略的平局规则保留。',
        '搜索轮数和结构参数以运行记录中的有效配置为准，受整case共享预算约束。全量脚本为 `scripts/run_unified_100.ps1`。',
        '代码提交及合并状态以Git仓库记录为准，本报告只说明已核验运行结果。', '']
    a.report.parent.mkdir(parents=True,exist_ok=True)
    a.report.write_text('\n'.join(lines),encoding='utf-8')
    print(a.report.resolve())


if __name__=='__main__':main()
