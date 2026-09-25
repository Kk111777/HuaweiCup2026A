# HuaweiCup2026A

华为杯 Python 程序协作项目。项目代码通过私有 GitHub 仓库维护，使用任务分支和 Pull Request（PR）共同开发。

- 仓库：<https://github.com/Kk111777/HuaweiCup2026A>
- 仓库所有者：`Kk111777`
- 协作者：`Zysishuiyears`
- 默认分支：`main`
- 基础环境：Python 3.12；单核基线及官方评估程序仅使用标准库。

## 项目目录

```text
official/                       官方程序、配置及说明；原始图留本地
src/problem_1_baseline.py        稳定的单核基线运行入口
src/common/baseline.py          归档校验、基线读取和加速比计算
src/solve_problem1.py           问题一统一串行求解入口
src/q1/                        问题一切图、Task 排布与官方评估适配
src/q2/、q3/                    第二、三问算法（实际加入时创建）
configs/unified.json            问题一统一搜索参数
results/problem_1_singlecore/   完整的 100 例单核基线，固定保留
results/qN/<日期_算法版本>/      正式实验的精简可复核归档
outputs/qN/<实验编号>/          本地实验、Trace 和中间候选（Git 忽略）
docs/                          结构约定、环境说明及各问算法文档
tests/                         基线、公共接口及各问测试
notebooks/                     Jupyter Notebook 分析与实验
```

当前提供问题一整图单 Task、单核基线及全部 100 例正式单核结果，串行基础初始化对照入口，以及共享图模型、调度内核和评估服务的统一构造改进入口。新版本的多核验收成绩以单独的验收报告为准。程序使用相对路径或配置指定输入输出位置。

后续开发先阅读[项目结构与基线复用指南](docs/architecture.md)。其中包含可直接运行的基线读取示例、加速比口径、正式结果归档规则，以及各问程序逐步接入的方式。

## 开始使用

先接受私有仓库邀请，再克隆项目：

```powershell
git clone https://github.com/Kk111777/HuaweiCup2026A.git
cd HuaweiCup2026A
```

已有本地项目的成员在现有目录继续工作，不需要重复克隆。环境准备见 [Python 环境说明](docs/environment.md)。

## 问题一：单 Task、单核基线

### 准备官方材料

将赛题 PDF 和附件压缩包保留在项目根目录。首次准备时，用 Python 3.12 将附件完整解压到 `official/`：

```powershell
python -m zipfile -e "通用神经网络处理器下的多核调度问题  附件.zip" official
```

`official/` 中的代码、说明和 `data/config.txt` 保持原样。已有官方材料时不要用不同版本覆盖；正式输入应为 `data/case_001.json` 至 `data/case_100.json`。约 253 MB 的原始计算图、压缩包和 PDF 不纳入 Git，其他成员需自行从同一附件准备数据。

### 计算口径

官方 `singlecore_evaluate.py` 将全部非 `COPY_IN`/`COPY_OUT` 操作映射到子图 0，采用 `core_schedules=[[0]]`。边界 COPY、缓存换入换出、四条 Pipe 的调度与共享 DDR 带宽模拟全部由官方代码完成；不使用随机切图示例生成基线。

最终耗时取结果 JSON 顶层的 **`makespan`，单位为 cycles**。`step3_by_task` 中的 `local_makespan` 只用于核内排布，不能替代最终指标。一个核心仍可以并行执行不同 Pipe；这里的单核指模拟的 NPU 核心数量。Python 实际运行秒数另记为 `runtime_seconds`，不用于加速比。

固定配置为 L1=524288 bytes、UB=131072 bytes、DDR=60 bytes/cycle，同核 Task 等待 100 cycles、跨核前驱等待 1000 cycles。本基线仅有一个从 0 时刻启动的 Task，不发生 Task 间等待；缓存不足时仍允许官方程序插入 Spill。入口通过 SHA-256 检查附件配置，不能通过参数改动评测数值。

### 运行

以下命令中的 `python` 应指向 Python 3.12；Windows 可替换为 `.\.venv\Scripts\python.exe`，无需激活环境或安装第三方库。

