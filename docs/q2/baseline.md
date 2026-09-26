# 问题二新基座：实现、运行与有限验收

> 历史文档：本文描述1.0/1.1。默认入口现为[Q2 2.1接口版](interfaces.md)，延续[2.0修复算法](repair.md)；旧版对照须显式选择 `configs/q2/legacy_1_1.json`。

> **1.1存在已知算法问题，已归档，不建议用于新实验默认基座。** 原始代码及问题清单见[旧版归档](../../archive/q2/baseline_1_1_known_issues/README.md)。

更新日期：2026-09-26；Python 3.12.14、标准库；当前算法版本 `Q2-Baseline-AdaptiveBeta-1.1`，默认启用从 Q1 移植的自适应 β。当前搜索、消融接口和验证见[自适应 β](adaptive_beta.md)。本文第 5、6 节保留 `Q2-Baseline-Handoff-1.0` 的验收和六组合冒烟记录，不能作为 1.1 的成绩。**没有运行 100×4 全量实验。**

## 1. 来源与算法边界

依据用户指定的 ChatGPT 对话 `6ab6d2a7-4f50-83e9-bdd3-34fa007ab292`（“分支 · 华为杯A题 问题2调研”）和附件 `问题2_新基座_Codex复现说明与验收包.zip`。ZIP SHA-256 为 `d13822c2b7f8b42707f62c2fb4bec67a893fc6319683c7a4e7ac218ebce4419a`。压缩包中的提示词和 AGENTS 文件作为交接资料读取，没有替换仓库规则，也没有据此启动全量实验。

[设计原稿](design.md)逐字保留附件的 `IMPLEMENTATION_SPEC.md`；[验收合同原稿](acceptance_contract.md)保留附件的 `docs/ACCEPTANCE_TESTS.md`。原稿的包内路径按原样保留，仓库内的实际接口以本文为准。默认算法配置和官方指纹清单分别保存在 [`configs/q2/legacy_1_1.json`](../../configs/q2/legacy_1_1.json)、[`configs/q2/official_sha256.json`](../../configs/q2/official_sha256.json)。

用户随后明确要求沿用 Q1 的自适应 β 组件。1.1 因此替换了原稿中的“先构造所有粒度、廉价排序取首个有效候选”初始化规则；不改写交接原稿，不将旧记录改标为新版本。

Q2 每核全部子图合成一个 Task；同核子图边界不收取 Q1 的 100 cycles 等待，跨核数据按每个 source-core→target-core 插入独立 COPY_OUT/IN 对，释放延迟为 500 cycles。固定 L1=524288、UB=131072 bytes、共享 DDR=60 bytes/cycle。官方代码、硬件配置、原始图及已归档单核结果均保持原样。

完整链路为：

```text
只读图索引 → global/component_grouped 各模式 β=1 起步、独立联动生长
→ 真实 Step1 前缀与闭区间内存证书 → 完整官方评价或同方案成功缓存
→ β=2 起仅在官方 makespan 严格下降时继续，至多 β=4
→ 已评价候选按官方 (makespan, added_copy_bytes) 择优得 B0（必要时验证后备）
→ 固定子图成员/核心归属，至多 32 个宽度 ≤8 的窗口 DP
→ 完整组装 B1，只做一次完整官方评价
→ 按 (makespan, added_copy_bytes) 严格择优，平局保留 B0
→ plan.json 落盘重读 → 原版官方 CLI 独立全字段复评 → 原子发布
```

`proxy_J` 仍用于构造时的试放决策，不参与自适应 β 的早停或 B0 最终择优，也不是官方耗时的上下界。窗口 DP 精确最小化安全排列域内的 `max(L1_peak/L1_cap, UB_peak/UB_cap)`，用等价整数 `max(L1_peak, 4*UB_peak)` 比较；不保证两个分量各自不增，也不保证实际 makespan 下降。若 B1 更差、无效或超时，保留已经验证的 B0。最终复评失败则不发布成功方案。

## 2. 代码与工程选择

