# Q3v3：Q1/Q2 完整方案、容量重排与搬运折衷

当前修订：`q1q2-memory-traffic-1`。入口为 `src/solve_q3v3.py`，默认命令行目标改为 `cycles-traffic`。官方程序不变，统一算法不读取 case 编号来决定策略，也不使用单核成绩作为图特征。

本版在 `q1struct-l2-1` 的全局构造、联合分核、结构候选和缓存窗口之上，增加完整 Q2 起点、私有存储容量模型、容量窗口重排以及可控制的时间/搬运折衷。所有继承方案和新方案都按问题3重新评估。

## 1. 为什么不再只按 cycles、命中率作字典序选择

此前一个周期的改善，就能压过任意规模的 spill 和 DDR 增长。并且，只看命中率可能鼓励保留本可消除的重复读取：消除 100 次无用读取以后，命中次数和命中率都可能下降，但实际 DDR 与总时间更好。

因此本版同时保存两个交付方案：

- `plan.json`：用户指定权重下的折衷方案，周期受硬上限保护。
- `fastest/plan.json`：本轮全部已评估可行方案中周期最小的方案。

两者分别通过独立官方问题3 CLI 重放，保存完整 `result.json`、`trace.json`、`official.log`、`replay.json`。即使两者相同，也分别验证，不能以搜索代理分数替代官方成绩。

## 2. 目标函数与权重的含义

记官方结果中的：

\[
T=\mathrm{makespan},\quad
A=P+S=\mathrm{added\_copy\_bytes},\quad
D=\mathrm{scheduled\_copy\_bytes}-\mathrm{cache\_hit\_bytes}.
\]

其中，`P` 是跨核划分产生的额外 COPY，`S` 是 spill 插入的额外 COPY，`D` 是实际经过 DDR 的搬运字节。问题3同核的优先级桶并非问题1的独立 Task；改变同核桶边界本身不必新增 COPY。

先按 Q3 官方规则评价全部输入起点，取周期最快、相同时 DDR 最少的起点作为固定参考。`B` 为原图 COPY 字节：

\[
T_r=\max(1,T_{\text{seed-fast}}),\quad
D_r=\max(1,B,D_{\text{seed-fast}}),\quad
A_r=\max(1,B,A_{\text{seed-fast}}).
\]

整个搜索中，这三个归一化分母保持不变。默认目标为：

\[
J(P)=\frac{T(P)}{T_r}
+\lambda\left[(1-\eta)\frac{D(P)}{D_r}
+\eta\frac{A(P)}{A_r}\right],\quad
\lambda=0.05,\ \eta=0.25.
\]

同时限制最终选择：

\[
T(P)\le(1+\varepsilon)T_{\min},\qquad \varepsilon=0.01,
\]

`T_min` 是截至当前所有已评估可行方案的最小周期，发现更快方案后自动收紧。最后选取满足上限且 `J` 最小的方案；同分时依次比较周期、DDR、额外 COPY、字节命中率，最终用完整状态摘要稳定打破平局。计算采用 `Fraction`，避免浮点边界误判。

例如，时间相对固定参考增加 0.2%，而加权归一化搬运下降 10%，目标变化为 `0.002 - 0.05 × 0.10 = -0.003`，可接受；若时间超过硬上限，则即使搬运大幅下降也不能交付。

参数独立控制偏好：

|参数|作用|默认|
|---|---|---:|
|`--cycle-tolerance`|允许比已发现最快方案慢多少|0.01|
|`--traffic-weight`|降低搬运相对降低周期的重要程度|0.05|
|`--added-share`|搬运项中额外 COPY 的占比，其余为实际 DDR|0.25|

这些参数不是官方评分公式，也不声称 0.05 对所有图最优。本轮参数在批次前固定，20 组统一使用。报告附带对已评估候选集合的权重敏感性重选；这不等于不同权重下重新搜索。

Python `solve_one()` 的历史默认仍保留 `cycles-hit`，避免旧调用方静默改变含义。直接调用新流程必须显式传 `objective_policy='cycles-traffic'`。命令行默认使用新流程。

## 3. 从原图和完整 JSON 到最终交付

```text
原图、核数 N、官方配置、Q1/Q2/可选旧 Q3 完整方案
  → 结构与覆盖检查；核对图、配置、方案和官方源码哈希
  → 各起点在官方 Q3 下重新评价，或验证本次预备阶段的完整重放
  → 固定归一化参考；建立 Pareto 档案、最快方案、折衷方案
  → 固定分核的完整方案顺序投影
  → L1/UB 容量生命周期与固定窗口重排
  → 成本排序的跨核读窗口及既有局部/联合/结构/全局构造
  → 每个完整候选接受官方 Q3 事件模拟
  → 更新 Pareto、探索队列、最快值和 1% 最终选择上限
  → 搜索预算耗尽或候选耗尽
  → 分别独立重放折衷方案与最快方案
  → 输出两份方案、逐项指标、候选履历、权重和复核文件
```

