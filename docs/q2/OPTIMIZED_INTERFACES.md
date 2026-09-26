# Q2 优化接口与复现

版本 `Q2-Optimized-3.0`（v3），输出格式 `schema_version=1`。Python 3.12，无新增第三方依赖。原 v2.1 的基座源码、配置和入口继续保留；见[版本映射](VERSIONS.md)。

## 入口

在仓库根目录执行（`python` 应指向 Python 3.12）：

```powershell
# 单图；四种核数共用 600/900/1200 秒预算
python src/solve_problem2_optimized.py solve official/data/case_004.json --cores 2 3 4 5 --output outputs/q2-opt/case_004

# 有限验收；每个指定组合最多两批，然后独立官方 CLI 复评
python tools/q2_optimized_pilot.py --output outputs/q2-opt-pilot

# 研究模式：关闭基座保底和中途校准；允许估计输出，不保证优于基座
python src/solve_problem2_optimized.py solve official/data/case_004.json --cores 2 --baseline-guard off --calibration off --output outputs/q2-opt-no-calibration

# 只发布已取得精确成绩的最好状态；没有时明确失败
python src/solve_problem2_optimized.py solve official/data/case_004.json --cores 2 --output-policy best-exact --output outputs/q2-opt-exact

# 冻结输出的独立复评：不重新搜索或改选
python src/solve_problem2_optimized.py replay official/data/case_004.json --snapshot outputs/q2-opt/case_004/N2 --output outputs/q2-opt-replay/case_004/N2

# 全量入口（本轮未执行）
python src/solve_problem2_optimized.py batch --data-dir official/data --cores 2 3 4 5 --workers 1 --output outputs/q2-opt-full
# 仅身份完全一致、完整实例已结束时复用；未完成实例需使用新目录重跑
python src/solve_problem2_optimized.py batch --data-dir official/data --cores 2 3 4 5 --workers 1 --output outputs/q2-opt-full --resume
```

13 组参考集合的工程复现命令如下，**本轮未执行**。它从原始 DAG 出发，不导入原型基座计划；不等同于重跑原型程序。各条默认运行至搜索终止或预算上限。

```powershell
$pairs = @('001/N5','002/N2','002/N5','003/N5','004/N2','004/N5','010/N2','010/N5','048/N2','048/N5','059/N5','086/N2','086/N5')
foreach ($pair in $pairs) {
    $parts = $pair -split '/N'
    python src/solve_problem2_optimized.py solve "official/data/case_$($parts[0]).json" --cores $parts[1] --output "outputs/q2-opt-13/case_$($parts[0])_N$($parts[1])"
}
```

## 组件合同

| 模块 | 接口与职责 |
|---|---|
| `contracts` | `Proposal` 保存候选来源、参数、父状态和可选 β 流；`ExecutionPreparation` 保存不可变准备结果；`ModelTrace` 保存两层预测及细层操作时序；`OptimizationResult` 返回双 incumbent、终止和轨迹 |
| `prepared` | `prepare_execution(service,state)` 复用真实 Step1/2/3，检查全局依赖，不调用完整官方模拟；返回副本，不暴露缓存可变引用 |
| `preparation` | v3 专用 `PreparationCache/PreparationService` 继承 v2.1 组件，增加执行准备和模型缓存，不改写原基座类 |
| `approximation` | `ModelEvaluator.evaluate(state,level)`；`Calibration.observe(trace,official_score)`；`rescore` 只更新校准，无需重复展开 |
| `proposals` | `order_proposals`、`rebuild_proposals`、`feedback_proposals`、`block_proposals`、`capacity_proposals`；不修改父状态 |
| `construction` | `Construction.next/observe/record`：构造共享、流独立、β 只接受精确反馈 |
| `service` | `EvaluationService.state/refined/official/cached_exact`：状态/保真验证、共享准备、精确数值复用和完整证据落盘 |
| `pool` / `engine` | 有界方案池、公平配额、固定父状态批次、精确反馈与校准队列 |
| `protection` | 默认基座保底：共享构造和评价，完成基座候选后局部搜索，发布前精确择优 |
| `runtime` / `api` | 图实例共享预算、核数轮转、检查点恢复、完整实例复用、冻结输出独立回放 |
| `backend` | 完整 `ProblemDefinition` 四组接口；公共 `CaseRuntime` 可选策略接入 Q2 实例级执行与恢复 |