```powershell
# 全部 100 个正式用例，按 CPU 数自动选择并发数（最多 8 个）
python src/problem_1_baseline.py

# 指定同时运行 8 个官方评估进程
python src/problem_1_baseline.py --workers 8 --output-dir outputs/singlecore_parallel

# 中断后在原输出目录续跑，已验证的成功用例不重新计算
python src/problem_1_baseline.py --workers 8 --resume

# 单例；含空格的路径请加引号
python src/problem_1_baseline.py official/data/case_001.json --output-dir outputs/case_001_check

# 自选目录，只读取其中的 case_*.json
python src/problem_1_baseline.py path/to/cases --output-dir outputs/subset_check

# 单元与集成测试（无需跑完正式用例）
python -m unittest discover -s tests -v
```

默认全量输入必须完整覆盖 001–100。每例完成后立即保存并更新汇总；单例失败会记录原因并继续，其余输入的原始内容不会被修改。全部成功时退出码为 0，存在失败或准备条件不满足时为 1，手动中断时为 130。大图可能运行较久，不设置隐式超时；打印 `[START]` 而尚未打印结果的用例仍在运行。

默认并发数取逻辑 CPU 数的一半，至少 1 个、最多 8 个；可用 `--workers 1` 串行运行。线程池仅负责管理独立 Python 子进程，实际模拟在多个 CPU 核上执行。大文件优先开始，汇总始终按用例编号排序。增加并发缩短的是整批耗时；每个评估进程仍然模拟一个 NPU 核心和一个 Task。程序处理的是图依赖和事件时间，不实际执行神经网络的矩阵计算，也没有 GPU 计算接口。

新实验的输出目录必须为空。重复实验请指定新的 `--output-dir`；继续同一实验时显式传 `--resume`。续跑会核对全部输入、配置及官方代码的 SHA-256，并重新读取已成功用例的结果、Trace 和日志，确认与汇总一致后才复用；来源变化或成功产物损坏时拒绝续跑。失败或未完成用例会重新评估，更新其原有产物。不要手工修改汇总或计算图来混合不同实验。

按 Ctrl+C 后取消尚未开始的用例，等待正在运行的进程结束并保存，再用 `--resume` 继续。强制关闭窗口或进程后也可续跑；未记入成功汇总的用例会重算。输出目录的 `.run.lock` 是操作系统文件锁，防止两个批处理同时写入；进程退出即释放锁，文件本身保留，不要在运行时删除。官方调度是确定性的，本入口不使用随机过程，不需要随机种子。

### 输出与验收

默认输出目录结构为：

```text
outputs/problem_1_singlecore/
  summary.csv
  run_info.json
  case_001/
    case_001_singlecore_res.json
    case_001_singlecore_trace.json
    case_001_singlecore_log.txt
  ...
  case_100/
    ...
```

三类逐例文件保持官方格式。Trace 可在 Perfetto 中打开。`summary.csv` 使用带 BOM 的 UTF-8，便于直接用 Excel 查看，各字段含义如下：

| 字段 | 含义 |
| --- | --- |
| `case`、`status` | 用例名称及 `ok` / `failed` 状态 |
| `makespan_cycles` | 最终模拟耗时，取官方 `makespan` |
| `original_graph_copy_bytes`、`scheduled_copy_bytes` | 原始图及调度后的 COPY 搬运量 |
| `added_copy_bytes` | 调度后搬运量减去原始搬运量 |
| `partition_added_copy_bytes`、`spill_added_copy_bytes` | 边界重建及缓存换入换出各自产生的额外搬运量 |
| `l1_peak_bytes`、`ub_peak_bytes` | 官方报告的核内缓存峰值 |
| `runtime_seconds`、`error` | 调用与检查程序的实际秒数，以及失败原因 |

失败行的评测数值留空，不能参与成功结果统计。`run_info.json` 保存解释器、平台、参数、官方代码及配置和每份输入的 SHA-256、运行起止时间、完成数及状态；`invocations` 记录每次运行的入口版本、并发数、复用数量和实际批处理秒数，总 `runtime_seconds` 表示从首次开始到最后结束的历时（含续跑间停顿）。`status=complete` 才表示本次运行全部成功。中断时已完成结果保留，完成数不足的记录不构成全量实验。

入口检查单核、单 Task、起始时刻、依赖与流量、操作时间线末端、内存峰值，以及结果、Trace、日志的一致性。全量验收要求汇总恰好 100 行且全部 `ok`，同时拥有 100 组完整的三类官方产物。单核加速比定义为 1；后续多核加速比逐例使用本表的 `makespan_cycles` 除以对应多核耗时，再对全部用例的加速比取平均。

