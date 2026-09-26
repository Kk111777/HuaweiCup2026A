# Q2 2.1：当前状态、评价证据与搜索快照

版本 `Q2-Repair-2.1`，状态格式 `schema_version=2`，Python 3.12、标准库。
2.1保留在 `fix/Zysishuiyears/q2-component-interfaces` 独立分支及草稿PR中，供后续第三版继续开发；当前正式基座实验仍使用main中的2.0，本次上传不合并替换它。
本次保留[2.0修复版算法](repair.md)的构造公式、候选家族及顺序、adaptive β规则、容量最小扰动DP和正式指标择优规则。
改动集中于组件接口和重复计算复用；没有实现近似模型或下一轮优化搜索。

在输入、配置相同、两版都完成同一搜索且没有超时或资源失败时，预期最终计划和官方指标相同。
源码指纹、状态格式、日志和官方调用次数会变化。如果受墙钟预算截断，缓存可能改变已完成候选集合，不能保证结果仍相同。
三个小图的实际对照和回归范围见[有限验证报告](interfaces_validation.md)。

## 当前状态与来源

`ConstructionDraft` 只记录最初构造过程；原 `search_blocks` 改名为 `construction_blocks_ops`。
迁核、重排、块拆合后，应通过 `derive_state` 生成新的 `ScheduleState`，不修改父状态或仅替换候选JSON。
评价中的核心归属、基础搬运量、分量拆分统计及保真检查，均从当前计划重建。

| 合同 | 字段及语义 |
|---|---|
| `ScheduleState` | 当前 `plan_json`、`core_of_op`、`compute_orders_by_core`、`realized_blocks_by_core`、三类身份、环境身份、`parent_signature`、`origin` |
| `BlockState` | `block_id`、稠密计算索引 `ops`、`core`、有序 `sgids`、核内块序号 `position`、`origin_block_ids` |
| `CandidateOrigin` | `family`、`mode`、`beta`、`encoding`；不参与搜索状态身份 |
| `EncodedCandidate` | 未准备的计划提案可以保留可选 `draft`；准备后携带权威的 `state` |

`ops` 和 `core_of_op` 的下标属于图索引 `Graph` 的稠密计算索引；`g.ids[v]` 才是原始操作ID。
sgid是提交计划中的子图编号，与前两者不同，即使数值碰巧相同也不能互换。
状态及块使用冻结数据类和元组，JSON属性每次返回独立副本；外部序列化用 `dataclasses.asdict`，还原用 `ScheduleState.from_record`。

`build_state(g, plan, preparation=...)` 根据当前计划和真实Step1生成核心映射、逐核计算投影及连续块。
可选 `construction_blocks_ops` 仅帮助保留构造边界；默认每段目标最多32个计算操作，不拆开已有sgid。
显式 `blocks_by_core` 可以是逐核 `BlockState` 列表或逐核sgid组列表，必须覆盖所有核心（含空核心）、按实际顺序完整覆盖每个sgid及计算操作。
`validate_state` 重新构建并核对整个状态，拒绝过期归属、顺序、块或环境身份。
`derive_state` 按计算操作交集记录块来源，避免将碰巧同号的sgid当作来源关系。

## 三类身份与环境

| 字段 | 内容与用途 |
|---|---|
| `plan_digest` | 完整提交映射和核心队列；保留映射插入顺序，因为官方构图遍历此顺序。忽略JSON空白及无关字段排版 |
| `state_signature` | 环境、当前分核、逐核计算顺序、以操作成员表示的子图及块边界，以及执行身份；排除纯标签、来源和评分 |
| `execution_digest` | 环境、逐核实际图、完整Step1序列、跨核链接；保留可能影响确定性执行的列表顺序 |

环境身份包含原始输入、容量、带宽、跨核延迟、官方源码哈希及适配器版本。
同执行、不同块边界可以共享精确数值，但仍是不同搜索状态。仅重命名sgid或来源且实际执行不变，不制造新搜索状态。
若标签变动实际改变了官方图或序列，则不能当作无语义重命名。
日志旧字段 `plan_hash` 仅用于展示；缓存和证据绑定使用新字段 `state_plan_digest` 对应的身份。

## 合法性、评分与双incumbent

`ExecutionValidator(g, preparation).validate(state, level=...)` 提供两个等级：

* `structural`：重建计划、Step1及当前状态，不承诺Step2/3能成功。
* `execution`：调用官方Step2、Step3准备过程和执行依赖检查，不运行完整多核时序模拟。

`LegalityRecord` 的状态是 `VALID`、`INVALID` 或 `UNKNOWN`。预算结束、超时、内存不足属于未知。
过期状态属于调用方错误，会抛出异常；不会掩盖为图非法。只有执行级 `VALID` 才能进入 `ScoredState` 和incumbent。
通用接口的 `check` 回调由调用方提供；`timeout_seconds` 可为执行检查启用独立子进程硬超时，结束后回收该子进程。
未提供硬超时时使用同进程检查。通用组件不自行预留最终复评预算。

`ScoreRecord` 绑定状态、计划、执行和环境身份，记录数值、评价/模型版本及校准依据：

| `source` | 含义 |
|---|---|
| `approximate` | 调用方模型给出的估计；额外搬运量可为 `None` |
| `official` | 当前计划正式评价得到的整数周期和已知额外搬运量 |
| `equivalent_official` | 相同执行身份的精确数值；保留原评价计划及证据路径 |