### 3.1 起点继承

`--warm-start` 读取完整 Q1 JSON，`--q2-warm-start` 读取完整 Q2 JSON，`--reference-start` 可加入之前 Q3 的完整方案。输入均为官方两字段方案；不读取 Q1/Q2 的中间候选、搜索缓存或原场景分数，也不重新运行 Q1/Q2 求解。

`PlanState` 对覆盖、依赖、提交格式、调度核数进行检查。摘要保留完整节点映射及其插入顺序、子图编号、每核队列顺序，不能只按节点分区去重。相同完整方案出现在多个来源时共享评价，但在继承记录中保留各来源角色。

沒有完整输入方案时，才调用已有 Q1 纯图候选接口；显式 `--seed-policy portfolio` 也可导出图候选。该路径保留 Q1 源码快照和 provenance，不调用 Q1 官方单核打分。主路径有完整 Q1/Q2 起点时不依赖其运行时代码。

本次对照额外把旧 Q3 方案作为保底候选，避免把旧成果丢掉。报告区分：输入方案收益、本轮新增搜索收益、从旧 Q3 到新折衷方案的整体变化。

### 3.2 固定原始操作分核，重建核内顺序

`memory_search.portfolio_orders()` 从 Q2 等完整方案提取合法操作优先顺序，投影到当前方案的原始操作分核上，再编码为可提交的桶顺序。它组合已有方案的分核和顺序，并非简单选择两个完整方案中分数较高的一个。

由于官方会重新生成 COPY 所属桶及核内实际执行序列，这个候选必须完整重评。顺序投影不承诺缓存一定改善，错误的投影同样可能增加 spill。

### 3.3 容量约束下的窗口重排

1. `scene_b_prefix.py` 构造与官方问题3一致的 Scene B 子图、COPY、Step1 顺序及桶优先级。只读调用官方函数，不修改其实现。
2. `memory_lifetimes.py` 建立 L1/UB 张量生命周期。使用分配先于释放的闭区间口径，记录各步占用、峰值位置和第一次超容量位置。
3. `MemoryService` 对完整方案摘要建立小型 LRU。对每核运行官方 Step2，计算真实 spill 字节，再核对父方案官方结果中的切分 COPY 与 spill 分解。
4. 优先处理 spill 较重的核心，在首次溢出和峰值附近选择 16、64、256、1024 个优先级桶组成的窗口。窗口最多分成 8 个相邻宏块，宏块可以包含多个桶，宏块内部顺序固定。
5. `CapacityWindow` 构建宏块依赖，包括跨核依赖和窗口外锚点；COPY_IN 按重新排序后的首个消费者动态归属，不能把 COPY 固定在旧桶。
6. 子集 DP 使用最多 `2^8` 个状态寻找 L1/UB 容量可行、顺序扰动最小的宏块顺序。每次扩展检查当前张量存活和操作瞬时峰值。
7. 若固定窗口内没有无 spill 可行排序，采用最小化超容量暴露量的压力 DP 生成提案。此时不声称得到零 spill 方案；仍由官方 Step2 插入 spill。
8. 重新生成候选前缀，检查实际操作顺序与 DP 预测一致、分核不变、跨核链接不变、其他核心不受意外影响。只有预计真实 spill 减少的窗口才送完整官方模拟。
9. 多个核心的独立窗口修复可以联合成一个候选，再用官方事件模拟检查组合后的效果。

这里的“精确”只覆盖固定窗口、固定分核和宏块内部固定顺序的搜索域，不是整个图的全局最优。具有合法 Q3 语义、但不满足窗口安全锚点假设的方案，可以跳过该窗口并继续其他提案。

### 3.4 分别记录并惩罚切分、spill 和 FIFO 重读

`traffic_model.py` 将官方缓存事件的 miss 分类为首次读取、仍在传输的重复读取、FIFO 驱逐后重读及其他；hit 单列。FIFO 驱逐 episode 在重新插入时结束，后续 miss 不能永远归入旧驱逐事件。

令 `F` 为 FIFO 重读字节，`M` 为总 miss 字节，默认提案惩罚权重为：

\[
w_P=1+P/\max(1,A),\quad
w_S=1+2S/\max(1,A),\quad
w_F=1+2F/\max(1,M).
\]

