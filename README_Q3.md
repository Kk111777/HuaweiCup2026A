# 第三问完整复现工程

入口为 `src/solve_problem3.py`，Python **3.12**。输入采用官方原始操作—张量 DAG；硬件固定为官方 `official/data/config.txt`。输出方案严格只有 `node_to_subgraph`、`core_schedules` 两个字段，文件名为 `<case>_multicore_res.json`。

2026-09-27 本机正式百例 × 1–5 核已完成 **500/500 组官方复评，0 失败**，完整产物审计通过。相对 Q2 方案直接启用 L2，263 组提速、237 组时间持平；运行预算、基准版本及无 L2 对比中的一项例外详见 `docs/q3/validation.md`。

可下载的 500 个方案、逐组指标、图表和来源记录见[正式结果归档](results/q3_full_20260927/README.md)。原始大体积事件时间线保留在本机，归档中提供文件指纹及重新复评方法。

Q3 直接接收 Q2 最终计划和已验证成绩。默认寻找仓库内 `results/q2_repair_full_20260926` 的已发布结果；核对发布清单、原图和官方环境后直接继承固定成绩，不重复求解 Q2。缺少对应图、对应核数的结果时，**自动调用 `src/solve_problem2.py` 生成并验证 Q2 结果，再进入 Q3**。只有计划、没有可验证成绩时补做一次 Q2 官方评价。不用手动先跑第二问，也不要求导入旧搜索日志或旧时间线。N=1 的无 L2 基准优先复用 `results/problem_1_singlecore` 的官方固定整图单核档案。

本实现使用保留的 **Q2 Repair 2.1** 准备/状态/容量接口。默认发布档案的算法成绩来自 Repair 2.0；2.1 提供接口并保留该基座算法。主分支并存的 `q2/optimized` v3 未被本入口自动替换使用；其合法最终方案也可以通过 `--q2-source` 显式输入。

## 环境与数据