`evaluate_approximate(state, legality, evaluator)` 只接受外部回调返回的、绑定正确且标记为近似的 `ScoreRecord`。
本仓库没有默认预测公式，也没有把构造代理值当作Makespan。
测试中的估计器仅用于验证接口，不用于基座命令。

`reconcile(candidates)` 对调用方持有的 `ScoredState` 集合重新选择：

* `Incumbents.search` 是当前可比较评分下的最好状态；`Incumbents.exact` 是精确证据中最好状态。
* 同状态更新估计后重新排序；同计划或同执行已有精确成绩时不能降级为近似。
* 相同执行的精确证据可校准不同块状态，但不合并其块边界。
* 时间相等且搬运量不可比较时保留先到状态，不能将未知量当成零。
* 同执行的两个精确目标冲突会报错。集合由调用方保存，本接口没有隐式无限候选池或搜索主循环。

以下是使用已加载图 `g`、合法计划 `plan` 及调用方模型 `my_estimator` 的组件示例：

```python
from q2 import (PreparationService, build_state, derive_state, ExecutionValidator,
                evaluate_approximate, ScoredState, reconcile, write_search_snapshot)

preparation = PreparationService(g)
parent = build_state(g, plan, preparation=preparation)
state = derive_state(g, parent, edited_plan, preparation=preparation)
legality = ExecutionValidator(g, preparation).validate(state, timeout_seconds=30)
if legality.level == 'execution' and legality.status == 'VALID':
    score = evaluate_approximate(state, legality, my_estimator)
    selected = reconcile([ScoredState(state, score, legality)])
    write_search_snapshot('outputs/q2_search_snapshot', selected)
```

硬超时在Windows上使用spawn；脚本入口须放在 `if __name__ == '__main__':` 中。
`my_estimator` 应返回 `ScoreRecord`，并填写实际模型版本和校准依据；示例不提供模型实现。

## 缓存与基座接入

`PreparationService` 绑定只读输入图及其索引。修改输入DAG时重新建立 `Graph` 和服务，不原地修改 `g.raw`。
容量、带宽、延迟、官方哈希的配置变化会切换命名空间；官方源码在 `Settings.read` 装载时核验，运行中不得修改已装载源码。

默认 `PreparationCache` 对Step1、驻留分析、合法性及精确成绩摘要共用64MiB预算；各类默认条目上限分别为8、64、64、256。
字节数估算的是序列化负载和键，不是进程RSS；调用方持有的副本和完整图索引不计入此上限。
缓存使用进程内生成的序列化副本，取出后可独立修改；不读取磁盘pickle。
达到条目或字节上限按LRU淘汰。记录hit、miss、build、eviction、invalidation、oversize及官方评价计数。

相同计划可复用Step1及驻留分析，但每个候选仍检查当前状态及其保真声明。
驻留分析唯一实现为 `memory.profile`；`capacity.profile` 和旧接口只转换格式。
精确成绩缓存绑定环境、评价版本和当前落盘目录，复用目标及与标签无关的spill指标。
执行等价命中时 `result_path=None`，只提供原计划的 `evidence_path`，不会用带旧sgid的JSON充当当前计划结果。
需要完整结果时调用 `CandidateEvaluator.materialize_result` 重新物化当前计划。

默认基座仍以正式精确目标选择候选，β只接受正式或可靠等价的精确反馈。
“下降或持平继续、严格增时停止”及同值先到规则不变；`PlateauBeta` 的内部控制器格式仍为 `Q2-Beta-2.0`，其布局和算法没有变化。
CLI仍保留600秒预算、60秒初始复核预留、锁、原子发布及当前最终计划的独立官方CLI复评。
完整结果落盘；缓存仅保存有界摘要及准备记录。最终复评失败不会发布成功组合。

## 快照、导入与旧接口

`BaselineResult` 保存状态、评分、合法性以及可选的完整官方JSON、构造来源、执行记录和β轨迹。
完整官方JSON只能作为当前计划的 `official` 证据；精确等价数值和近似数值都不自动拥有当前计划的完整JSON。

`write_search_snapshot` / `load_search_snapshot` 是独立接口，保存候选来源、评分版本和产物SHA-256清单。
近似搜索结果标记 `SUCCESS_ESTIMATED`；已获精确分数但未经最终发布复评的快照标记 `SUCCESS_EXACT_UNREPLAYED`。
快照不生成 `complete.json`，不进入正式成绩汇总，也不能被基座 `--resume` 当作已验证组合。
加载时验证清单、版本、当前输入状态和证据绑定；快照是调用方保存的实验记录，不能替代正式发布复评。

`import_baseline_v2(directory, graph, graph_path)` 是2.0已验证结果的显式只读导入适配。
它核对旧产物哈希、旧指纹、原始输入文件、官方代码/配置、核数及发布方案/结果的一致性，再从实际计划重建新状态。
旧档案不被改写，旧扁平 `search_blocks` 不会被当作实际执行块。导入不等于跨版本断点续跑。
`pipeline.load_baseline_result` 只读取当前格式；未知版本和旧版resume明确拒绝。

旧控制器在 `q2.legacy.beta.LegacyBetaSearch`，旧峰值窗口在 `q2.legacy.window`。
原 `q2.beta.BetaSearch`、`q2.window` 公共名称保留带弃用警告的转发；1.1对照配置和云端源码归档保留。
`encoding.realized_blocks` 仅保留旧格式兼容参考，新流程使用逐核类型化状态。
Q1及公共调度框架语义不变；未注册Q2优化后端。
