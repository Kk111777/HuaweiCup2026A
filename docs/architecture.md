# 项目结构与基线复用

程序按问题组织，结果按实验归档。公共工具负责校验和读取基线，各问负责自己的切图、排布、调度和官方评测适配。当前仓库提供完整单核基线、公共接口及第一问初始化算法；后续算法通过新任务分支与 PR 加入。

## 文件树与职责

```text
HuaweiCup2026A/
├─ official/                         # 原始官方代码、说明及配置
│  └─ data/                          # config.txt 跟踪；原始 case 图留本地
├─ src/
│  ├─ problem_1_baseline.py           # 现有稳定入口，保留原始字节与来源哈希
│  ├─ common/baseline.py              # 公共基线读取与加速比计算
│  ├─ q1/                            # 第一问切图、排布、调度、搜索与评估
│  ├─ q2/                            # 第二问
│  └─ q3/                            # 第三问
├─ configs/q1/、q2/、q3/              # 各问的搜索参数
├─ results/
│  ├─ problem_1_singlecore/           # 已发布的完整基线，位置和内容固定
│  └─ q1/、q2/、q3/<日期_算法版本>/    # 各问正式实验归档
├─ outputs/<问题>/<实验编号>/         # Git 忽略；实验、Trace、日志和候选
├─ docs/
│  ├─ architecture.md                # 本指南
│  ├─ environment.md                 # Python 环境
│  └─ q1/、q2/、q3/                  # 各问算法与复现说明
├─ tests/                            # 基线、公共工具及各问测试
└─ notebooks/                        # 分析与可视化
```

第一问初始化入口为 `src/solve_problem1.py`，实现放在 `src/q1/`：`graph` 建立索引，`partition` 切图，`schedule` 分核排布，`official` 适配官方接口，`solver` 构造并选择完整候选，`run` 负责产物与批处理。算法和接口见 [初始化说明](q1/initialization.md)。尚无代码的目录不提前创建占位实现。

各问参数逐步整理到 `configs/qN/`，算法文档整理到 `docs/qN/`；迁移时同步更新默认路径、说明和测试。现有本地实验目录不为匹配文件树而直接搬动，避免破坏正在使用的保底方案或历史复现记录。

`official/` 保持官方布局和字节不变。不同问题的评估器分开调用；将来第二、三问复用单核分子前，必须先核对赛题与评测语义，不能只按相同 case 编号认定适用。

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

第一问初始化入口默认从已发布归档批量加载基线，再将单条记录传给各 case 构造流程。多核结果及汇总记录 `baseline_ref`，不复制已发布的单核产物。自定义小图可显式使用 `--recompute-baseline`；默认归档校验失败时直接报错。输入、配置或官方程序改变时，显式运行新的基线实验并以新版本归档，不覆盖现有归档。

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
