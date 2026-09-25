# 协作者题目一：完整 100 case 运行归档

本归档保存 `Zysishuiyears` 算法提交 `04634a2400ce9fa0c45084bed06fb4ce72de917d` 的正式实验。**100/100 case 成功，2/3/4/5 核共 400 份方案均通过写入后重读、官方完整重放和结果一致性检查**。未修改求解器决策；未将单核保底或其他算法的成绩替换进多核结果。

## 下载与查看

- [完整提交方案压缩包](problem1_submission_100cases.zip)：400 份官方格式 JSON，含 `node_to_subgraph` 和 `core_schedules`，另附排布、加速比和耗时表。
- [按核数浏览方案](submission/)；[子图调度排布表](submission/subgraph_schedules.csv)。排布表中的开始/结束时间单位为 cycles，核心、子图和队列位置编号从 0 开始。
- [逐例加速比](submission/speedups.csv)、[平均加速比](submission/average_speedups.csv)、[100 例完整运行时间](case_timings.csv)。
- [实验报告](report.md)、[运行来源与硬件](run_info.json)、[全量验证记录](audit.json)、[提交包验证记录](submission_audit.json)。
- [100 个 case 的完整原始产物](cases/)：每例一个 ZIP，解压后是 `case_XXX/`，内含四个核数的 plan、result、trace、construction、metrics、events、日志、完成标记及该例的 timing 和 rows。
- [原始文件逐份 SHA-256 与存储位置](original_artifacts.json)、[归档核验](archive_validation.json)、[本目录文件 SHA-256](manifest.sha256)。

## 实验结果

| 模拟核数 | 完成例数 | 百例算术平均加速比 | 快于单核 | 慢于单核 |
|---|---:|---:|---:|---:|
| 2 | 100 | 1.832685 | 94 | 6 |
| 3 | 100 | 2.463278 | 93 | 7 |
| 4 | 100 | 2.933060 | 94 | 6 |
| 5 | 100 | 3.338753 | 93 | 7 |

每例加速比 = 同一计算图的已校验单核基线 `makespan` / 当前多核方案的官方 `makespan`；两个数都是 cycles。分别按核数对全部 100 个比值取算术平均，慢于单核的结果按原值保留。Python 实际运行秒数不参与加速比。

单核基线来自仓库已有的 [`results/problem_1_singlecore`](../problem_1_singlecore/README.md)，通过输入、配置、官方代码与归档 SHA-256 校验后复用，不在本实验中重新计算。

## 运行与计时

设备为 Intel Core Ultra 9 275HX，24 个逻辑 CPU，物理内存 63.435 GiB。`workers=20`，每 case `candidate_jobs=1`，候选策略 `adaptive`，每例依次运行 2、3、4、5 核。算法不使用随机过程。

整批于北京时间 2026-09-25 02:36 开始，05:26 结束，共 **10239.945 秒（2 小时 50 分 40 秒）**，包含前置校验和最终验收。逐例墙钟中位数 63.602 秒，最慢 `case_076` 为 10230.567 秒，形成整批长尾。

`case_timings.csv` 完整记录 100 行：

- `wall_seconds`：worker 开始至求解器产物写入完成，不含排队等待与计时记录自身写入。
- `cpu_seconds`：该 worker 用户态加内核态 CPU 时间；本次每例没有嵌套候选子进程。
- `N2_seconds` 至 `N5_seconds`：原求解器的逐核阶段计时，不含共享图初始化及完成标记哈希。
- `started_at_utc`、`finished_at_utc`：UTC 时间。

并发负载影响墙钟时间，不能将此表解释为独占机器的串行基准。各 case 墙钟存在重叠，不可相加当成整批耗时。

## 完整性与保存方式

为减小 Git 体积，case 内原始文件按例无损 ZIP 压缩；所有内容逐份对原文件验证 SHA-256。直接保存的表格、方案、脚本、提交包及早期阶段快照也逐份核验。`.gitattributes` 禁用本归档的换行转换，保留指纹有效性。仅运行锁、Python 缓存和临时文件不上传，完整排除清单见 `archive_validation.json`。

`snapshot_20260925_041353*` 为运行期间交付过的 **99 例阶段快照**，仅保留历史记录；正式成绩使用本目录顶层汇总与 `submission/` 的 100 例结果。日志和来源记录中的本机绝对路径、PID 作为历史证据原样保留，不是下载者需要配置的路径。原始计算图不重复上传，按仓库说明获取官方附件并核对 `run_info.json` 的输入指纹。

`run_batch.py`、`package_results.py`、`export_snapshot.py` 是本次实际使用的薄包装脚本，按原样归档；其路径布局针对本次隔离工作区，不应直接在只读归档目录重新执行。

## 复现与复评

标准入口可在包含对应官方数据的仓库根目录执行，输出使用新的目录：

```powershell
python -X utf8 src/solve_problem1.py batch --data-dir official/data --cases all --cores 2 3 4 5 --jobs 20 --candidate-jobs 1 --beta-search adaptive --output-root outputs/q1_reproduction
```

该命令复现求解策略；本次附加的总墙钟/CPU 计时来自归档包装脚本。核对代码版本后再比较结果。

单份已提交方案可直接交给官方评估器：

```powershell
python -X utf8 official/code/multicore_cut_evaluate_problem_1.py official/data/case_001.json results/q1_adaptive_100cases_20260925/submission/N5/case_001_plan.json --config official/data/config.txt
```

复评应使用新的输出位置或官方默认临时输出，不要覆盖此归档。上传整理没有重跑完整算法，保留已有成功实验的原始字节和来源记录。
