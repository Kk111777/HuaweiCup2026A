# 问题一初始化有限验收

以下先记录迁移前本地实现的有限验收，随后记录协作仓库接入检查。历史大体积运行产物仍留在原工作区，未随本次程序上传；指标摘录用于复现对照，不构成已归档的正式多核实验。

日期：2026-09-24。运行环境：Windows、Python 3.13.5。配置为原附件 `config.txt`，原官方 10 个 Python 文件实施前后 SHA-256 全部一致。算法使用 β=1,2,3,4、K=8。本记录是有限实现验收，不是全部 100 例的实验结论。

## 完成项目

- `python -m unittest discover -s tests -v`：**20 项测试全部通过**，其中包含 24 个固定种子的随机小 DAG 的公式差分与官方执行检查。
- 集成测试在 Windows 下用 `--jobs 2` 执行两个小图，验证多进程入口；在另一工作目录直接运行官方命令行，完整结果相等。
- 真实 spill 小图检查得到额外 spill 搬运 120 bytes，预分析成功时没有触发拆分。
- case_001 各核数各评价 8 个候选；case_086 为单计算分量，评价 4 个候选。合计 **36 个正常候选均通过**，无修补、失败或后备方案。每组都包含 β=3。
- 5 个最终多核方案写入后重新读取，官方完整复评结果全部一致。
- 再独立调用官方命令行复评 **5 个多核结果及 2 个单核基准**，与保存结果的完整 JSON 内容一致。
- 对两批真实结果执行 `--resume`，7 个完成项全部通过指纹及产物哈希检查并跳过计算；测试另外验证旧 β 集合与被修改产物不能复用。

原工作区检查记录：`outputs/problem1/official_integrity.json` 和 `outputs/problem1/independent_verification.json`。

## 实际指标

单核基准使用原附件 `singlecore_evaluate.py`。时间单位为 cycles，搬运单位为 bytes。表中总额外搬运为 `added_copy_bytes`，不是 `cross_task_traffic`。

| 用例 | 核数 | 选中模式 | β | Task 数 | Makespan | 调度后总搬运 | 总额外搬运 | 加速比 |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| case_001 | 1 | 官方单核 | — | 1 | 233,110 | 1,639,552 | 0 | 1.0000 |
| case_001 | 2 | global | 1 | 2 | 116,868 | 1,640,704 | 1,152 | 1.9946 |
| case_001 | 3 | components | 1 | 5 | 79,182 | 1,644,160 | 4,608 | 2.9440 |
| case_001 | 4 | global | 1 | 4 | 58,984 | 1,643,008 | 3,456 | 3.9521 |
| case_001 | 5 | global | 1 | 5 | 47,502 | 1,644,160 | 4,608 | 4.9074 |
| case_086 | 1 | 官方单核 | — | 1 | 118,403 | 338,022 | 0 | 1.0000 |
| case_086 | 2 | global | 2 | 12 | 113,951 | 1,295,564 | 957,542 | 1.0391 |

上述选中结果的 `spill_added_copy_bytes` 均为 0，总额外搬运全部来自切图。并不意味着其他用例没有缓存换入换出；对应测试已单独覆盖。

完整汇总包含两级缓存峰值、求解墙钟耗时及未四舍五入的加速比：

- 原工作区 `outputs/problem1/smoke_001/summary.json` 和 `summary.csv`。
- 原工作区 `outputs/problem1/smoke_086/summary.json` 和 `summary.csv`。

`summary.json` 中的 `complete=true` 只表示该命令请求的验收范围已经完成，不表示全部 100 例已经完成。没有汇总两个不同核数覆盖范围为全体平均成绩。

## 在协作仓库复跑

以下使用迁移后的入口，默认复用已校验单核归档；新输出另设目录。

```bash
python src/solve_problem1.py solve official/data/case_001.json --cores 2 3 4 5 --output-root outputs/q1/smoke_001 --resume
python src/solve_problem1.py solve official/data/case_086.json --cores 2 --output-root outputs/q1/smoke_086 --resume

python official/code/multicore_cut_evaluate_problem_1.py official/data/case_001.json outputs/q1/smoke_001/case_001/N2/plan.json --config official/data/config.txt -o outputs/q1/manual_check/result.json --trace-output outputs/q1/manual_check/trace.json --log-output outputs/q1/manual_check/result.log
```

