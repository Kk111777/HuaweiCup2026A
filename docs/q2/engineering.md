# 第二问工程准备与官方语义审计

审计日期：2026-09-26。本文保留**工程准备阶段**的源码审计记录；当时尚未实现 Q2 求解器、未执行 Q2 调度评估或搜索。后续已完成的算法程序、命令和有限验证见[实现与验收](baseline.md)。下文区分题面要求、当时官方实现、可复用工程设施及建议；源码行号以审计时基线提交为准。

## 1. 工作区与材料身份

- 团队仓库：`Kk111777/HuaweiCup2026A`。本次 `git ls-remote`、`git fetch origin main` 确认主线为 `eaf0d4ad6adfaab504b0623ffdd5650ed62c35e0`，包含 PR #7 的 Q1 V2 合并。
- 本地任务分支：`feat/Zysishuiyears/q2-engineering-prep`，从上述 `origin/main` 新建；工作区为 `C:/Users/10433/.codex/worktrees/b2f0/huaweicup2026npu`。不复用已 Squash 合并的 V2 分支。
- 外层交接：`E:/Texdoc/Python/huaweicup2026npu/docs/q2_engineering_handoff.md`。其中“外层 HEAD 尚无提交”是交接时记录；本次 Git 的 common-dir 为外层 `.git`，worktree 注册表已显示外层 `main=eaf0d4a`。本文以新工作区及实时远端核验为准，未切换、重置、清理外层工作树，也未处理其未提交文件。
- 原始题面及附件位于 `E:/Texdoc/Python/huaweicup2026npu/official/`。本次核对 PDF、DOCX、ZIP 的 SHA-256 与 `manifest.json` 一致；读取完整 PDF 文本，并渲染核对第 6 页的问题二正文。
- 仓库 `official/` 的 10 个 Python 文件、2 个说明文件、README、配置共 **14 份文件**，均与外层 `official/attachment/` 和原 ZIP 对应文件逐字节一致。原附件 **100 例**输入与 ZIP 的 SHA-256 全部一致。输入保持原位置，后续通过显式路径读取，不复制另一套数据。
- 本次只读结构清点：100 例合计 699118 个 op，其中 634506 个非 COPY op；原始 Op→Op 边、非 COPY 的非 M/V op、具有多个非 COPY 生产者的 tensor 均为 0。这只确认这三项数据特征，不代替完整合法性或运行验证。

关键身份如下，完整逐文件记录保存在本地忽略目录 `outputs/q2_engineering_audit_20260926/materials.json`；该记录是材料审计证据，不是 Q2 成绩或完成标记。

| 材料 | SHA-256 |
| --- | --- |
| 原始题面 PDF | `0f9d7e2a004142951889cf19376840047180a1b46989fd610bde359cb81fb0fb` |
| 原始附件 ZIP | `3112331344df7dcf2dcef2ff5f789669c0419e32cefbbc6e2742b9e36d5c43c9` |
| `official/code/multicore_cut_evaluate_problem_2.py` | `0b39f84d5ec0a7fba9a4c92a598a9044b97ab79c71393824c1ba130ecfe6c464` |
| `official/data/config.txt` | `dcd10de54b23f8366428fb24e828812b1da9549e6eae4a3c3f38604fe5ae77b9` |

