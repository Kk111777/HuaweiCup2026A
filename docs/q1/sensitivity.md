# 第一问基座算法：β 灵敏度分析

本实验使用第一问 **V1 TaskGrowth 基座**，测试固定 β=1、2、3、4，并与默认自适应 β 配对比较。不调用 V2/V3，不修改基座或官方算法。源码入口为 `tools/q1_sensitivity.py`，配置为 `configs/q1/sensitivity.json`。

默认从 100 张官方图中抽取 20 张：按非 COPY 计算操作数和 case ID 排序，分为连续四档，各档 25 张，用固定种子 20260927 各抽 5 张。模拟核数为 2、5，共 `20 × 2 × (4 + 1) = 200` 份最终方案。抽样只使用输入规模，不访问既有成绩。另支持显式小样本验证。

## 环境与启动

Python 3.12 或更新版本；求解、复评、审计和无图报告只用标准库。官方图准备在 `official/data/case_001.json` 至 `case_100.json`，官方配置固定读取仓库 `official/data/config.txt`。只有绘图需要 matplotlib，可安装在自己的虚拟环境中：

```bash
python -m pip install matplotlib
```

先做一个小样本，检查代码、输入和官方程序能接通：

```bash
python tools/q1_sensitivity.py run --data-dir official/data --cases case_001 --cores 2 --workers 1 --output-dir outputs/q1_sensitivity_smoke
python tools/report_q1_sensitivity.py --output-dir outputs/q1_sensitivity_smoke
```

正式运行：

```bash
python tools/q1_sensitivity.py run --data-dir official/data --output-dir outputs/q1_sensitivity --workers 2
python tools/q1_sensitivity.py audit --output-dir outputs/q1_sensitivity
python tools/report_q1_sensitivity.py --output-dir outputs/q1_sensitivity
```

也可先冻结样本而不启动实验，再检查 `experiment.json` 中的名单：

```bash
python tools/q1_sensitivity.py prepare --data-dir official/data --output-dir outputs/q1_sensitivity
python tools/q1_sensitivity.py run --output-dir outputs/q1_sensitivity --workers 2
```

`prepare` 与直接 `run` 是两种起步方式，不要在同一非空目录重复 prepare。已准备的目录运行时不再指定 `--data-dir/--config/--cases/--cores`；这些值从冻结清单读取。改变参数或源码须用新输出目录。`--workers` 可按机器资源调整，它是实验并行度，不是模拟 NPU 核数。

## 续跑、失败与部分报告

```bash
python tools/q1_sensitivity.py run --output-dir outputs/q1_sensitivity --workers 2 --resume
python tools/q1_sensitivity.py audit --output-dir outputs/q1_sensitivity --allow-partial
python tools/report_q1_sensitivity.py --output-dir outputs/q1_sensitivity --allow-partial --no-plots
```

续跑逐一检查输入、配置、实现与结果哈希，并要求 Python 版本一致。只复用完整图/核数组合；不恢复半途候选。每个未完成尝试保留在 `runs/<case>/.N<n>-<timestamp>/`，成功后才原子发布至 `N<n>/`。异常另存 `failures/`；失败或缺失不会记为零，不进入正式均值。

同一输出目录有独占 `run.lock`。强制终止进程可能留下锁；先核实锁中 host/PID 对应进程已结束，才移除这个锁并续跑，不能在另一运行仍活跃时删除。不要同时运行两个相同输出目录的批次。

默认不设置搜索时间截断，完整计算四个 β。可在实验配置中给 `official_timeout_seconds` 设置单次独立官方复评的秒数上限；超时会使该组合失败并保留诊断，不会发布截断方案，也不表示整个算法具备硬时间上限。CLI 返回 0 表示请求成功，1 表示批次有未完成组合，2 表示参数/输入/指纹或审计错误。

## 算法与公平性

基座的目标粒度为

`H_beta = max(1, ceil(max(W_M, W_V) / (beta * N)))`。