已核对的参考值：附录 B.7 最小图为 **6 cycles**；`case_001` 为 **233110 cycles**；`case_003` 为 **602212 cycles**。完整结果见[已归档的 100 例汇总](results/problem_1_singlecore/summary.csv)。

2026-09-23 在 Python 3.12.14 下完成全量复现：**100/100 成功，300 个官方产物齐全**，其中 40 个用例产生 Spill。前 57 个成功用例原样复用，后 43 个用例以 8 进程并行完成，该并行阶段耗时约 467 秒。17 项测试通过；全量结果另已核对操作覆盖、原始计算依赖时序、同 Pipe 不重叠，以及 Trace、日志、汇总和源码/数据校验值的一致性。

### 已上传的完整结果

[单核 baseline 结果归档](results/problem_1_singlecore/README.md)保存 100 例的 300 份官方产物、汇总和运行记录，附本次上传的 SHA-256 清单和核验记录。日常新实验仍写入被 Git 忽略的 `outputs/`，完成并明确要求上传后才整理为正式结果。

## 问题一：统一构造与改进

本工作区将协作者的 Task 生长策略与个人的结构候选、局部改进及区域重构放入同一个搜索控制器，共用图模型、方案校验、调度内核、官方评估服务和预算。没有两个独立求解管线及最终择优汇合点。

当前统一入口允许近似改进立即覆盖方案；官方评估在时间充足时按批次或计时间隔校准。来不及核验时保留近似结果，`score_source` 标明来源；离线重放另存真实评分，不改写原始预测值。架构与接口规则见 [统一说明](docs/q1/unified.md)。

```powershell
python src/solve_problem1.py path/to/new_graph.json --config official/data/config.txt --cores 4 --output outputs/new_graph_N4
```

输入可为任意名称的合法新图。默认现场计算官方单核基线，并计入每个目标核数的 600 秒目标预算；保留有条件延至 900 秒、已启动工作最多至 1200 秒的项目保护机制。单个求解串行运行。本地批量测试才允许不同 case 并发，以及显式、校验过的单核归档复用。

详见[彩色分层架构、模块归属、调用与时间口径](docs/q1/unified.md)。新成绩与逐例耗时写入新实验目录；旧版两管线择优的成绩不代表本版本。

原 `initialize` 保留为串行基座对照 API。旧命令行移到 `tools/reference_problem1.py` / `q1.reference_cli`，其中候选并发被禁止。历史公式说明见 [初始化算法](docs/q1/initialization.md)，其中旧入口和并发说明以本页与统一架构文档为准。

## 日常协作

1. 开始新任务前，确认工作区干净，更新本地 `main`。
2. 为每项任务创建 `feat/<用户名>/<任务>` 或 `fix/<用户名>/<任务>` 分支。
3. 修改和验证程序；需要上传时，检查改动后提交并推送自己的分支。
4. 创建面向 `main` 的 PR，说明改动和验证方法，约定由另一位成员审核后合并。
5. 使用 **Squash and merge** 合并；随后更新本地 `main`，下一项任务重新建分支。

双方均可提交、审核和合并 PR。免费个人私有仓库采用团队约定，不将审核规则描述为平台强制保护。

详细协作指南和 PR 模板由首个协作配置 PR 加入，合并前可在该 PR 分支阅读。

## 在 Codex 中操作

日常修改默认只保留在本地。

- “提交”：检查后创建本地 Git 提交，不自动上传。
- “同步上传”或“上传”：检查改动及远端状态，完成适用验证，提交并推送当前任务分支，创建或更新 PR。
- “合并 PR”：作为单独操作，需要明确指示。

本项目没有后台自动提交、定时上传或自动合并任务。首次建仓与首个协作 PR 按本次初始化授权完成。

## 文件管理

代码、Notebook、项目说明和 `results/` 下明确归档的正式结果纳入版本管理。虚拟环境、缓存、本机凭据、临时文件和 `outputs/` 下的运行产物不提交。需要作为正式成果保留的图表和文档可放入 `docs/`。比赛数据加入前检查是否适合共享及文件大小；不将完整原始数据直接塞进提交历史。