工程基础版本仍是 Python 3.12。当前终端默认 `python` 为 `E:/anaconda3/python.exe`，版本 3.13.5，本轮仅用于材料哈希、结构清点和已有归档读取。已找到并执行版本探测的 3.12.14 解释器：`C:/Users/10433/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。后续执行时显式指定解释器，不改变全局 PATH；本轮没有新建环境或安装依赖。

## 2. 题面约束与官方代码对应

题面依据：PDF 第 2–6 页的 1.2–1.5 节及问题二，第 9–10 页的附录 B，第 12–14 页的附录 C/D。目标是对全部用例、固定配置、N=2/3/4/5 生成可执行方案，主要缩短总 makespan，兼顾总额外搬运。正文报告 1–5 核逐例加速比的算术平均，单核固定为 1；附录列逐例 makespan、额外搬运。题面建议避免暴力迭代搜索、单例约 5–10 分钟，并非评估器自动实施的墙钟截止。

| 约束或语义 | 当前实现及源码位置 | 工程含义 |
| --- | --- | --- |
| 方案只有两字段；每个非 COPY op 恰好属于一个子图 | [derive_multicore_plan](../../official/code/stub_multicore_cut_and_schedule.py)，114–175 行；`node_to_subgraph`、`core_schedules` | 不把 tensor、COPY_IN、COPY_OUT 放入映射；保留空核心列表；核数由外层列表长度确定 |
| 原图和子图收缩图均无环，同核直接子图依赖顺序合法 | [validate_graph](../../official/code/evaluation_validation.py)，171–215 行；`derive_multicore_plan`，188–231 行 | 原图 DAG 不自动保证任意分区的商图为 DAG；基础方案合法不等于 Q2 可执行 |
| 每核全部子图合成一个 Task | [Q2 `_build_scene_b_tasks`](../../official/code/multicore_cut_evaluate_problem_2.py)，61–86、236–272 行 | Task 的单位是核心；`task_count=N`，包括空核心。需要单独记录子图数，不能把 Task 数当切分粒度 |
| 同核 tensor 依赖保留片上数据 | Q2，133–156、181–200 行 | 同核子图边界不额外插入 DDR COPY，但长期驻留可能导致 spill；不保证同核通信的总成本为零 |
| 图输入、图输出重建 | Q2，158–179 行 | 无非 COPY 生产者的输入，每个消费核心读入一次，按该核最早消费子图归属；最终输出按生产核心写回，归属最晚生产子图 |
| 跨核 tensor 通信 | Q2，181–207 行 | 对每个 tensor 的每个不同 source-core→target-core 组合插入一对 COPY；多个消费者在同一目标核共享该次输入，不按每条 op 依赖分别计费 |
| 跨核等待 | Q2，374–402 行；[配置](../../official/data/config.txt) `[multicore_scene_b]` | `COPY_IN` 最早释放时间为对应 `COPY_OUT` 完成时间加 500 cycles；本地前驱、Pipe 队首等条件仍可能继续推迟它 |
| 子图顺序参与核内调度 | Q2 `_prioritize_task_seq`，43–59、244–265 行 | 对 Step1 序列按子图顺序稳定分桶，桶内保序；若破坏核内拓扑直接报错，随后以此顺序执行 Step2 |
| L1/UB 容量及 spill | [Step2](../../official/code/schedule_step2.py)，101–107、157–173、253–284 行；[Step3](../../official/code/schedule_step3.py)，289–296、595–672 行 | Step2 选择非当前输入/输出、下次使用最晚的可换出对象；不足时失败。Step3 检查容量与生命周期，并把内存额度复用依赖写入执行图 |
| Pipe 固定顺序和全局合法性 | Step3，282–287、675–701 行；[validate_execution](../../official/code/evaluation_validation.py)，226–243 行 | 最终执行必须保留每条 Pipe 的 FIFO 和本地数据/内存依赖，再加入跨核 COPY 边检查全局环；不能只检查子图 DAG |
| 共享 DDR | Q2，329–371、477–486 行；Step3 `_op_duration/_uses_ddr_bandwidth`，74–92 行 | 全核 DDR 搬运共享 60 bytes/cycle；独占参考工作量按 `max(1, ceil(bytes/bandwidth))`，并发时公平分配并重新计算完成时刻 |
| 最终指标 | Q2，273–280、550–600 行 | 取全局 `makespan` 和 `data_movement_bytes.added_copy_bytes`；不能用 `step3_by_core.local_makespan`、单个子图 duration 或程序秒数代替 |

固定容量为 L1=524288、UB=131072 bytes；每核 MTE2、MTE3、M、V 四条 Pipe，各条同时仅一个在途 op。Q2 不使用 Q1 的同核 100 / 跨核 1000 cycles Task 等待，也不使用 Q3 的 L2 Cache。

### 2.1 子图优先序不等于整个子图串行屏障

分桶后的顺序决定各 Pipe 的投影顺序、内存申请次序及 spill 选择。最终模拟检查的是 op 级依赖与资源；代码没有为相邻子图添加“前一个全部完成，后一个才能开始”的统一屏障。不同 Pipe 上的子图操作在依赖、内存条件允许时可以交叠。因此既不能忽略 `core_schedules`，也不能把 Q1 的整 Task 串行关系原样施加给 Q2。

跨核等待仅限制目标 COPY_IN 的释放，不占用等待期间的 Pipe 或 DDR 带宽，也不要求源核心全部计算结束。以 `t_out` 表示源 COPY_OUT 实际完成时刻，则对应目标 COPY_IN 满足 `start >= t_out + 500`；等号是否成立还取决于 FIFO 和其他前驱。

### 2.2 搬运量和内存证据的边界

- `cross_task_traffic` 每个跨核连接只累计一次 tensor 大小；一对 COPY 的读写都计入 scheduled COPY 字节，因此该字段不是总 DDR 搬运量，也不是题面“总额外搬运量”。同一个生产核向多个目标核广播时，当前代码为每个目标核分别建立 COPY_OUT/COPY_IN 对。
- `added_copy_bytes = scheduled_copy_bytes - original_graph_copy_bytes`；其中 scheduled 还包含图输入输出和 spill。不能未经推导把 Q1 的子图边界字节加总用于 Q2。
- Step3 将可拆分内存额度的复用转换为 WAR/WAW 完成依赖；Q2 事件循环装载这些边，不重新选择 spill 或动态重排。`memory_peak_by_core` 直接取 Step3 记录，是这一官方构造的容量证据，不是另一次按全局时间线独立重演的内存峰值。
- 严格容量约束不等于“每个子图所有 tensor 大小总和不超容量”；生命周期和 spill 决定实际占用。启发式压力代理也不能单独证明方案不可行。

### 2.3 题面、说明文件与代码的差异

题面 1.2/B.3 只允许 Tensor↔Op 原始边；`validate_graph` 只禁止 Tensor→Tensor，Q2 209–234 行还处理原始 Op→Op（跨核时生成合成 UB tensor 与 COPY 对，缺省 `data_size=0` 仍有至少 1 cycle 的 COPY）。本轮 100 例没有这种原始扩展边。正式模型与输入沿用题面；原始扩展格式测试与官方内部生成的 MEMORY_REUSE Op→Op 边须分开表述。

`official/docs/核内调度算法.md` 的开头和 0.4 节仍有将“题目1/2/3”分别对应存储峰值、spill、流水排布的旧称呼，与本次多核题面及三个评估入口不一致。它的核内步骤细节可参考，但题号与入口以 PDF、`official/README.md`、当前代码为准；不改动官方材料。

## 3. 复用判断：数据结构复用，场景假设隔离

| 现有模块 | 可复用部分 | 不能直接搬用的部分 |
| --- | --- | --- |
| [q1/model.py](../../src/q1/model.py) `Graph/topological/components` | 原图、任意非负 ID、op/tensor 关联、收缩 COPY 后的 DAG、确定性拓扑与弱分量索引 | `reference_pressure` 是无 spill 参考序代理；`features` 不替代 Q2 可行性与精确代价。收缩 COPY 的候选依赖也不能未经核实当作最终执行图 |
| [q1/graph.py](../../src/q1/graph.py) `GraphIndex` | 在明确 M/V、单生产者假设下复用静态索引和分量信息，本次数据清点满足这两项 | `single_q/single_d/pressure` 带有 Q1 边界估价假设，不能视为 Q2 严格界；零周期启发式与官方至少 1 cycle 的口径也要区分 |
| [q1/plan.py](../../src/q1/plan.py) | `quotient` 的分区覆盖/商图 DAG，`plan_digest`，makespan/搬运字段提取 | `validate_plan` 把全部同核相邻子图加入串行边后检查环，是 Q1 规则，不能作为 Q2 的等价判定器；词典序接受是算法选择，不是题面唯一目标标量化 |
| 官方 `evaluation_validation`、`derive_multicore_plan` | 原图/参数/最小方案校验直接用原版；Q2 最终入口继续负责新引入的核内/全局检查 | 基础校验通过不能跳过 Step2/3 与 `validate_execution` |
| [q1/official.py](../../src/q1/official.py) | 原版薄适配、JSON 读取、官方原函数调用的做法；简单方案格式工具 | `Settings.read/evaluate/preanalyze` 固定场景 A；`SCHEDULING_ERRORS` 也没有 SceneB 错误。不能只给 Q1 `evaluate` 改个别名 |
| [common/baseline.py](../../src/common/baseline.py) | **可直接复用**已有归档校验、`baseline_ref` 与 `calculate_speedup` | 它只校验分子的来源，不校验 Q2 分母；Q1 多核成绩不能当成 Q2 成绩 |
| [q1/run.py](../../src/q1/run.py) | `atomic_text/write_json/file_hash/normalized` 的纯产物操作及显式来源、逐例配对统计方式 | provenance、metrics、completion 依赖 Q1 算法名、beta、construction 和产物集合；不能整套当通用 runner。当前 Scene A/B Trace wrapper 虽同调 `_trace_json`，Q2 仍选明确的 B 入口 |
| [q1/stage2/run.py](../../src/q1/stage2/run.py) | 保存方案后独立原版复评、完成标记最后写入、全批一次验证基线、显式继承来源的流程 | `read_initial` 的归档格式、expected result、源码集合及 run_case 都固定 Q1；只可把 Q1 plan 作为注明来源、重新经 B 评估的候选 |
| [q1/stage2/evaluate.py](../../src/q1/stage2/evaluate.py) | 容量有界缓存、返回对象隔离、实际模拟/逻辑预算分别统计的原则 | 准备缓存只以 partition signature 为结构键，并允许仅改 core 后复用 Task；Q2 换核、重排均可能改变 COPY、spill、FIFO、内存补边，必须重新设计 |
| [q1/stage2/bounds.py](../../src/q1/stage2/bounds.py)、state/diagnose/search | 固定候选对照、停止原因、可定位剪枝证据的验证方式 | B0/B1、DDR 窗口与 partial 界使用 Q1 Task 边界、整 Task 等待及展开图，不能直接启用。状态、五原则诊断与邻域优先级同样需要 Q2 建模依据 |
| [q1/unified/budget.py](../../src/q1/unified/budget.py) | 真正需要硬超时时可复用仅管控本程序子进程树的执行设施 | V3 的分级时间政策和触发阈值不是 Q2 已确定政策；心跳及合作式预算检查本身不能中断同步官方调用 |

### 3.1 单核分子已核清

题面问题二明确沿用问题一的加速比定义；问题一要求固定启发式单核基准。官方 [singlecore_evaluate.py](../../official/code/singlecore_evaluate.py) 22–57 行将全部非 COPY op 放入同一子图、核心 0，再调用 Scene A 评估，是现有归档的入口。

本次调用现有 `load_baseline_catalog` 只读验证整个归档及 100 个请求输入：产物清单、汇总/结果/Trace/日志、配置、10 个官方模块和输入身份均通过。`case_001=233110`、`case_003=602212` cycles；归档 `manifest.sha256` 的 SHA-256 为 `54f10fc9c5d1631f72f5decdc51ea41458e6ea900ec3d4d26e422f20368be2ba`。没有重新计算这些成绩。

因此可以复用该归档作为 Q2 分子，分母必须来自同一输入和配置的 `evaluate_scene_b`。这不声称“任意 Q2 单核多子图方案与固定单核基准同分”，也没有证明 Scene A/B 任意方案等价。批量时校验一次 catalog，再向各 worker 传入单条记录。

### 3.2 Q2 的缓存与下界从最小可信范围开始

初版直接调用原版 Q2，不启用预处理模板缓存或 Q1 剪枝。以后完整结果缓存至少绑定题号/场景、输入、官方版本、配置、完整分区、各核有序子图列表、核数及评估参数。先保留原 sgid/op id，不擅自假设重命名等价；输出内还含这些 ID。

按核缓存必须进一步核对本核 op、全部相关 tensor 的源/目标核心集合、输入输出和 COPY 归属、子图顺序，以及跨核引用生成 ID 的一致性。仅有本核节点集合或仅有 partition signature 不够。可变模拟状态必须与共享模板分离；失败结果、缓存命中和淘汰各自测试。

未来若设计严格下界，应先说明针对哪张固定的 Q2 执行图、保留哪些必需依赖与工作量、放松哪些资源约束，再证明不超过官方 makespan。固定图内成立的界不自动成为“改分区/改顺序后”的分支界。小图枚举用于查反例和实现错误，不代替证明。

## 4. 第一问经验如何落实到第二问

V1 是 TaskGrowth 自适应 beta 基座；V2 是基座加严格剪枝多原则分层改进；V3 是从 V1 派生的基座优化路线。版本号不表示 V3 继承 V2。这里只复用已经辨明语义的模块，不复制三套工程。

| 第一问处理经验 | 第二问落实 |
| --- | --- |
| 加速或缓存会改变墙钟内的搜索位置 | 等价检查固定候选、顺序、逻辑预算、并列规则和冻结时钟；完整结果、接受历史与停止位置都比较。限时质量另作实验 |
| 实际模拟与逻辑预算不同 | 分别记录 `official_evaluations`、`budget_evaluations`、命中/淘汰；明确初始评价和最终复评是否计入，不能凭缓存命中扩大搜索域 |
| 缓存与复制优化需状态归属证据 | 先分阶段测热点，再决定缓存；保证返回对象隔离、容量边界、输入身份和失败后状态隔离 |
| 完成继承不等于搜索栈恢复 | 同身份完成结果可验证后复用；旧版本继承须有显式等价证据和迁移清单，保留原 producer 指纹。Q1 完成标记绝不作为 Q2 完成标记 |
| 当前 V2 checkpoint 仅保留接受方案及事件 | `--resume` 复用完整产物，没有恢复分支前沿、调用栈、预算和 RNG 的代码；从 checkpoint plan 重新开始时记录新 attempt、父方案与预算重置 |
| 心跳不证明时间截断 | 分别记录 phase、阶段耗时、最近接受位置、预算消耗、domain/overall stop_reason；同步官方调用若无监督进程，不宣称硬截止 |
| worker 数不等于模拟核数 | CLI/报告分开 `workers` 与 `num_cores`；上服务器后先核对本账户实际 cgroup CPU/内存配额、亲和性、峰值 RSS，再选并发。本次没有访问服务器 |
| 变化中的完成集合会改变均值 | 固定 case/N/基准进行配对；报告成功/请求数、改善分布及自然完成/截断/失败/继承。缺项不填 0，也不宣称完整平均成绩 |

本次还读取了原 `outputs/q1_model_v2_publish_20260926/tests.log` 和 `equivalence.json`：历史记录为 128 项测试、127 通过/1 跳过；32 张小图、192 次完整候选结果、576 次下界/阈值、22 项固定候选和 6 条冻结时钟路径对照通过。它们是原运行包的有限证据，本轮未重跑，也不是全量 Q1 成绩或任何 Q2 验证。现有 `configs/q1/stage2.json` 的 1800 秒总搜索、120 秒局部域等参数不直接作为 Q2 默认预算。

## 5. 下一步最小工程闭环（建议，尚未实施）

先完成“给定 plan → 官方 B 评估 → 落盘 → 重新读取 plan 独立复评 → 验证完成标记”，不预先选定第二问核心算法。

1. **入口只承担已确定职责。** 新增一个实际可运行的 Q2 入口，接收 graph、已有 plan、固定 config、期望核数和独立 output；不先创建一套空 q2 模块。初始方案可用题面最小例子；后续可读取标明来源的 Q1 plan，但必须按 B 重新判定合法性及成绩。
2. **复用官方流程。** 读取 `read_evaluation_config` 与 `read_scene_b_config`，调用原 `evaluate_scene_b`；原官方文件保持字节不变。参数校验、SceneB/Step2/Step3/全局环错误均明确失败，不补写成功成绩。通用图索引只在实际候选生成需要时接入。
3. **共享产物只做必要整理。** 复用已有纯 I/O/hash/归一化函数；若第二个实际调用方需要公共位置，再从 `q1/run.py` 原位抽出到一个 common 模块并保留 Q1 导入兼容，不复制另一份 runner。算法名、题号、产物清单由 Q2 明确提供；整理导致源码哈希改变时，不改写旧实验来源。
4. **结果和复评分开。** 持久化 plan 后，使用同一显式解释器启动原版 Q2 CLI，重新读取输入和配置，三个输出都放入新 output 下；比较除输入路径显示字段外的完整官方字段，特别是 timeline、COPY transfers、memory/Step3 统计，而不仅比较 makespan。复评失败不能写完成标记。
5. **首次完成只覆盖小闭环。** 最小图和必要语义用例通过后，再选少量真实输入做零搜索预算接入。检查成功结果与错误路径，不从零预算接入推断优化效果；随后再讨论切图/分核/顺序联合决策与搜索预算。

最小产物放在 `outputs/q2/<run>/<case>/N<n>/`：`plan.json`、完整 `result.json`、`result.log`、`trace.json`、`metrics.json`、`provenance.json`、复评证据及最后写入的 `complete.json`。失败保存本次错误与阶段，不复用旧成功标记；部分方案置于独立 attempt，不覆盖旧产物。不要为了填目录生成不存在的 optimization/history。

来源至少包含 `problem=2`、`scene=B`、case、N、输入/config/官方/实际求解及共享代码 SHA-256、Git HEAD 和 dirty 文件身份、解释器绝对路径/版本、参数/逻辑预算/随机种子（如使用）、初始 plan 来源与哈希、baseline_ref；完成标记覆盖必需产物哈希及复评结果。不同题号、输入、配置、源码、核数或预算不自动续用。

已有官方 CLI 可作为下一轮独立复评入口，以下仅展示接口，本轮未执行（`$graph/$plan/$out` 由调用方提供，`$out` 应为新实验目录）：

```powershell
& $python312 official/code/multicore_cut_evaluate_problem_2.py $graph $plan `
  --config official/data/config.txt `
  --output "$out/result.json" `
  --trace-output "$out/trace.json" `
  --log-output "$out/result.log"
