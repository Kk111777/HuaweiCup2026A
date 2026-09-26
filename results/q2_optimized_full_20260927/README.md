# 第二问 V3：100 图全量官方结果

本目录归档 `Q2-Optimized-3.0` 在官方 100 张图、2/3/4/5 核上的完整本地实验。
**400/400 份冻结方案均通过独立官方复评**，复评周期及新增搬运量与搜索内精确记录逐组一致。
运行源码为提交 [`29747d8`](https://github.com/Kk111777/HuaweiCup2026A/commit/29747d80dac80a45a7902af35e2aa58b526ebbf6)；归档不修改求解器。

| 模拟核数 | V3 平均加速比 | 历史 V2.0 | Q1v3 |
| ---: | ---: | ---: | ---: |
| 2 | 2.083251 | 2.038162 | 2.090409 |
| 3 | 2.880824 | 2.775597 | 2.939747 |
| 4 | 3.594802 | 3.452734 | 3.686617 |
| 5 | 4.184883 | 3.991946 | 4.296547 |

加速比为同图固定单核官方周期数除以本组独立复评周期数，再对 100 张图取算术平均。
输入、核数、单核分母和官方评分程序一致。**历史 V2.0 每张图每种核数各有 600 秒预算；本轮 V3 每张图四种核数合计共享 600 秒目标，近期改进可延至 900 秒，已启动工作由 1200 秒保护。**
因此表内 V2 数字仅是历史成绩对照，不能解释为同时间预算下的算法增益；目前的 V2.1 接口版也没有在此重跑全量。
V3 对历史 V2.0 为 223 胜、168 平、9 负；对 Q1v3 为 162 胜、26 平、212 负。

## 搜索与核验口径

100 张图均完成四种核数的方案输出；80/400 组搜索耗尽提案，其余因时间窗口停止，不能称为搜索最优。
当次基座保底在 331/400 组完成；保底未完成的组不享有完整基座不退化保证。
整图平均实际搜索墙钟 630.08 秒，17 张图使用延长机制，0 张图触及硬保护超时。
本批次曾调整外层并发并在机器重启后恢复；只复用源码、输入、参数指纹相同且产物完整的已完成图。最终批次参数为 6 路并发，历史段见 `execution_segments.json`。
独立官方复评在搜索冻结后进行，不计入搜索墙钟；搜索内精确成绩与独立复评逐组一致。

## 归档文件

- `plans.zip`：400 份原样官方格式方案，成员为 `case_001/N2/plan.json` 等；不包含赛题输入。
- `details.csv`：逐组正式周期、加速比、搬运量、搜索终止原因、墙钟与证据哈希。
- `comparison.csv`：同图同核数的历史 V2.0 与 Q1v3 对照；预算不一致的限制如上。
- `batch_request.json`、`batch_summary.json`、`replay_summary.json`：本次运行和独立复评的原始汇总。
- `provenance.json`、`input_sha256.txt`：源码、配置、输入及对照来源指纹。
- `artifact_hashes.csv`：本机 3,200 份冻结及复评产物的大小和 SHA-256。未上传的完整官方结果、Trace 和中间搜索文件不能仅凭此清单从仓库下载。
- `archive_validation.json`、`manifest.sha256`：打包检查及发布文件校验值。

## 复现与独立评分

使用 Python 3.12，先准备官方 `case_001.json` 至 `case_100.json` 和 `official/data/config.txt`，并按 `input_sha256.txt` 核对输入。运行本次算法应检出上面的源码提交；墙钟截断及多进程争用会使重新搜索的方案可能不同。
若只需复核已发布的方案，在仓库根目录运行：

```bash
python -m zipfile -e results/q2_optimized_full_20260927/plans.zip outputs/q2_v3_published_plans
mkdir -p outputs/q2_v3_recheck_case001_N5
python official/code/multicore_cut_evaluate_problem_2.py official/data/case_001.json \
  outputs/q2_v3_published_plans/case_001/N5/plan.json --config official/data/config.txt \
  -o outputs/q2_v3_recheck_case001_N5/result.json
```

对其余 399 组按相同格式替换图、核数和输出目录；将官方 `makespan` 与 `details.csv` 对照。