`W_M/W_V` 是两类计算管道的总工作量，`N` 是模拟核数。β 越大，目标粒度越细；实际 Task 数和收益仍由依赖、生长规则和官方模拟决定。

完整扫描直接调用原生 `initialize(..., beta_search='grid')`。观察包装只保存每个候选的完整结果，不改变候选生成、排序或接纳。同一 β 的 global/components 候选仍按原目标 `(makespan, added_copy_bytes, mode_order, beta)` 选最优。各候选从原图重新构造，共享只读索引，无候选热启动。

原生 `fixed` 只表示 β=1，因此本工具没有把它误用为任意固定 β。β=2/3/4 来自完整扫描中各自档位的独立构造。

默认自适应策略另外原生运行。每种模式从 β=1 开始，下一档不再严格减少周期时停止；随后择优。程序检查该原生方案与完整扫描中按同一早停规则重建的方案完全一致。任何候选失败会使该图/核数组合失败，不用缺失候选伪装完整扫描。

最终 5 份方案逐份写入、由独立进程调用原官方 CLI 重读复评，除 `input_graph/input_plan` 文件名元数据外，全部结果字段须与构造内评价一致。相同方案也分别复评。结果与 Trace 使用 gzip 无损保存。

## 指标与输出

主指标逐例计算

`D(case, N, beta) = 100 * (T_fixed / T_adaptive - 1)`。

`T` 是官方周期数，正值表示固定 β 比默认策略慢。先求逐例比值，再汇总均值、中位数、四分位区间和最大变化。额外记录 Task 数、搬运和 spill。自适应对四档最短周期的差距用于检查早停代价，不称为全局最优性差距。

固定 β 时间是共享图读取/索引时间加该 β 各模式候选时间之和；自适应时间为单独原生调用，图已读取。两种计时不完全对称，只作成本诊断，不据此宣称端到端运行加速。官方复评时间单列。不要把模拟周期的下降与 Python 求解秒数混在一起。

| 文件 | 内容 |
|---|---|
| `experiment.json` | 抽样总体与名单、参数、输入/官方/源码哈希、Python 版本 |
| `run_status.json` | 已验证数、失败及当前批次状态 |
| `details.csv` | 每个 case/N/variant 的原始指标 |
| `paired_details.csv` | 对默认自适应的配对变化与四档最优差距 |
| `summary.csv/json` | 分核、分 β 的统计与诊断量 |
| `REPORT.md` | 中文实验说明、结果表及完整性标识 |
| `figures/beta_sensitivity.png/svg` | 可用于论文的英文标注曲线，阴影为样本四分位区间 |
| `audit.json`、`report_manifest.json` | 审计结果与报告产物哈希 |
| `runs/<case>/N<n>/` | 全部候选、自适应原生记录、5 份方案、官方结果/Trace/日志、完成清单 |

图中的阴影不是置信区间。20 张图的分层样本不等于全部 100 张图；显式 smoke 更不应作为正式灵敏度结论。官方原图已随仓库提供，见[数据说明](../data.md)。新的 `outputs/` 和本机路径记录留在本地；已完成的 5 核样本实验见[正式归档](../../results/q1_sensitivity_5cores_20260927/README.md)。

## 验证

```bash
python -m unittest discover -s tests -p test_q1_sensitivity.py -v
python -m unittest discover -s tests -p test_q1_initialization.py -q
```

实验测试覆盖分层抽样、固定 β=1 与原生策略一致、小图独立官方复评、早停重建、完整结果复用、未完成尝试保护、损坏产物/输入变更拒绝、运行锁、部分状态以及逐例比值先算后平均。

2026-09-27 本地验收：上述 4 项实验测试和 35 项基座测试全部通过。正式版入口在 `case_026/case_011 × 2/5 核 × 5 策略` 上完成 20/20 独立官方复评，208 个产物哈希通过审计；续跑完整复用，报告及 PNG/SVG 成功生成并检查布局。这是代码有限验收，不是 20 张分层图的完整灵敏度成绩。