```

必须显式给出三个输出路径，因为原 CLI 默认把结果写入输入图旁边。原 CLI 不生成工程 provenance、跨文件完整性标记或独立复评证据，这些才是最小入口需补上的职责。

### 5.1 最小验证范围

| 验证组 | 需验证的行为 |
| --- | --- |
| 正常语义 | 题面单 op 最小例（含空核）、同核跨子图复用、跨核 COPY 的 500-cycle 最早释放、多目标核通信、多个子图共享输入只按消费核读入 |
| 调度与容量 | 相同分区不同分核/顺序、无依赖的跨 Pipe 交叠、驻留压力触发 spill、容量失败、局部次序错误、全局 FIFO/COPY 等待环 |
| 接口边界 | 非连续 op/tensor/sgid、空核心、重复/缺失节点、原图环、商图环；原始 Op→Op 仅作为评估器扩展兼容测试，单独标明 |
| 可信产物 | 完整字段独立复评；变更 plan/input/config/官方/题号/源码身份后拒绝续用；缺文件、篡改或失败均不得生成有效完成标记 |
| 后续优化才需 | 缓存命中/淘汰与对象隔离、固定候选/逻辑预算等价、确定性并列规则、有限域枚举查错；性能按阶段计时且固定同机工作量 |

本轮新增本文和本地材料审计记录，并在架构文档加入入口链接；未修改算法、官方代码/配置或历史结果，没有提交、推送、PR、部署、服务器操作或全量实验。下一轮实现时再运行与实际代码改动相关的测试，本轮文档准备不重复运行全部 Q1 测试。