容量提案排序使用 `(wP × P + wS × S + wF × F估计) / DDR带宽`。父方案 FIFO 密度按候选 spill 变化作估计，仅为提案排序信号，最终 FIFO 数据必须重新模拟。

本轮这些分项惩罚用于新增容量窗口和读窗口提案，原有全局/结构家族仍采用原先构造代理，然后由统一官方目标筛选。没有把所有结构代理替换成精确的内存模拟。

`F` 与 `P/S` 对应的 COPY 存在重叠，不能把 `P+S+F` 声称为实际总搬运。最终交付分数使用 `D` 和 `A` 两个明确指标；分项原因用于诊断与构造。

### 3.5 跨核 L2 读取窗口

`cache_windows.window_candidates()` 根据真实缓存事件找到重复在途读取及驱逐后的重读机会，估计把 DDR 读取转成 L2 命中的节省，加入等待间隔和结束时间位置的成本提示，优先处理收益较大的读取窗口。一次协调 3、8 或 16 个窗口，保留已有单窗口提案作为后续补充。

修改仍限制在合法桶顺序与已有操作的优先级，不添加虚假读取，不改变算子，不向官方提交人为等待指令。旧时间线只用于排序，不能当作变更后精确的关键路径余量。官方模拟会重新计算 FIFO 插入、命中、跨核等待和资源冲突。

### 3.6 有限搜索与异常收尾

候选生成器以完整来源方案及家族为键持续推进。探索队列保留折衷最优、最快、最少 spill、最少 DDR 等不同方案，可以保留不满足最终 1% 上限的中间解。最终选择只能从满足上限的候选中进行。

内存、局部、缓存、联合、结构、全局候选交替获得预算。所有最终接受依据均来自官方问题3模拟。超时或官方拒绝只淘汰当前新候选；无效输入种子则报错。搜索期限到达后仍必须重放已保存的最优方案，不能因最后一个候选超时而丢掉整个用例。

## 4. 模块与接口

|文件|职责/主要接口|
|---|---|
|`src/solve_q3v3.py`|完整方案目录解析、主机并发、参数校验、Q1 可选快照|
|`src/q3v3/search.py`|`solve_one()` 根据目标分发；保留旧 cycles-hit/copy 流程|
|`src/q3v3/traffic_search.py`|`solve_traffic()`：三源起点、预算、候选档案、双方案交付|
|`src/q3v3/traffic_objective.py`|`TrafficPolicy`、精确目标、动态周期上限、Pareto 和探索队列|
|`src/q3v3/scene_b_prefix.py`|`build_scene_b_prefix()`：与官方一致的核内前缀|
|`src/q3v3/memory_lifetimes.py`|生命周期、L1/UB 预测峰值和溢出位置|
|`src/q3v3/capacity_window.py`|`CapacityWindow`、`minimum_disruption()`、`apply_window()`|
|`src/q3v3/memory_search.py`|容量缓存、顺序投影、压力 DP、容量候选|
|`src/q3v3/traffic_model.py`|缓存事件分类、三类成本和提案惩罚|
|`src/q3v3/cache_windows.py`|成本排序的多读取窗口候选|
|`src/report_q3v3.py`|按运行记录目标审计，重算全部候选选择及双方案重放一致性|
|`src/benchmark_q3v3_memory_traffic.py`|冻结 20 组运行、源码快照、20 主机进程与实际 PID/亲和记录|
|`src/report_q3v3_memory_traffic.py`|Q2/Q3 对照、继承收益、搬运、命中、用时和权重敏感性|

容量前缀/生命周期/窗口三个模块适配自 Q2-Optimized-3.0 的提交 `29747d80dac80a45a7902af35e2aa58b526ebbf6`，原始文件哈希见 [来源记录](q3v3_memory_traffic_sources.md)。它们已成为 Q3 本地模块，不在运行时导入另一个 Q2 工作区。官方目录未修改。

## 5. 运行方式与文件

本次仅发布代码、测试和说明，不上传数据或实验归档。下面的一般CLI命令可处理自备输入；`outputs/` 路径记录原本机实验，只有保留相应本地产物时才能按原路径重跑。克隆仓库后请自行准备赛题原图和完整Q1/Q2方案。

单图的完整 Q1/Q2 方案组合：

```powershell
.\.venv\Scripts\python.exe src/solve_q3v3.py official/data/case_014.json --cores 3 --warm-start <Q1完整方案.json> --q2-warm-start <Q2完整方案.json> --reference-start <旧Q3完整方案.json> --objective cycles-traffic --cycle-tolerance 0.01 --traffic-weight 0.05 --added-share 0.25 --seconds 120 --max-evaluations 64 --evaluation-timeout 600 --workers 1 --output-dir outputs/q3v3_memory_traffic_new
```