完整接口、公式对应、产物结构与迁移说明见 [初始化说明](initialization.md)。代码修改后续跑会因指纹不同而重算。

## 尚未执行的范围

没有运行 case_067、case_086 的 3–5 核或全部 100 例，没有进行第二阶段改进，也没有迁移到服务器或在 Linux 上实测。当前有限结果不能推断所有用例的性能、可行性修补频率或全量运行耗时。

## 2026-09-25：候选并行与等价计算优化

为计算 case_087 的 5 核结果，新增 `--candidate-jobs`，用进程独立计算同一例的完整候选。并行完成顺序不改变候选记录顺序或择优规则。原官方程序保持原样。

另缓存已有计算跨度的最大值，并直接从输入张量计算基础搬运复用；确定为零的复用项由第一份短名单覆盖。这两项仅减少重复计算，不改变评分或候选集合。

标准库测试扩展为 **23 项，全部通过**，包括串行/候选并行完整结果一致、Windows 两层进程池命令行、跨度最大值下降及短名单过滤与原四份名单对照。仍包含 24 个随机小 DAG。跨度缓存加入后，另核对 case_001 的 5 核方案与之前保存的方案完全相同，Makespan 仍为 47,502。

case_087 使用两个候选工作进程，单核基准不属于此次追加请求；最终结果保存在 `outputs/problem1/case_087_5cores/`。此前两批验收记录保留其原始代码指纹，新版本运行时不会把旧指纹直接当作匹配的续跑结果。

追加运行已完成：8 个候选均通过官方评估，选中 `components, β=1`，共 102 个 Task。最终提交文件落盘重读后，主进程的官方完整复评与候选工作进程结果完全一致；完成标记与文件哈希检查通过。

| 指标 | case_087，5 核 |
|---|---:|
| Makespan / cycles | 881,011 |
| 原图搬运 / bytes | 5,919,098 |
| 调度后总搬运 / bytes | 28,269,260 |
| 总额外搬运 / bytes | 22,350,162 |
| 切图新增搬运 / bytes | 17,004,882 |
| spill 新增搬运 / bytes | 5,345,280 |
| L1 峰值 / bytes | 520,704 |
| UB 峰值 / bytes | 74,242 |

最终一次完整运行（两个候选进程、含落盘复评）耗时 636.71 秒；不含此前停止的单核基准及性能诊断运行，不能据此宣称相对串行的实测加速比。未计算此次单核基准或 NPU 加速比。

原工作区保留 `outputs/problem1/case_087_5cores/` 中的最终方案、官方完整结果、汇总、8 个候选指标及验收记录。

## 2026-09-25：协作仓库接入验收

将本地 `problem1/` 接入预留的 `src/q1/`，入口为 `src/solve_problem1.py`，默认官方路径为 `official/`，新实验写入 `outputs/q1/initialization/`。评分、四种 β、候选顺序及择优规则保留原实现。

默认使用已有 `common.baseline` 统一校验并读取单核归档，结果保存仓库相对 `baseline_ref`；源文件指纹还覆盖公共基线接口和入口。自定义小图可显式使用 `--recompute-baseline`。原官方程序、单核入口、公共接口和已发布单核归档未改动。

本次在 Windows / Python 3.13.5 上验证：

- `python -X utf8 -m unittest discover -s tests -v`：**67 项全部通过，无跳过**。其中 27 项为本次初始化模块测试，覆盖原算法测试和新增的归档默认值、拒绝不匹配输入、基线只读复用、双层进程池及任意工作目录入口。
- 公共接口实际校验全部 100 例单核归档，再读取 case_001 的基准；没有重新运行单核调度或复制其产物。
- 以两个候选工作进程重新计算 case_001 的 2–5 核，共 32 个候选；四份最终方案及完整官方结果与迁移前逐项一致，Makespan 分别为 116868、79182、58984、47502 cycles。
- 每份最终方案落盘后完整复评一致；另使用官方独立命令行复评 5 核结果，完整 JSON 一致。
- 对这四组结果运行 `--resume`，均通过基线、代码指纹及产物哈希验证并跳过重算。

本次新增产物位于协作工作区的 `outputs/q1/upload_acceptance/` 与 `outputs/q1/upload_independent/`，由 Git 忽略。没有重跑 case_087 或全量多核实验；没有将历史大体积 Trace、原图和过程候选作为本次程序提交内容。