在工程根目录运行。求解器和测试仅用 Python 标准库；默认报告画图额外需要 matplotlib：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-q3.txt
```

将正式原图置于 `official/data/case_001.json` 至 `case_100.json`。原图不纳入 Git，但可包含在本地复现包中。官方代码和配置通过 `configs/q2/official_sha256.json` 校验，不允许修改带宽、Cache 容量或官方调度逻辑。只有最终报告需要 matplotlib；可用 `--no-plots` 运行纯标准库流程。

## 直接运行

单张图，同时求 1–5 核，自动处理 Q2 前置输入：

```powershell
python src/solve_problem3.py solve official/data/case_016.json --cores 1 2 3 4 5 --output-dir outputs/q3_case016
```

使用已有 Q2 新基座结果目录：

```powershell
python src/solve_problem3.py solve official/data/case_016.json --cores 2 3 4 5 --q2-source outputs/q2_run --output-dir outputs/q3_from_q2
```

也可将 `--q2-source` 指向某个核数的 `plan.json`（此时只请求该核数），或带 `details.csv`、`provenance.json` 的 `plans.zip`。目录识别规则为 `<root>/<case>/N<n>/plan.json`、`<root>/N<n>/<case>_plan.json`、`<root>/N<n>/plan.json`、`<root>/plan.json`。目录存在但缺少所需组时自动调用 Q2；显式路径拼错、计划非法或档案哈希冲突会报错。

百例 × 五种可用核数：

```powershell
python src/solve_problem3.py batch --data-dir official/data --cores 1 2 3 4 5 --workers 4 --output-dir outputs/q3_full
```

`--workers` 是电脑上同时处理的原图数；`--cores` 是每组模拟的 NPU 核数。每张图的所有请求核数共享默认 600 秒 Q3 预算，包含输入处理、搜索和最终验证；生成 Q2 的用时单列，不占这 600 秒。默认 Q2 前置预算为每个缺失核数 600 秒。报告制图、压缩导出不计入原图求解时间。

在新输出目录上可改变搜索预算：

```powershell
python src/solve_problem3.py batch --data-dir official/data --cases case_001 case_016 case_028 case_086 --workers 4 --wall-seconds 600 --q2-seconds 600 --max-candidates 240 --output-dir outputs/q3_selected
```

与相同命令一起加 `--resume`，只会复用输入、算法参数、Q2 输入、源码、官方环境及全部输出文件哈希一致的完整案例。失败案例保留在 `.case_*-attempt-*`，重新执行时可以重试；已发布但被修改的案例会拒绝续跑，须使用新的输出目录。不会覆盖用户已有结果。

## 输出与验证

批次根目录：

- `run_info.json`：环境、完整输入哈希、算法参数、请求覆盖率和批次状态。
- `summary.json` / `details.csv` / `report.md`：逐用例成绩、逐核平均值、失败或缺失列表。
- `plans.zip`：`N1/` 至 `N5/` 下可直接提交的两字段方案。
- `figures/`：平均周期/加速比曲线、优化前后散点图、每种核数逐例对比图。
- `report_artifacts.json`：汇总文件及图片的 SHA-256。

每个 `case_XXX/N<n>/`：

- `q2_plan.json`、`q2_source.json`：冻结参照与输入来源；如自动生成，另有 `q2_bootstrap/` 完整 Q2 证据。
- `no_l2/`：固定分子 T2；`reference.json` 记录复用的已验证 Q2/固定单核档案及哈希，或保存补做的独立官方 Q2 复评产物。
- `seed_l2/`：Q2 计划直接启用 L2 的独立官方成绩 T2→3。
- `case_XXX_multicore_res.json`：Q3 最终提交方案。
- `verified/result.json`、`trace.json`、`log.txt`：独立官方 Q3 子进程输出。
- `audit.json`：操作覆盖、依赖、Pipe、跨核同步、私有容量、Cache 容量和实际传输路径复核。
- `metrics.json`：该组数值指标。

每张图另有 `case.json`、`search.json`、`complete.json`。仅完整官方复评、附加审计、源码稳定性检查和预算检查全部通过，才发布 `SUCCESS_VERIFIED`。`stop_reason=candidate_limit/case_time_budget/evaluation_timeout` 表示保留已验证方案并停止有限搜索，不宣称全局最优。失败不会以部分结果冒充整张图完成。

对任意已给定方案独立复核：

```powershell
python src/solve_problem3.py verify official/data/case_016.json outputs/q3_full/case_016/N3/case_016_multicore_res.json --output-dir outputs/q3_verify_case016_N3
```

校验全部已发布产物哈希并重新出报告：

```powershell
python src/solve_problem3.py report --output-dir outputs/q3_full
```

全量结束后，独立核对覆盖率、全部完成清单、原图/源码/官方环境哈希、逐组数值、逐核单调性、CSV 和方案 ZIP：

```powershell
python tools/audit_q3_results.py --output-dir outputs/q3_full --audit-output outputs/q3_full/final_audit.json
```

该命令只读取既有证据，不再次运行搜索或改写方案；审计文件不能覆盖已有文件。运行期间可加 `--allow-partial` 检查已经发布的案例，此时状态明确为 `PARTIAL_VERIFIED`，不表示全量完成。

退出码：0 为请求全部通过；1 为求解/报告覆盖不完整；2 为输入、环境或复核错误。独立官方日志中的绝对路径记录的是复评时的临时目录；发布后通过同目录方案文件和 SHA-256 对应，不修改原官方结果数值或日志。

## 指标口径

优化目标按字典序最小化 `(官方 makespan, 官方 added_copy_bytes)`。命中率不替代目标，允许增加额外搬运量换取更短时间。

| 指标 | 定义 |
|---|---|
| 硬件加速比 | T2 / T2→3 |
| 算法加速比 | T2→3 / T3 |
| 总加速比 | T2 / T3 |
| 字节命中率 | 官方 hit_bytes / (hit_bytes + miss_bytes) |
| 实际 DDR 字节 | 逐条实际 DDR 路径的 COPY 工作字节和，单独列出 |

平均加速比是**逐用例比值的算术平均**。同一方案的官方 `added_copy_bytes` 不因 L2 命中扣减。N=1 有 L2 成绩实际计算，不强制加速比为 1。更多可用核数可以保留空核；末尾升序传递较少核数的好方案，保证本次请求中相邻核数最终时间不增加。

## 算法与回归

实现与限制见 `docs/q3/implementation.md`；实际验收记录见 `docs/q3/validation.md`。

```powershell
python -B -m unittest discover -s tests -p test_q3.py -v
python -B -m unittest discover -s tests -p test_q2_interfaces.py -v
```

附件中的 S1–S4 为机制小图，分别是 `shared_0/1/3/6`，不与正式比赛 case 编号混用。`tests/fixtures/q3/provenance.json` 记录用户 ZIP 及每个提取文件的哈希。测试包含 68 组小图的初始/最终完整官方一致性、200 组带宽放松枚举对照、联合候选、容量修复、Q2 自动生成、超时清理、提交验证及哈希续跑。