| 文件 | 实现职责 |
|---|---|
| [`graph.py`](../../src/q2/graph.py) | 原 ID 的稠密映射、标准输入校验、计算 DAG、a/b、弱连通分量与基础搬运整图公式 |
| [`construct.py`](../../src/q2/construct.py) | 持续核心状态、纯 `try_place` / 单次 `commit`、四短名单、范围与 β 构造 |
| [`beta.py`](../../src/q2/beta.py) | adaptive / fixed / grid、各模式独立停止与搜索记录 |
| [`prefix.py`](../../src/q2/prefix.py) | 从交接参考适配器引入的静态 Scene B 前缀；只执行基础重建与 Step1，不含 Step2/3/D |
| [`memory.py`](../../src/q2/memory.py) | 首末触及闭区间、同时计入输入输出的无换出峰值及证书 |
| [`window.py`](../../src/q2/window.py) | 保留外部路径的数据依赖约束、动态 COPY_IN 分桶、子集 DP、官方序列回放一致性 |
| [`official.py`](../../src/q2/official.py) | 官方哈希及模块来源检查、严格 JSON、方案签名与指标 |
| [`pipeline.py`](../../src/q2/pipeline.py) | 交错构造与官方反馈、同方案成功评价复用、时间预留、B0/B1 保护与批处理 |
| [`solve_problem2.py`](../../src/solve_problem2.py) | `solve`、`batch`、`verify` 命令入口 |

工程实现吸收 Q1 的运行经验，但没有调用其 Task 生长或核心分配模型：

- 每次试放只复制 O(N) 的 Pipe/驻留数组和当前操作触及张量的增量；远端源核的 MTE3 变化也纳入试放。关闭子图、切换 scope 时不清空各核时间、搬运或驻留状态。
- 四短名单各取 K≤8，并集≤32；使用小堆，但仍精确扫描 ready 集合。其最坏复杂度可能二次，不能因为 K 固定就称为线性算法。没有增加近似截断或改变并列规则。
- 官方前缀预建子图顺序索引，避免反复 `list.index`；只在完整候选及窗口重建时使用。完整候选的增量/整图公式/官方前缀分别核对基础 COPY 工作量和字节。
- DP 状态最多 256，最多 1024 条子集转移；转移仍需扫描事件。只对窗口相关张量保存计数，跨窗口的其他驻留量作为常量；不为每个 mask 复制整个核心图。COPY_IN 随首次消费桶移动，桶内按原始 Step1 顺序归并。
- 默认 workers=1；并行只在 case/N 层。整个批次只校验一次约 469 MiB 的单核归档，再把对应记录传给 worker。模拟核数与实际 CPU 进程数分开。
- 从 `q1.run` 原样抽出 `normalized`、`atomic_text`、`write_json`、`file_hash` 到 `common.artifacts`，保留原导入兼容性；Q1 来源记录补入该共享文件。Q1 算法、参数和官方程序没有改动。

快速模型支持原始二部图、M/V 计算、每个本地逻辑张量至多一个计算生产者及标准单输入/单输出 DDR↔L1/UB COPY。不支持的输入明确报告 `UNSUPPORTED_INPUT_MODEL`；没有把近似结果标成完整支持。

## 3. 运行

从仓库根目录执行，`python` 应指向 Python 3.12。无需安装第三方依赖；本轮显式使用现有 3.12.14 解释器，没有修改全局 PATH。

```powershell
# 单例；输入也可以放在仓库外，路径含空格时加引号
python -X utf8 src/solve_problem2.py solve official/data/case_001.json --cores 2 --output-dir outputs/q2_single

# 有限冒烟：90 秒预算 / 15 秒复核预留 / 6 个组合
python -X utf8 src/solve_problem2.py batch --data-dir official/data --cases case_001 case_067 case_086 --cores 2 5 --workers 1 --wall-seconds 90 --verification-reserve 15 --output-dir outputs/q2_smoke

# 同一来源、同一参数、同一输出根目录续跑；成功组合只读复用
python -X utf8 src/solve_problem2.py batch --data-dir official/data --cases case_001 case_067 case_086 --cores 2 5 --workers 1 --wall-seconds 90 --verification-reserve 15 --output-dir outputs/q2_smoke --resume

# 独立复核已有方案，另存输出
python -X utf8 src/solve_problem2.py verify official/data/case_001.json outputs/q2_single/case_001/N2/plan.json --output-dir outputs/q2_replay

# 自定义小图：不引用固定百例基线，不报告加速比
python -X utf8 src/solve_problem2.py solve tests/fixtures/q2/minimal.json --cores 2 --without-baseline --output-dir outputs/q2_minimal
```