公共调用方式：

```python
from scheduling.api import solve_case
result = solve_case(graph_path, config_path, [2, 3, 4, 5], output_path,
                    seconds=600, problem='q2')
```

调用方也可直接使用 `q2.optimized.api.solve_case(..., options={...})`。默认参数集中在 `configs/q2/optimized.json`。所有候选的核心归属和实际计算序列从当前计划恢复，父草案不参与评价或搬运统计。

用户在首轮试跑后追加了“至少保证优于新基座”的要求，因此默认 `baseline_guard=true`。默认发布以官方成绩确认的最好方案，实际保证是“不退化”，严格改善需要实验支持。`baseline_guard.complete` 记录是否完成当次全部基座候选；未完成时不能宣称完整基座不退化保证。`calibration=off` 只关闭搜索中的模型校准，不关闭保底所需正式评价；如需纯估计搜索，两者均须显式关闭。

## 缓存与输出

Step1、驻留、准备、模型及精确数值缓存共用默认 64 MiB 上限，并有分类条目限制。执行等价缓存只复用数值与无标签指标；不会将别的计划带旧 sgid 的完整 JSON 当成当前结果。完整官方 JSON 留在本次输出的 `official/eval_*`，记录其哈希与原始计划身份。任何缓存命中都要先验证当前状态与保真声明。

每核输出计划仅含题面两个字段 `node_to_subgraph`、`core_schedules`。旁文件包括 `state.json`、`score.json`、`legality.json`、`pool.json`、`beta.json`、`timings.json`、`candidate_log.jsonl`、`checkpoint.json`、`frozen_manifest.json`。实例包含输入与源码指纹、各核覆盖、共享预算、墙钟及 CPU 计时。

`SUCCESS_ESTIMATED` 和 `SUCCESS_EXACT_UNREPLAYED` 都不是本轮独立复评通过标记。回放输出另存 `verified.json`；失败或精确证据不一致时不写该文件。`complete.json` 表示本次配置的全部核数运行正常结束，**不表示穷举完成**；例如两批限制输出中 `search_complete=false`。

离线比较入口（求解器从不调用此工具）：

```powershell
python tools/q2_optimized_compare.py outputs/q2-opt-pilot --output outputs/q2-opt-comparison
# 可选：需用户提供完整原型证据包及原始 case_059
python tools/q2_optimized_false_promotion.py --reference-zip "D:/Microsoft/Downloads/问题2_改进模型_试跑与修正完整证据.zip" --output outputs/q2-opt-059-control
```

外层进程监督覆盖同步构造和官方准备，超时只回收本程序的子进程树；通过已经完成的合法检查点恢复，不接受未完成的候选。`--resume` 仅跳过身份完全一致的完整图实例，不承诺恢复任意内部迭代位置。

## 兼容范围

- Q1 默认后端、搜索判据和等待规则不变；公共框架仅添加可选实例策略。
- `q2` 原基座 CLI、Q2 2.0 归档、Q2 2.1 状态格式及 1.1 对照保留。
- 优化批跑使用独立入口和格式；估计输出不会被基座正式汇总或其 `--resume` 接受。
- 本模型没有共享输入片段聚合、部分减压、sgid 压缩，也没有宣称近似模型全域等价于官方评价。
- 仅支持已有 Q2 标准二部输入合同；不把任意带中间 DDR 回路的图静默改写为标准输入。
- 发布前试跑使用内部版本 `Q2-Optimized-1.0`，其冻结输出与证据保持原标记；v3 不将这些输出或 v2.1 输出当作可续跑实例。历史效果证据读取已归档 CSV/JSON，不改写旧清单来绕过版本检查。

组件验证使用 `python -m unittest discover -s tests -v`。验收情况见 `OPTIMIZED_ACCEPTANCE.md`，算法公式见 `OPTIMIZED_MODEL.md`。
