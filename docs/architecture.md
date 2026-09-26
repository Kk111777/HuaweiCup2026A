# 项目结构与基线复用

程序按问题组织，结果按实验归档。第一问保留三版模型：V1 TaskGrowth 基座；V2“基座 + 基于严格剪枝的多原则候选方案分层改进策略”；V3 基座专用优化模型。V2/V3 是从 V1 派生的两条路线，发布顺序不是算法依赖顺序。总览见 [README 模型谱系](../README.md#第一问模型谱系)。

## 文件树与职责

```text
HuaweiCup2026A/
├─ official/                         # 官方程序、说明和配置；计算图本地准备
├─ src/
│  ├─ problem_1_baseline.py           # 稳定的单核参照入口
│  ├─ solve_problem1.py               # V2 improve 子命令 / V3 默认图路径入口
│  ├─ solve_problem2.py               # Q2 solve / batch / verify
│  ├─ solve_problem3.py               # Q3 solve / batch / verify / report
│  ├─ common/baseline.py              # 基线读取、来源校验与加速比
│  ├─ common/artifacts.py             # 无问题语义的原子产物写入与哈希
│  ├─ q2/                            # Scene B 增量构造、内存证书、窗口 DP、完整复核
│  ├─ q3/                            # 固定 Q2 输入、L2 搜索及独立官方复评
│  └─ q1/
│     ├─ graph.py, model.py           # 共享图模型与基座特征索引
│     ├─ partition.py, solver.py      # V1 构造策略与初始化择优
│     ├─ schedule.py, allocation.py   # 共享核心分配
│     ├─ official.py                 # 原版官方程序的薄适配
│     ├─ reference_cli.py, run.py     # V1 参考入口及公共产物函数兼容导出
│     ├─ stage2/                     # V2 五原则诊断、三层搜索与严格剪枝
│     ├─ unified/                    # V3 统一搜索、预算与评估服务
│     └─ improvement/                # V3 结构、近似及局部改进策略
├─ configs/q1/stage2.json             # V2 参数
├─ configs/q2/                       # Q2 参数及官方版本指纹
├─ configs/unified.json               # V3 既有参数路径
├─ tools/                            # V1 入口、第三版审计、Q2 试放微基准
├─ scripts/                          # 第三版批量实验脚本
├─ docs/q1/                          # initialization / stage2 / unified
├─ docs/q2/                          # 工程审计、设计原稿、实现验收
├─ tests/                            # 公共基础、Q1 三版及 Q2 测试
├─ results/problem_1_singlecore/      # 固定的官方单核归档
├─ results/q1_adaptive_100cases_20260925/ # V1 完整 400 组合归档
└─ outputs/                          # 本地实验与中间产物，Git 忽略
```

| 模型 | 显式入口 | 算法说明 |
|---|---|---|
| V1 | `python tools/reference_problem1.py solve ...` / `batch ...` | [初始化](q1/initialization.md) |
| V2 | `python src/solve_problem1.py improve ...` / `improve-batch ...`；或配置 `PYTHONPATH=src` 后 `python -m q1.stage2 ...` | [分层改进](q1/stage2.md) |
| V3 | `python src/solve_problem1.py graph.json --config ... --cores 4 --output ...` | [统一搜索](q1/unified.md) |
| Q2 新基座 | `python src/solve_problem2.py solve ...` / `batch ...` / `verify ...` | [状态与评价接口](q2/interfaces.md)、[算法修复](q2/repair.md) |
| Q3 | `python src/solve_problem3.py solve ...` / `batch ...` / `verify ...` / `report ...` | [运行说明](../README_Q3.md)、[实现](q3/implementation.md)、[全量验收](q3/validation.md) |

不复制三个版本的基座或官方代码。V2 使用 V3 已接入的共享 `GraphIndex`，保持自身状态、候选、剪枝与精确接受逻辑；V3 的算法模块与配置在本次接入中保持原样。V1 的当前参考实现按 V3 已有约定仅允许 `candidate_jobs=1`，历史并行版本可从提交 `04634a2` 检出。已有原始 V1 归档可直接供 V2 读取。

第二问由 `q2/` 独立实现，配置位于 `configs/q2/`；第三问由 `q3/` 和 `configs/q3/` 提供独立入口，复用 Q2 2.1 的准备/状态/容量接口并调用官方 Q3 事件仿真。缺少 Q2 最终输入时先自动生成；既有结果核对来源后冻结为比较基准。V3 的现有配置路径保留，历史实验目录、归档指纹和官方代码不随展示调整搬动。

第二问的官方语义与模块复用依据见早期[工程准备与源码审计](q2/engineering.md)，当前接口见[Q2 2.1](q2/interfaces.md)，验证见[有限测试报告](q2/interfaces_validation.md)。Q2 不调用 Q1 的 Task 划分、核心分配和 Task 等待模型；复用 `common.baseline`、输出锁，以及从 `q1.run` 原样抽出的四个产物工具函数。`q1.run` 保留兼容导入，并将新公共文件纳入来源哈希，因此旧源码产物不能冒充本次源码的续跑结果。

Q2 2.1 保留2.0按构造／范围／编码独立维护的 β 流，官方时间持平继续、严格增加停止；真实顺序、容量修复及显式1.1对照见[算法设计](q2/repair.md)。状态独立于构造来源，近似搜索快照与正式发布分离；本轮没有近似模型或Q2公共优化后端。Q1实现和历史归档保持原样。

三版输出分别指定 `outputs/q1_v1`、`outputs/q1_v2`、`outputs/q1_v3` 等目录，不能让不同求解器写入同一实验目录。源码、参数和运行环境均进入各自来源记录；Git 接入后的字节指纹与服务器旧运行包不同，不能把旧产物重新标成新源码产生的结果。

`official/` 保持原始布局和字节不变。不同问题的评估器分开调用；第二问已核对固定单核分子的题面与源码口径，默认经 `common.baseline` 校验后只读复用，不能以 Q2 的 N=1 优化结果替换。第三问采用同核数固定 Q2 时间 T2 与最终 Q3 时间 T3 比较，并单列同一 Q2 方案启用 L2 的 T2→3；500 组官方复评和指标口径见[Q3 验收记录](q3/validation.md)。

## 基线读取接口

`common.baseline.load_baseline_catalog(archive_dir, input_paths, config_path, official_code_dir, *, repo_root=...)` 返回按 case 文件名 stem 索引的字典。每项包含：

| 字段 | 含义 |
| --- | --- |
| `case` | 如 `case_001`，匹配原始输入文件名 |
| `makespan_cycles` | 官方单核结果顶层 `makespan`，正整数 cycles |
| `baseline_ref.archive` | 相对于仓库根目录的归档路径 |
| `baseline_ref.manifest_sha256` | 归档清单 SHA-256，固定本次使用的归档版本 |
| `baseline_ref.case` | 对应基线用例 |
| `baseline_ref.result` | 相对于归档目录的官方结果路径 |
| `baseline_ref.result_sha256` | 该官方结果文件的 SHA-256 |

调用时校验整个归档的清单及文件 SHA-256、完整成功状态、逐例汇总与结果/Trace/日志的一致性，然后核对当前配置、10 个官方程序以及请求输入的 SHA-256。历史路径只用于识别文件名，实际文件位置由调用参数决定。缺少文件抛出 `FileNotFoundError`，内容或版本不匹配抛出 `ValueError`；不降级使用旧成绩，也不自动重算。

归档必须位于 `repo_root` 内；输入与官方材料可通过参数指定其他位置。一次批量加载后，将各 case 的记录传给 worker，避免每个 worker 重复校验约 469 MiB 的完整归档。接口只读，不复制或覆盖基线。

从仓库根目录执行以下 PowerShell 示例；先按根 README 准备官方计算图：

```powershell
@'
from pathlib import Path
import json
import sys

root = Path.cwd()
sys.path.insert(0, str(root / "src"))
from common.baseline import load_baseline_catalog

catalog = load_baseline_catalog(
    root / "results/problem_1_singlecore",
    sorted((root / "official/data").glob("case_*.json")),
    root / "official/data/config.txt",
    root / "official/code",
    repo_root=root,
)
print("已校验用例数：", len(catalog))
print(json.dumps(catalog["case_001"], ensure_ascii=False, indent=2))
'@ | python -X utf8 -
```

完整材料应返回 100 项，`case_001` 为 **233110 cycles**，`case_003` 为 **602212 cycles**。归档入口见[完整单核结果](../results/problem_1_singlecore/README.md)。

## 加速比与后续程序接入

计算口径为同一输入、匹配配置和官方程序版本下的：

```text
speedup = 单核官方结果的顶层 makespan / 多核官方结果的顶层 makespan
```

在各问程序已验证多核结果来自同一输入和评测配置之后调用：

```python
from common.baseline import calculate_speedup

baseline = catalog[case_id]
row["baseline_cycles"] = baseline["makespan_cycles"]
row["makespan_cycles"] = official_result["makespan"]
row["speedup"] = calculate_speedup(row["baseline_cycles"], row["makespan_cycles"])
row["baseline_ref"] = baseline["baseline_ref"]
```

`calculate_speedup` 拒绝非正整数、浮点数、布尔值和缺失值。程序执行秒数用于统计搜索成本，不能替代 cycles；Task 局部耗时也不能替代总 makespan。`--workers` 是电脑上的并行进程数，与模拟 NPU 核数不同。

第一问 V1 参考入口与 V2 改进入口默认从已发布归档批量加载基线，再将单条记录传给各 case 构造流程。多核结果及汇总记录 `baseline_ref`，不复制已发布的单核产物。自定义小图可显式使用 `--recompute-baseline`；默认归档校验失败时直接报错。输入、配置或官方程序改变时，显式运行新的基线实验并以新版本归档，不覆盖现有归档。

各核数分别计算逐例加速比的等权算术平均，并报告 `successful_cases / requested_cases`。失败项不填 0，也不能悄悄省略后仍标为完整成绩；不足 100 例时标注“部分结果”，且只比较相同用例集合。记录各例的实际搜索预算与参数，混合预算的实验须明确说明。

## 正式实验归档

新实验先写入 `outputs/qN/<实验编号>/`。用户明确要求上传后，再整理为 `results/qN/<日期_算法版本>/`；每次正式归档使用新目录，历史归档保持不变。

后续默认采用精简可复核归档：

- 保留各 case、各核数的最终方案和最终官方结果，以及逐例汇总、必要图表和报告。
- 保存运行参数、每例预算、随机种子（如使用）、Python/依赖信息、源代码提交及文件哈希、原始输入和评估器哈希、基线引用、复现命令及产物 SHA-256 清单。
- 依赖历史保底方案时，记录已归档的来源及哈希；来源尚未入库时，包含复现所需的最小方案和来源信息，不能引用队友机器上不存在的目录。
- 大型 Trace、过程日志和中间候选留在本地；通过最终方案调用官方评估器可重新生成 Trace。需要完整诊断归档时，另行明确上传范围。

`results/problem_1_singlecore/` 已有的 302 份原始有效产物继续完整保留。新实验只保存 `baseline_ref`，不再复制这些产物。历史 `run_info.json` 中的绝对路径是执行记录；新引用使用仓库相对路径，读取者以自己的仓库根目录解析，并校验保存的清单哈希。

## 验证与 Git 协作

运行当前基线及公共接口测试：

```powershell
python -X utf8 -m unittest discover -s tests -v
```

测试包含官方最小图实际执行、串行/并行一致性、归档损坏与版本不匹配拒绝，以及移动仓库后的引用有效性。正式上传另核验全部 100 例归档；结构整理不需要重算全量调度。

每项后续任务从更新后的 `main` 建立 `feat/<用户名>/<任务>` 或 `fix/<用户名>/<任务>` 分支，通过 PR 说明改动、验证和结果口径。提交、上传、合并继续分别遵循根目录 `AGENTS.md` 的明确授权规则；已经 Squash 合并的分支不复用。