`batch` 若省略 `--cases` 会枚举输入目录的全部 `case_*.json`，默认核数为 2/3/4/5。1.0 的六组合冒烟显式限定了三个 case 和两种核数；1.1 的有限对照集合见[自适应 β 验证](adaptive_beta.md)。

`solve` 和 `batch` 支持 `--beta-search adaptive|fixed|grid`；未指定时使用配置，默认 `adaptive`。消融必须使用独立输出目录。新版本改变源码和算法配置指纹，不能续用旧版成功目录。

`solve --cores 1` 只供内部诊断；正式多核统计用 N=2/3/4/5，正文的固定单核点仍为 1，不使用 Q2 优化过的 N=1 方案替换单核分子或正文单核点。

默认配置为每 case/N 600 秒、复核预留 60 秒，官方评价耗时会用于动态增大预留。构造与 Step1/DP 使用协作式检查，官方评价使用真正可终止并回收的独立子进程；这里没有把线程 Future 超时当成停止计算。已审官方 CLI 不再派生子进程，终止范围限于本次创建的评价进程。

预算不是全调用硬截止承诺：某次同步建图、前缀构造或落盘可能跨过检查点。`run.wall_seconds` 从单组合图读取前开始，包含求解、最终复评和来源复核，止于报告生成；批次预先校验单核归档、初始来源哈希和随后报告发布的少量开销不在其中。`stages.official_seconds` 包含最终复评，与 `verification_seconds` 有重叠，不能把各阶段列直接相加当总时长。

## 4. 输出与复现

```text
outputs/<实验>/
  summary.json / summary.csv
  case_001/N2/
    plan.json / result.json / trace.json / result.log
    baseline_plan.json / baseline_result.json
    run.json / provenance.json / windows.jsonl / complete.json
    progress.json / eval_000/ / eval_001/ ...
```

`plan.json` 仅含官方要求的 `node_to_subgraph` 与 `core_schedules`。`result.json` 是最终独立复评的完整结果。`run.json` 记录候选指标、去重、试放与 ready 扫描计数、短名单轮数/总数/最大值、复制元素计数、C1 证书次数/时间、窗口状态/转移/事件计数、B0/B1 指标、真实官方调用数、阶段时间、峰值内存与停止原因。预算中断的构造计数和耗时单独保存在 `interrupted_construction`，不把部分方案列为可评候选。`plan_serialization_seconds` 包含 B0 方案/结果写入及最终方案写入重读，不包括每个官方子进程的内部 I/O；其时间同样可能与其他阶段交叉。

1.1 另记录 `beta_search`、`search.modes` 中逐模式已尝试 β、最好官方目标及停止原因，以及每候选的 `search_decision`、缓存来源和真实官方调用索引。CSV 追加策略、选中模式/β、候选数、初始化官方调用数和缓存命中数。`initialization_official_seconds` 属于 `official_seconds`，不能重复加总。

`provenance.json` 保存输入、固定配置、10 个官方程序、算法配置、实现源码、解释器及固定单核分子的指纹，以及 Git HEAD/工作区状态。本文的六组合冒烟在提交前的源码快照上执行，实际版本以各次运行记录的逐文件 SHA-256 为准；提交整理后的源码不追溯替换旧记录。`complete.json` 校验主要产物哈希，并要求 `SUCCESS_VERIFIED` 和最终复评标记。

失败或未完成尝试保留在 `case/.N*-attempt-*`，用于诊断；不覆盖旧成功目录。`--resume` 只复用完整且指纹匹配的结果，对未完成组合重新求解，不恢复半途 DP 栈。来源改变或成功产物损坏时拒绝覆盖，需新输出目录。墙钟中断位置受机器负载影响，跨机运行可能选择不同的已完成候选，不能只凭确定性并列规则声称限时结果完全一致。

