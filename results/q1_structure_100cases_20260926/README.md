# 问题一结构驱动第三模型：100 case 正式结果

本目录归档已固定版本（Q1 合并提交 `7471470`，实际运行工作区 `93be2aa`）的 100 case、2/3/4/5 核共 400 组完整结果。400 份最终方案均已通过落盘后的独立官方重放。本次归档只重新核对数据和哈希，没有重跑优化或重新生成成绩。

| 目标核数 | 平均官方加速比 |
|---|---:|
| 2 | 2.090409× |
| 3 | 2.939747× |
| 4 | 3.686617× |
| 5 | 4.296547× |

加速比 = 同图固定单核官方 cycles / 当前最终方案多核官方 cycles。单核基准只用于报告，没有进入优化或重复计入算法时间。

## 时间口径与限制

本地 6 case 并发，仅用大核逻辑 CPU 0、1、10、11、12、13、22、23；每 case 内部四种核数串行，共享 600 秒目标、900 秒条件延长、1200 秒保护。不同 case 按输入文件大小降序进入队列。

每 case 四核数合计平均算法用时 513.748818 秒；整批墙钟 8678.505671 秒。搜索中参与决策的官方调用计入算法时间；冻结方案后的独立官方评分整批 466.893210 秒单独统计。15 个 case 超过 600 秒，0 个超过 900 秒，0 个保护超时，详见 official_aggregate.json。这些是本机并发实验测量，不能视为其他硬件上的耗时保证。

原求解器输出中 357 组官方成绩、43 组近似成绩；最终成绩统一使用独立官方重放的 official_summary.csv，不使用 solver_summary.csv 的混合分数替代。此次已取消的新版 8 并发试验不在本归档中。

## 文件

- official_summary.csv：逐 case、逐核数的真实 cycles、加速比、额外/划分/溢出搬运量、算法耗时、独立评分耗时、方案哈希。
- official_aggregate.json：全官方统计及超时名单。
- case_timings.csv、timings.csv：原始整 case 与逐核数耗时。
- solver_results.json、solver_summary.csv、solver_aggregate.json：原始搜索结果，保留官方/近似来源。
- final_results_N2.zip 至 final_results_N5.zip：每包 100 组，每组包含 plan、official_replay、verified、原 evaluation、summary、request、baseline、features 八份 JSON。官方重放文件保留完整时间线、内存峰值、搬运和 DDR 争用记录。
- artifact_hashes.json：上述 3200 份原文件 SHA-256；frozen_inputs.json：独立核验前冻结的 1604 份原文件哈希，已逐项重新检查。
- solver_source.zip、source_hashes.json：实际历史算法源码及依赖代码快照。整库指纹也包含 Q2，当前 main 的 Q2 更新会改变整库指纹，不能据此将历史结果改标成当前整库运行。
- input_hashes.json、benchmark.json、provenance.json：100 个输入的指纹、实验设置与来源。
- manifest.sha256：本归档所有发布文件的哈希（不含清单本身）。

解压四份 final_results 包到同一空目录即可得到 case_001/N2 至 case_100/N5。独立复评可以使用仓库官方 `evaluate_problem_1` 入口，输入原题图、原配置和 plan.json；不把近似评价当作正式成绩。数据包不包含官方原始输入，请使用本地官方附件并核对 input_hashes.json；单核基准仍为仓库已有的 results/problem_1_singlecore。

日志、临时检查点和搜索过程候选保留本地，不纳入 Git。所有最终方案、正式评价和完整 100 case 指标均已归档。

## 版本对应核验

`version_audit.json` 和 `original_launch.json` 保留版本证据：原启动命令明确指向固定版；运行清单、400 份结果、400 份请求及 400 份独立官方核验标记的源码签名均一致；请求参数均与历史统一配置一致，未复用其他运行结果。当前保留版本的 69 份 Q1、共享运行代码、官方代码及顶层配置与历史实际源码逐字节相同。源码压缩包也已重新核对每项 SHA-256。
