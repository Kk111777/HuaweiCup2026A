"""Audit a completed Q3 run and compare against its freshly re-evaluated warm plans."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics

from q3v3.official import ROOT, sha256, write_json, metrics, read_settings, _validate_replay
from q3v3.objective import key
from q3v3.traffic_objective import TrafficPolicy, select
from q3v3.state import Graph, PlanState


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def report(output):
    output = Path(output).resolve()
    manifest, rows = read(output/'run_info.json'), read(output/'summary.json')
    if manifest['status'] != 'complete' or len(rows) != manifest['expected']:
        raise ValueError('run is incomplete')
    for name, digest in manifest['source_hashes'].items():
        if sha256(ROOT/name) != digest:
            raise ValueError(f'current source differs from run: {name}')
    counts, accepted, stops = Counter(), Counter(), Counter()
    comparisons = []
    checked_q1 = set()
    checked_current_q1 = set()
    q1_changed_files = set()
    reexported = 0
    graphs = {Path(p).stem:Path(p) for p in manifest['graphs']}
    for row in rows:
        if row['status'] != 'ok':
            raise ValueError('failed run row')
        folder = output/row['case']/f"N{row['num_cores']}"
        if (sha256(graphs[row['case']]) != row['source_hashes']['graph']
                or sha256(manifest['config']) != row['source_hashes']['config']):
            raise ValueError('input/config changed since evaluation')
        origin = row['q1_source_provenance']
        if origin is None:
            complete = row.get('warm_start_mode') == 'complete_solution_portfolio'
            if (row['singlecore_score_used'] or not (complete or
                    (row.get('warm_start_mode') == 'full_plan_only' and 'warm_plan' in row['source_hashes']))):
                raise ValueError('missing warm-only provenance')
            if complete and (not row.get('seed_paths') or any(
                    sha256(path)!=row['seed_hashes'][name] for name,path in row['seed_paths'].items())):
                raise ValueError('full solution seed identity changed')
            changes = []
        else:
            changes = check_q1_origin(output, folder, row, manifest, checked_q1, checked_current_q1,
                                      q1_changed_files)
        reexported += bool(changes)
        result = read(folder/'verified/result.json')
        trace = read(folder/'verified/trace.json')
        history = read(folder/'history.json')
        if (sha256(folder/'plan.json') != row['plan_sha256']
                or result['makespan'] != row['makespan_cycles']
                or trace['otherData']['makespan'] != result['makespan']
                or result['input_graph'] != row['case']+'.json'
                or result['num_cores'] != row['num_cores']):
            raise ValueError(f'identity/hash/result mismatch in {folder}')
        supplied = [r for r in history if r['name'] in ('provided_q1_plan','q1') and r['status']=='ok']
        old_result = read(folder/supplied[0]['evaluation_directory']/'result.json') if supplied else None
        policy = row.get('objective_policy', 'cycles-copy')
        if policy=='cycles-traffic':
            audit_traffic(folder,graphs[row['case']],manifest['config'],row,result,history)
        else:
            if old_result is not None and key(result, policy) > key(old_result, policy):
                raise ValueError('result regressed from re-evaluated warm start')
            if result['makespan'] > row['seed_makespan']:
                raise ValueError('result regressed from best seed')
        old = old_result['makespan'] if old_result else row['seed_makespan']
        gain = 100*(1-result['makespan']/max(1,old))
        state = 'faster' if result['makespan'] < old else ('equal' if result['makespan']==old else 'slower')
        counts[state] += 1
        stops[row['stop_reason']] += 1
        accepted.update(r['family'] for r in history if r.get('accepted') and r['family'] != 'seed')
        comparisons.append(dict(case=row['case'],cores=row['num_cores'],initial_cycles=old,
            final_cycles=result['makespan'],reduction_percent=gain,seed_cycles=row['seed_makespan'],
            winner=row['winner'],official_calls=row['official_calls'],stop_reason=row['stop_reason'],
            cache_hit_rate_bytes=row['cache_hit_rate_bytes'],
            initial_hit_rate_bytes=old_result['cache_stats']['hit_rate'] if old_result else None,
            spill_added_copy_bytes=row['spill_added_copy_bytes'],
            preparation_seconds=row['preparation_seconds'],search_seconds=row['search_seconds'],
            final_replay_seconds=row['final_replay_seconds'],total_seconds=row['total_seconds']))
    aggregate = dict(status='passed',count=len(rows),counts=dict(counts),accepted_families=dict(accepted),
        stops=dict(stops),median_reduction_percent=statistics.median(r['reduction_percent'] for r in comparisons),
        mean_reduction_percent=statistics.mean(r['reduction_percent'] for r in comparisons),
        elapsed_seconds=manifest['elapsed_seconds'],comparisons=comparisons,
        note='Equal-weight case/core percentages; not a competition total score or a speedup geomean.')
    if len(checked_q1) > 1 and reexported != len(rows):
        raise ValueError('Mixed Q1 versions require reproducing every exported seed contract')
    aggregate.update(q1_source_versions=sorted(checked_q1),current_q1_source_versions=sorted(checked_current_q1),
                     q1_reexported_identical=reexported,q1_changed_files=sorted(q1_changed_files))
    write_json(output/'AUDIT.json',aggregate)
    return write_report(output, manifest, rows, comparisons, aggregate, counts, stops, accepted,
                        checked_q1, reexported)


def audit_traffic(folder,graph_path,config,row,result,history):
    """Recompute contextual selection and both complete official replays."""
    policy=TrafficPolicy.from_record(read(folder/'policy.json'))
    graph=Graph(read(graph_path));settings=read_settings(config)
    candidates=[]
    for h in history:
        if h['status']!='ok': continue
        p=folder/h['evaluation_directory']
        state=PlanState(graph,read(p/'plan.json'));r=read(p/'result.json')
        if state.digest!=h['state_digest'] or any(h[k]!=v for k,v in metrics(r).items()):
            raise ValueError('candidate history identity or metrics mismatch')
        candidates.append(dict(digest=state.digest,result=r))
    chosen,fastest=select(candidates,policy)
    for base,expected in [(folder,chosen),(folder/'fastest',fastest)]:
        replay_result=read(base/'verified/result.json');meta=read(base/'verified/replay.json')
        _validate_replay(replay_result,read(base/'verified/trace.json'),
            (base/'verified/official.log').read_text(encoding='utf-8'),Path(graph_path).name,'plan.json',settings)
        if (meta['status']!='ok' or meta['plan_sha256']!=sha256(base/'plan.json')
                or meta['graph_sha256']!=sha256(graph_path) or meta['config_sha256']!=sha256(config)
                or PlanState(graph,read(base/'plan.json')).digest!=expected['digest']
                or metrics(replay_result)!=metrics(expected['result'])):
            raise ValueError('delivered selection/fastest plan differs from verified history')
    if (not policy.eligible(result,fastest['result']['makespan']) or
            row['objective']!=policy.record(result,fastest['result']['makespan']) or
            row['fastest_metrics']!=metrics(fastest['result']) or
            row['fastest_plan_sha256']!=sha256(folder/'fastest/plan.json')):
        raise ValueError('traffic objective or cycle guard mismatch')


def check_q1_origin(output, folder, row, manifest, checked_q1, checked_current_q1, q1_changed_files):
    origin = row['q1_source_provenance']
    if manifest.get('q1_snapshot') and origin['q1_source_sha256'] != manifest['q1_snapshot']['source_sha256']:
        raise ValueError('case used a different Q1 source snapshot from the batch')
    if origin['q1_scoring_calls'] or origin['baseline_used'] or row['singlecore_score_used']:
        raise ValueError('single-core score unexpectedly entered the Q3 pipeline')
    repository = Path(origin['q1_source_root']).parent
    changes = [p for p,d in origin['q1_source_files'].items() if sha256(repository/p) != d]
    q1_changed_files.update(changes)
    checked_q1.add(origin['q1_source_sha256'])
    if changes:
        # A user's concurrent Q1 edit is not silently ignored. Require an
        # independently regenerated, identical bridge contract using the
        # current source, while preserving the historical run fingerprints.
        fresh = read(output/'current_q1_reexport'/row['case']/f"N{row['num_cores']}"/'q1_seeds.json')
        original = read(folder/'q1_seed/q1_seeds.json')
        if any(fresh[k] != original[k] for k in ('plans','features','block_candidates')):
            raise ValueError('Current Q1 exports a different seed/feature contract; rerun required')
        p = fresh['provenance']
        if (p['input_sha256'] != origin['input_sha256'] or p['config_sha256'] != origin['config_sha256']
                or p['cores'] != row['num_cores'] or p['q1_scoring_calls'] or p['baseline_used']):
            raise ValueError('Q1 re-export identity mismatch')
        if p['q1_source_sha256'] not in checked_current_q1:
            if any(sha256(repository/f) != d for f,d in p['q1_source_files'].items()):
                raise ValueError('Q1 source changed again after compatibility re-export')
            checked_current_q1.add(p['q1_source_sha256'])
    return changes


def write_report(output, manifest, rows, comparisons, aggregate, counts, stops, accepted, checked_q1, reexported):
    lines = [f"# {manifest.get('algorithm', 'Q3')} 通用核调度实测与审计", '',
        f"完成 {len(rows)} 份方案；比同一传入方案的 Q3 重评结果更快 {counts['faster']} 份，持平 {counts['equal']} 份，稍慢 {counts['slower']} 份。",
        f"批次墙钟 {manifest['elapsed_seconds']:.2f} 秒；{manifest['workers']} 个主机 case 进程并行。", '',
        '对照双方均使用官方问题3配置。因此下表衡量 Q3 搜索带来的额外改善，不混入 Q1→Q3 执行语义变化。', '',
        '|case|核数|原方案 Q3 cycles|新方案 Q3 cycles|降低|求解合计秒|胜出动作|',
        '|---|---:|---:|---:|---:|---:|---|']
    for r in comparisons:
        lines.append(f"|{r['case']}|{r['cores']}|{r['initial_cycles']:,}|{r['final_cycles']:,}|{r['reduction_percent']:.3f}%|{r['total_seconds']:.2f}|{r['winner']}|")
    timing = []
    for case in sorted({r['case'] for r in comparisons}):
        subset=[r for r in comparisons if r['case']==case]
        timing.append(dict(case=case, total_seconds=sum(r['total_seconds'] for r in subset),
            preparation_seconds=sum(r['preparation_seconds'] for r in subset),
            search_seconds=sum(r['search_seconds'] for r in subset),
            final_replay_seconds=sum(r['final_replay_seconds'] for r in subset)))
    aggregate['case_timing']=timing
    write_json(output/'AUDIT.json',aggregate)
    lines += ['', '## 求解用时', '',
        'cycles是模拟NPU执行周期；秒是本机求解墙钟。各case内部核数串行，各case之间并行，逐例时间不能相加当成批次墙钟。', '',
        '|case|准备/种子秒|搜索秒|最终复核秒|各核数合计秒|',
        '|---|---:|---:|---:|---:|']
    for r in timing:
        lines.append(f"|{r['case']}|{r['preparation_seconds']:.2f}|{r['search_seconds']:.2f}|{r['final_replay_seconds']:.2f}|{r['total_seconds']:.2f}|")
    if manifest.get('q1_snapshot'):
        snap=manifest['q1_snapshot']
        lines += ['', f"Q1快照仅在批次开始捕获一次，用时{snap['capture_seconds']:.3f}秒，包含{snap['file_count']}个源码/配置文件。",
                  f"全部{len(rows)}组种子使用同一快照指纹 `{snap['source_sha256']}`；后续上游源码编辑不影响本批结果。", '']
    lines += ['', '## 验证与解释', '',
        '- 每份最终方案经过独立官方 CLI 重放，保留完整 trace/log；本报告检查标识、方案哈希、周期和运行时记录的目标。cycles-traffic 还重新选择全部已评估候选并核验 fastest。',
        '- Q3代码及官方源文件哈希与运行记录一致。完整方案模式不执行Q1源码；纯图种子模式另有逐次导出指纹。',
        '- 跨核数运行会继承较少核心的已优化方案，因此 seed_makespan 与传入同核数方案可能不同。',
        '- cycles-traffic 使用固定种子归一化的时间/搬运加权目标，cycles 受动态最快值的容忍上限保护。cycles-hit/cycles-copy 按历史严格字典序审计。',
        '- 接近计算下界时仍可进行有界缓存调度搜索；未改善不证明已达最优。',
        '- accepted_families 是被接受动作计数，不能用来直接做独立收益归因；L2提案效果需看单独开关消融。',
        '- 缓存命中增加不保证总时间下降，单核成绩没有进入本次特征和搜索。', '',
        f"停止原因：`{dict(stops)}`。接受的搜索家族：`{dict(accepted)}`。", '']
    if reexported:
        lines += ['## 当前 Q1 兼容性复核', '',
            f'运行期间上游Q1有独立更新，原始导出记录中存在{len(checked_q1)}个全目录指纹，未将其改写为单一版本。',
            f'使用当前Q1重新导出{reexported}组纯图结果，逐组比较plans、features、block_candidates，全部与原运行完全一致。',
            '因此这些源码更新没有改变本轮实际使用的桥接输入。新增导出文件及逐文件指纹保存在current_q1_reexport；审计再次核对它们与当前源码一致。',
            f'发生变化的文件：`{aggregate["q1_changed_files"]}`。', '']
    (output/'COMPARISON.md').write_text('\n'.join(lines),encoding='utf-8')
    return aggregate


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    args = parser.parse_args()
    result = report(args.output)
    print(json.dumps({k:v for k,v in result.items() if k!='comparisons'},ensure_ascii=False,indent=2))