RSS 分别记录求解进程的历史峰值和官方子进程采样的峰值；串行批次中的求解进程峰值可能来自较早 case。它们不是同一时刻的总和，也不是整机或整个进程树的统一峰值。未设置系统级内存上限；可识别的 Python `MemoryError` 记为 `RESOURCE_LIMIT`，不凭任意退出码推测 OOM。

## 5. 历史 1.0 已运行验证

验收源代码为 [`tests/test_q2.py`](../../tests/test_q2.py)，附四组小图/方案 fixture。实际执行命令如下；PowerShell 的 `PYTHONUTF8=1` 同时覆盖旧 Q1 测试启动的子解释器。

```powershell
$env:PYTHONUTF8='1'
python -X utf8 -m unittest discover -s tests -p test_q2.py -v
python -X utf8 -m unittest discover -s tests -v
python -X utf8 tools/benchmark_q2_trial.py official/data/case_001.json --output outputs/q2_trial_benchmark.json
```

已覆盖的重点包括：官方路径/哈希混用拒绝；四组前缀接回原 Step2/3 的图、序列、Pipe、链接与流量差分；纯试放对慢 deepcopy 参考的状态逐步对照；多核心映射的搬运公式穷举；随机小图闭区间证书对 Step2；m≤6 窗口与独立官方排列 oracle 对照；m=8 状态规模；动态 COPY_IN、外部路径和数据/顺序重合边；不完整 DP 不应用；B0/B1 严格择优；后备方案；预算中断计数；最终复评失败不发布；真实子进程超时；workers=2 的独立组合与失败隔离；续跑、指纹变化和产物损坏拒绝。

控制例 `memory_interleaved` 的 DP 实际找到了更好顺序，官方值由 3736 降到 2302 cycles，Spill 额外字节由 80000 降到 0；原序无换出 UB 峰值 140000，重排后为 100000。这验证该机制在控制例上生效，不代表真实百例的总体收益。

最终完整测试 **154 项，153 项通过、1 项跳过**，耗时 20.227 秒；其中 Q2 新增 **26 项全部通过**。唯一跳过项为原有 Q1 的“与根目录原始附件 ZIP 比较”测试，原因是此 worktree 根目录没有该 ZIP；Q2 的官方十个源码文件与配置仍由冻结清单逐字节核验。最终日志为本地 `outputs/q2_validation/tests_complete.log`，`git diff --check` 通过。

提交整理后使用 Python 3.12.14 再次运行完整测试，仍为 **154 项，153 项通过、同一项跳过**，耗时 37.473 秒；日志保存在本地 `outputs/q2_validation/pre_upload_tests.log`。本次只重跑现有测试，没有启动真实百例实验。

验收合同中的极端多输出/重复边组合、两个缓存峰值反向变化的专项构造、OS 强制 OOM、Linux 平台及全量性能尚未逐项穷尽；资源错误仅做了受控异常注入验证，不制造真实内存耗尽。不把有限测试描述为全输入正确性证明。

## 6. 历史 1.0 性能测量与实测记录

微基准固定 case_001 的 600 个计算操作、801 个逻辑张量，先接受 300 个操作，留下 100 个 ready；5 核，同一份 128 次试放列表，交替测量顺序，3 次重复。所有快慢参考的分数、工作量/字节、时钟、驻留与峰值一致；父状态不变。

| 测量项 | 增量试放 | 整状态 deepcopy 慢参考 |
|---|---:|---:|
| 128 次试放耗时中位数 | 0.001808 s | 0.307468 s |
| 单独启用 tracemalloc 的分配峰值 | 1904 bytes | 185160 bytes |

这只是固定状态的试放内核微基准，慢参考还会整图重算虚拟驻留；不等价于与优化过的另一个完整求解器比较。计时排除了建图、短名单、commit、DP 和官方评价，不能把约 170 倍比值写成整个算法的提速。可复现脚本为 [`tools/benchmark_q2_trial.py`](../../tools/benchmark_q2_trial.py)，最终源码下的记录保存在本地忽略目录 `outputs/q2_validation/trial_benchmark_final.json`；前一快照下的测量保留在 `trial_benchmark.json`，不混记源码。