传入目录时可并行跑多个 case：`--workers 20` 表示主机 20 个进程；`--cores 2 3 4 5` 表示模拟的 NPU 核数，各 case 内不同 N 按顺序求解。目录需要覆盖每个请求的 case/N，缺失不静默回退。

重跑本次冻结 20 组并复用已核验的起点重放：

```powershell
.\.venv\Scripts\python.exe src/benchmark_q3v3_memory_traffic.py --prepared-seeds outputs/q3v3_q1q2_memory_traffic20_20260927/prepared_seeds.json --output-dir outputs/q3v3_memory_traffic_repeat20 --workers 20 --seconds 120 --max-evaluations 64
.\.venv\Scripts\python.exe src/report_q3v3_memory_traffic.py outputs/q3v3_memory_traffic_repeat20
```

预备重放在本次修改期间完成，记录 60 份方案的完整官方轨迹和图/方案/配置/官方源码指纹。复用时重新核验这些指纹和轨迹内容。一般 CLI 不传预备结果，会直接重新评价起点。历史文件缺失时可使用一般 CLI，不应伪造缓存成绩。

每例输出包含：`plan.json`、`fastest/`、`verified/`、`history.json`、`summary.json`、`seed_results.json`、`policy.json`、`pareto.json`、`memory_profiles.json`、`cache_analysis.json`，并保留每个已评价候选的完整官方结果。`summary` 明确记录输入种子路径、哈希、Q1 桥接来源、搜索用时与最终重放用时。

## 6. 问题3的单核结果如何取得

题面第6页要求问题3给出 1～5 核的无 L2 / 只读 Cache 对照。这个单核点不是问题1/2图中按定义固定为 1 的单核加速比点，也不能从多核结果按比例换算。

取得基础单核结果无需 Q1v3 先提供单核优化解：

1. 官方 `official/code/singlecore_evaluate.py::build_singlecore_plan()` 可以把全部非 COPY 操作放进一个子图、一个核心，得到合法完整 JSON。
2. 将该 JSON 交给官方 `multicore_cut_evaluate_problem_2.py`，得到场景 B、无 L2 的单核时间。
3. 将相同 JSON 交给官方 `multicore_cut_evaluate_problem_3.py`，得到场景 B、有 L2 的单核时间、命中率和搬运量。
4. 同方案单核 Cache 加速比为 `T_B_noL2(1) / T_B_L2(1)`，不是人为设为 1。

这是获取基础对照的方法。继续优化单核时，可以把这份 JSON 作为 Q3 的 N=1 完整起点，优化核内顺序、L1/UB 压力、spill 和 FIFO 重读。N=1 没有跨核通信窗口，仍可能有容量与重复读问题。优化结果应另列，避免把改变调度的收益全归给 L2 硬件。

目前 CLI 支持“有完整单核 JSON”的 N=1 搜索，但尚未自动调用上述构造器来生成单核起点。当前引用的 Q2 发布包 `plans.zip` 共 400 份方案，只有 N=2/3/4/5。本次20组也只覆盖这四种核数，**尚不构成题目要求的完整1～5核实验**；报告中没有单核实测值。这里说明了取得方法，没有虚构已完成的单核结果。

问题1固定单核评估器自身调用 Scene A，不会启用问题3 L2。其时间即使与某份场景 B 无 L2 单核方案恰好相同，也不能当作已测出的有 L2 单核时间。缓存基线和完整方案必须按实际场景核验。

## 7. 本次验证与结果入口

81 项 Q3 回归测试通过，包含真实容量窗口减少 spill、跨核 COPY 前缀与官方一致、准备种子哈希失效、候选子进程超时保留正确交付、完整种子继承及双方案独立重放等验证。

有效本地实验目录为 `outputs/q3v3_q1q2_memory_traffic20_r2_20260927/`；完整数据与日志不随本次提交上传。主要指标和发布验证见[验证说明](q3v3_validation.md)。本地 `run_info.json` 为 `complete`，`comparison.json` 为 `passed`；首轮异常目录保留用于追溯，不混入有效批次。

Q2 比值使用固定已发布方案在问题2的周期 `T2` 作分子，新 Q3 方案在问题3的周期 `T3` 作分母；另列同一个 Q2 JSON 直接移植到 Q3 的对照。没有把同图求和、不同核数均值或加权内部目标当成官方统一总分。

有限预算下的收益不证明找到全局最优。特别是容量窗口的范围、跨核窗口的时间估计、20 路竞争导致的搜索次数差异，以及多源继承收益，都需要在解读结果时保留。
