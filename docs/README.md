# 文档索引 · Documentation

[项目首页](../README.md) · [English overview](../README.en.md) · [题面与数据 / Problem & data](data.md)

首页介绍研究问题、模型关系与代表性结果；本页汇总各版本的算法、接口和实验记录。各文档中的发布说明与验证数量对应其记录的版本。

## 开始使用

| 目标 | 阅读入口 |
|---|---|
| 安装与解释器 | [Python 环境](environment.md) |
| 原版题面、100 张图与哈希校验 | [数据说明](data.md) |
| 模块职责、基线读取和运行约定 | [工程结构](architecture.md) |
| 团队身份、分支与 PR 流程 | [协作约定](../AGENTS.md) |
| 官方评估语义 | [原版官方 README](../official/README.md) |

## Q1：多 Task 划分与调度

| 模型／实验 | 方法与接口 | 结果与验证 |
|---|---|---|
| 官方单核参照 | `src/problem_1_baseline.py`；[官方流程](../official/README.md) | [100 例固定基线](../results/problem_1_singlecore/README.md) |
| V1 TaskGrowth | [初始化公式](q1/initialization.md)；当前入口 `tools/reference_problem1.py` | [400 组完整归档](../results/q1_adaptive_100cases_20260925/README.md) |
| V2 分层改进 | [方法](q1/stage2.md)、[运行与预算](q1/stage2_server.md) | [验收](q1/stage2_acceptance.md)、[400 组历史择优指标](../results/q1_stage2_100cases_20260926/README.md) |
| V3 结构感知联合搜索 | [算法与预算](q1/unified.md)、[接口](q1/interfaces.md)、[近似评价分析](q1/approximation_audit.md) | [400 组独立官方结果](../results/q1_structure_100cases_20260926/README.md) |
| 五组件消融 | 原基座 full / no_structure / no_communication / no_cache / no_adaptive_beta / no_smart_core | [2400 行正式明细](../results/q1_ablation_2400_20260926/README.md) |
| β 灵敏度 | [运行工具与抽样设计](q1/sensitivity.md) | [已完成 20 图 × 5 策略，仅 5 核](../results/q1_sensitivity_5cores_20260927/README.md) |

V2 和 V3 分别从 V1 发展。V2 归档包含全部 400 组指标及逐例择优记录；V3 的正式评分来自独立官方复评，原搜索汇总另含近似评价值。

## Q2：联动构造与片上容量

| 版本 | 方法与接口 | 结果与验证 |
|---|---|---|
| 1.1 历史版 | [已知问题和冻结源码](../archive/q2/baseline_1_1_known_issues/README.md) | 旧版对照 |
| Repair 2.0 | [修复设计](q2/repair.md)、[自适应 β](q2/adaptive_beta.md) | [400 组全量归档](../results/q2_repair_full_20260926/README.md) |
| Repair 2.1 | [状态与组件接口](q2/interfaces.md)；保留 2.0 基座算法 | [接口验证](q2/interfaces_validation.md) |
| Optimized v3 | [数学模型](q2/OPTIMIZED_MODEL.md)、[接口](q2/OPTIMIZED_INTERFACES.md)、[版本与标签](q2/VERSIONS.md) | [400 组独立官方结果](../results/q2_optimized_full_20260927/README.md)、[发布时有限验收](q2/OPTIMIZED_ACCEPTANCE.md) |

2.0 的预算为每图每核数 600 秒，v3 为每图跨核数共享预算。2.1 的验证侧重复用接口，基座全量结果沿用 2.0。

## Q3：L2 复用与时间／搬运折衷

| 模型 | 方法与运行 | 结果与验证 |
|---|---|---|
| 已发布 Q3 | [运行说明](../README_Q3.md)、[实现](q3/implementation.md)、[官方参考模型](q3/model_reference.md) | [500 组全量归档](../results/q3_full_20260927/README.md)、[完整验收](q3/validation.md) |
| Q3v3 | [独立入口](../README_Q3V3.md)、[容量重排与搬运目标](q3v3_memory_traffic.md)、[组件来源](q3v3_memory_traffic_sources.md) | [20 组、40 份最终方案验证](q3v3_validation.md) |

Q3 的硬件、算法和总加速比都以同核数冻结方案为参照。Q3v3 的 `cycles-traffic` 用于权衡周期与搬运量，同时保存纯最快方案；已完成的 20 组实验覆盖 2–5 核，完整方案和日志保留在本地。

## 复现方式

1. 用[输入清单](../official/inputs.sha256)确认题面、100 例原图和配置未变。
2. 阅读目标归档的源码版本、预算、完成状态及发布文件范围。
3. **复评已发布方案**：按归档命令解压所需方案并调用对应官方入口，输出写入新的 `outputs/` 子目录。
4. **重新搜索**：选择归档指定的源码提交／标签和参数。新搜索结果写入独立目录；限时搜索的轨迹会受到机器负载影响。

历史归档中提到的原图可从当前仓库的 `official/data/` 读取；各归档列出了所含方案、结果和运行记录。