真实冒烟为 Windows 11、Python 3.12.14、workers=1、90/15 秒预算。单核分子来自原有固定百例归档并通过输入/配置/官方代码校验。阶段瓶颈主要是候选试放及 ready 扫描；本次没有为缩短秒数改变短名单规则或官方评估语义。

本机实际数据目录为 `E:/Texdoc/Python/huaweicup2026npu/official/attachment/data`；输出为 `outputs/q2_validation/acceptance`，命令为上述六组合 batch 命令分别替换 `--data-dir` 和 `--output-dir`。完整用例名及参数已显式记录，没有按 case 名设置专用算法分支。

| case | N | 官方 makespan / cycles | 官方 added COPY / bytes | 最终方案 | 求解墙钟 / s | 完整官方调用数 |
|---|---:|---:|---:|---|---:|---:|
| case_001 | 2 | 116868 | 1152 | B0 | 3.062 | 2 |
| case_001 | 5 | 47502 | 4608 | B0 | 4.688 | 2 |
| case_067 | 2 | 32484854 | 170023168 | B0 | 41.172 | 2 |
| case_067 | 5 | 21724184 | 140557056 | B1 | 69.828 | 3 |
| case_086 | 2 | 111708 | 1354342 | B1 | 16.282 | 3 |
| case_086 | 5 | 108425 | 2175762 | B0 | 36.281 | 3 |

六组合均为 `SUCCESS_VERIFIED`，每个结果包含一次从落盘方案启动的独立官方复评；“完整调用数”计入该次复评。case_067/N5 构造阶段因 90 秒测试预算保留两次评价时间而中断，完成五个候选后使用其中有效方案；其余完成全部适用候选（单弱连通分量时不重复 grouped 模式）。因此本表不是默认 600 秒预算的算法成绩。

| case / N | 全部构造 / s | Step1 与候选证书 / s | 增强阶段 / s | 全部官方评价 / s | 试放次数（含中断部分） | DP 状态 / 转移 / 事件 |
|---|---:|---:|---:|---:|---:|---|
| 001 / 2 | 1.436 | 0.188 | 0.859 | 0.468 | 82232 | 8192 / 32768 / 327056 |
| 001 / 5 | 3.361 | 0.138 | 0.563 | 0.468 | 201560 | 8192 / 32768 / 173480 |
| 067 / 2 | 27.171 | 2.486 | 7.235 | 3.812 | 1755982 | 6797 / 26904 / 3161200 |
| 067 / 5 | 58.170 | 1.563 | 3.156 | 6.438 | 3344595 | 708 / 1355 / 65110 |
| 086 / 2 | 9.515 | 0.734 | 1.891 | 3.844 | 672912 | 503 / 788 / 28500 |
| 086 / 5 | 29.282 | 0.859 | 2.047 | 3.812 | 1641895 | 753 / 1509 / 55420 |

每组合均检查了 32 个窗口。case_067/N5 接受一个窗口，官方 makespan 从 21783641 降到 21724184；case_086/N2 接受一个窗口，从 111721 降到 111708。case_086/N5 的三个局部峰值改善窗口使 B1 官方耗时变为 108941，大于 B0 的 108425，因此最终选择 B0。求解进程的历史峰值为 111.0–120.8 MiB，单个官方子进程峰值为 25.0–160.1 MiB；不能据此直接设置多进程数或外推最大百例内存。

全部主要产物哈希已再次核对，明细在本地 `outputs/q2_validation/review.json`。验收后清除了 `common/artifacts.py` 文件尾多余空行，AST 完全一致；原实测产物保留执行时指纹，没有改标成新哈希。随后用该快照重新运行最小例，独立复评仍为 6 cycles，记录在 `outputs/q2_validation/post_format`。提交整理还按照仓库约定将本次新增或修改的文本统一为 LF 行尾，这同样可能改变逐文件指纹。严格指纹续跑不会跨越源码字节变化，新实验应使用新输出目录。

未开展百例完整成绩、默认 600 秒预算比较、多进程大图压力或服务器跨平台测试。下一次扩展实验应保持固定数据和官方版本，另建实验目录，先按实际 CPU/内存配额选择 workers，再报告覆盖数与失败原因。
