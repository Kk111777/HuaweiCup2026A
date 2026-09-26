# 第二问新基座 Q2-Repair-2.0：100 例全量结果

本目录归档第二问新基座在官方 100 个计算图、2/3/4/5 核上的一次完整运行。**400/400 组均输出方案并通过文件重读后的独立官方复评**；其中 384 组完成配置要求的全部搜索，另外 16 组因 600 秒墙钟预算的预留阈值而提前结束搜索，不能称为搜索穷尽或最优。原始算法源码为提交 [`11501c5`](https://github.com/Kk111777/HuaweiCup2026A/commit/11501c5137d949514724baed3f6d0d3bd9e38896)。本归档没有改动求解器，也没有把后续串行复查混进该批次成绩。

## 结果

| 模拟核数 | 成功组数 | 完整搜索 | 百例平均加速比 | 快于单核 |
| ---: | ---: | ---: | ---: | ---: |
| 2 | 100 | 96 | 2.038162 | 100 |
| 3 | 100 | 96 | 2.775597 | 100 |
| 4 | 100 | 96 | 3.452734 | 100 |
| 5 | 100 | 96 | 3.991946 | 100 |

每例加速比 = 同一输入的[已归档单核基线](../problem_1_singlecore/README.md)周期数 ÷ 第二问官方复评的 `makespan`。表中对各核数的 100 个比值取算术平均，单位为倍；电脑上的求解秒数不参与加速比。逐组周期、搬运量、所选方案、搜索完整度、耗时及哈希见 [`details.csv`](details.csv)，精确汇总见 [`summary.json`](summary.json)。

预算截断全部集中于 `case_014`、`case_072`、`case_076`、`case_091` 的 2–5 核，共 16 组。它们的**方案及成绩已独立复评**，但可能因搜索提前结束而低于在更少并发或更长预算下的成绩。一次单 worker 对照在 `case_014/N2` 多评价了 1 个候选，成绩仍为 8,828,879 cycles；串行复查单独记录，结果不属于本次 400 组归档。这里的“400/400 成功”仅指方案被官方确认，不代表 400/400 搜索完整。

## 文件与证据范围

- [`plans.zip`](plans.zip)：400 份原样的官方格式 `plan.json`，归档成员为 `N2/case_001_plan.json` 等；不包含原始计算图。
- [`details.csv`](details.csv)：每组的正式目标、加速比、搜索状态与对应方案及官方结果的 SHA-256。
- [`artifact_hashes.csv`](artifact_hashes.csv)：本机原始输出中 4,400 份正式产物的文件大小和 SHA-256，包括方案、官方结果、Trace、运行记录及状态文件。此清单是本地核验证据，**未上传的 Trace 和中间评价文件不能从本仓库下载**。
- [`provenance.json`](provenance.json) 和 [`input_sha256.txt`](input_sha256.txt)：算法参数、源码与官方程序指纹、100 份输入指纹、Python 版本及单核基线归档指纹；不发布本机绝对路径。
- [`archive_validation.json`](archive_validation.json)：本次打包时对 400 组状态、4,400 份正式文件、100 份输入及压缩包成员的核验计数。
- [`manifest.sha256`](manifest.sha256)：本目录已发布文件的 SHA-256，不包含清单自身。

本地完整实验约 40 GB，含候选评价、完整官方结果与 Trace；将其全部放入 Git 不适合仓库维护。因此本目录发布可复评的 400 份方案、完整逐组指标、来源和原始文件指纹。打包时逐份重新哈希原始正式产物，并重新解压核验 400 份方案；压缩包并非重新求解的产物。

## 运行与复评

运行环境为 macOS arm64、Python 3.12.14，`workers=4`；每组墙钟预算 600 秒，初始保留 60 秒用于最终复评。`workers` 是电脑上的并行进程数，2–5 核是**模拟的 NPU 核数**。算法默认自适应 β。随机种子不适用；预算截断点可能随机器负载变化，因此重跑的搜索完成度和周期数可能不同。

当前 `main` 的默认求解器已升级为 Q2 2.1。要**重新运行本归档的 Q2 2.0 算法**，须使用指向源码提交 `11501c5` 的 `q2-baseline-2.0` 标签。先从赛题附件准备 `official/data/case_001.json` 至 `case_100.json`，用 [`input_sha256.txt`](input_sha256.txt) 核对，再从当前仓库根目录建立独立检出并运行：

```bash
Q2_DATA_DIR="$(pwd)/official/data"
git worktree add ../HuaweiCup2026A-q2-2.0 q2-baseline-2.0
cd ../HuaweiCup2026A-q2-2.0
python src/solve_problem2.py batch --data-dir "$Q2_DATA_DIR" --cores 2 3 4 5 \
  --workers 4 --algorithm-config configs/q2/baseline.json \
  --output-dir outputs/q2_reproduction
```

若只需**复评已发布方案**，可留在含本归档的当前仓库根目录，先解压压缩包，再使用官方适配入口；复评后的 `makespan` 可与 `details.csv` 对照：

```bash
python -m zipfile -e results/q2_repair_full_20260926/plans.zip outputs/q2_published_plans
python src/solve_problem2.py verify official/data/case_001.json \
  outputs/q2_published_plans/N5/case_001_plan.json \
  --output-dir outputs/q2_plan_replay_case001_N5
```

复评写入新的 `outputs/` 目录，不修改本归档。单核基线、官方固定配置与代码须同源，程序会先检查已审版本的 SHA-256。
