# 问题二新基座算法：Codex 复现实施规范

版本：Q2-Baseline-Handoff-1.0。对象：2026 华为杯 A 题问题 2，场景 B，无 L2。

本文件是代码工程的实现合同，不是待再次自由设计的算法草案。复现目标为：**子图生长与核心选择联动 + 核心级持续状态 + 少量同核窗口精确缓存排序 + 官方全图评价保底**。不将问题一第二阶段的大规模迁移、边界重构和分支定界重新作为默认主循环。

## 0. 信息来源、固定内容和工程补充

算法内容来自本次对话最近确认的“问题2基座适配与低成本精确增强”方案；硬件、评估和提交行为以本次原始附件为准。参考版本 SHA-256 见 `official_source_manifest.json`，函数和源码范围见 `docs/OFFICIAL_API_AUDIT.md`。此前优秀论文只用于写作组织，不是今年缓存、输入格式或评估规则的来源。

下列是为了消除代码歧义而在本实施稿明确冻结的工程约定，而非新的理论结论：候选并列比较、构造候选的便宜排序、窗口选择顺序、超时返回状态、模块接口和输出目录。它们应在实现中显式配置或说明，不由 Codex 任意更换。若实际附件与已审版本不同，先报告差异，再修改适配器；不得悄悄修改官方脚本使自己的结果通过。

三个精度层次必须分开：

| 层次 | 计算内容 | 能声称的结论 |
|---|---|---|
| 构造参考模型 | 四 Pipe 预计时间、基础 COPY、虚拟缓存压力 | 有物理解释的启发式排序，不是官方时间上下界 |
| 精确缓存组件 | 真实 Step1 顺序的无换出驻留；固定窗口的最小归一化峰值 | 固定序列的无换出证书、限定窗口缓存目标最优 |
| 正式评价 | 不变的官方 Scene B 评估器 | 真正 Makespan、额外搬运量及执行可行性 |

**严格范围修正：**窗口动态规划优化的是 `max(L1_peak/L1_cap, UB_peak/UB_cap)` 这一标量。其下降不保证 L1 和 UB 两个峰值各自都不增加，更不保证 Makespan 下降。不能将它写成对双分量同时占优或对总体执行时间的精确求解。最终由保留的基础方案保护时间性能。

## 1. 不得改变的场景 B 语义

同一核心所有子图合为一个 Task；同核封闭一个子图不清空缓存，不产生 100 周期切换，也不触发强制 DDR 中转。跨核目的 COPY_IN 在其对应源 COPY_OUT 完成后再等待 500 周期。不得加入场景 A 的 100/1000 等待，不得启用问题3的只读 L2。

`core_schedules` 决定合并核图的子图分桶顺序；它不是每个子图整体完成的运行屏障。必须保留不同 Pipe 可重叠的实际规则。不得要求“核心间商图”无环：跨阶段 Core0→Core1→Core0 可能合法。最终运行可行性以扩展操作、Pipe 顺序、内存复用边与跨核 COPY 共同决定。

从官方 `config.txt` 读取：L1=524288 字节、UB=131072 字节、DDR=60 字节/周期、跨核 COPY 延迟=500 周期。算法配置文件不得覆盖它们。普通操作有效周期 `max(1, cycles)`；单张量基础 COPY 的参考工作量 `max(1, ceil(size / bandwidth))`。零字节搬运仍可能需要一个参考周期，但搬运字节数为零。

## 2. 输入输出与工程接口

### 2.1 输入和正式结果

输入为 UTF-8 原始 JSON：`tensors`、`ops`、`edges`。字段和大小写遵从原附件，而不是2025年的 `Nodes/Edges/BufId/ALLOC/FREE`。

正式方案仅包含：

```json
{"node_to_subgraph":{"11":0},"core_schedules":[[0],[]]}
```

映射键为原非 COPY 操作 id 的规范十进制字符串，值为非负整数。外层列表严格为请求核心数 N，允许空列表；使用过的每个 sgid 全局恰出现一次。原节点不能遗漏、重复计算、重新编号，不能把诊断、参数、峰值、指标写入这份 JSON。

结果目录建议：

```text
results/problem_2/N2/case_001_multicore_res.json
results/problem_2/N2/case_001_problem_2_res.json
results/problem_2/N2/case_001_problem_2_trace.json
results/problem_2/N2/case_001_problem_2_log.txt
results/problem_2/N2/case_001_run.json
results/problem_2/N2/case_001_construction.jsonl
results/problem_2/N2/case_001_windows.jsonl
```

其中后3项为研究日志，不是提交方案的字段。不同核数不得覆盖同一路径。基础方案 B0、增强尝试 B1 可保存到 case 独立工作目录，不修改原始 data/。

### 2.2 建议命令

```bash
python -m q2_baseline solve official/data/case_001.json -n 2 \
  --reference-root official --config official/data/config.txt \
  --algorithm-config configs/default.json --output-dir results/problem_2/N2

python -m q2_baseline batch --data-dir official/data --cores 2 3 4 5 \
  --reference-root official --algorithm-config configs/default.json \
  --output-dir results/problem_2 --workers 1

python -m q2_baseline verify official/data/case_001.json \
  results/problem_2/N2/case_001_multicore_res.json --reference-root official
```

正常正式实验 N∈{2,3,4,5}。N=1仅用于内部测试/兼容，不将自行优化后的单核方案作为题目规定的单核加速比分母。

### 2.3 最小模块划分

```text
q2_baseline/
  __init__.py
  __main__.py          # CLI、单例/批处理、状态码
  graph.py             # 原图解析、索引、静态量、候选范围
  model.py             # Plan、构造状态、TrialDelta、报告数据类
  construct.py         # 四短名单、种子、生长与提交
  proxy.py             # 便宜试放、精确基础通信、虚拟驻留
  official.py          # 唯一官方适配入口、版本校验、评价进程
  memory.py            # 真实Step1、生命周期、无换出证书
  window_dp.py         # 外部约束、动态COPY桶、子集DP、回溯
  pipeline.py          # 初始候选排序、B0/B1保底、输出
  report.py            # JSONL/汇总、加速比口径
configs/default.json
tests/
```

允许合并非常短的模块；不要建几十个抽象基类。核心数据类使用标准库 `dataclasses`，主算法优先标准库。测试依赖可使用 pytest。不要引入网络服务、训练模型、数据库、全局求解器或与本算法无关的插件。

## 3. 官方适配先行：真正昂贵的边界在哪里

本次源文件中：

```python
settings = read_evaluation_config(config_path)  # {'bandwidth':..., 'capacity':...}
delay = read_scene_b_config(config_path)['cross_core_copy_delay_cycles']
result = evaluate_scene_b(graph_json, plan_json,
    bandwidth=settings['bandwidth'], capacity=settings['capacity'],
    cross_core_copy_delay=delay)
```

`_build_scene_b_tasks` **已执行 Step1、Step2、Step3**，不是“只生成基础图”的便宜函数。默认构造/候选筛选不能为每个试放调用它。正式 `evaluate_scene_b` 又会调用它，先手动展开后再调用正式评价会重复展开，应计入成本。

为了只取得基础核图与Step1，本包提供 `tools/scene_b_prefix_reference.py`：静态复用官方 `_build_scene_b_tasks` 的任务构图部分，停在 Step1 后。它不修改官方文件，不在运行时 monkeypatch，不执行 Step2/3/D。适配器可导入或按其行为实现，但须保留来源和通过差分测试。

它返回每核 `graph, raw_seq, seq, op_subgraph, subgraph_order`，以及 `cross_links, base_copy_bytes, original_copy_bytes, plan_view`。`seq` 是 `_prioritize_task_seq` 后的真实 Step1 序列。

原图与计划的基本检查用 `derive_multicore_plan`。本算法还用子图依赖与同核顺序的统一无环关系界定构造/窗口安全域，但这不替代最终 `validate_execution` 和正式模拟。不存在本合同所称的 `_reorder_seq_by_subgraph` 等虚构官方API；真实名称见接口审阅文件。

## 4. 原图索引与支持范围

先保留原始输入的不可变快照，再构建内部索引。原 id 可很大、很稀疏，禁止按 `max(id)+1` 分配数组；按排序后原计算 id 建立稠密 index，最终必须映射回原 id。操作和张量各自保留独立类型，不把整数相同的跨文件 id 混为一个对象。

至少建立：

- `compute_ids`, `original_to_dense`, `dense_to_original`；
- `preds[v], succs[v]`：去重的计算直接前后继；
- `inputs[v], outputs[v]`：按原tensor id排序的唯一逻辑张量；
- `producer[t]`：计算生产者或None；`consumers[t]`：不同计算消费者；
- `size[t], pos[t], final_output[t]`，后者含官方“无计算消费者的产物也写出”的情况；
- `pipe[v], duration[v]=max(1,cycles)`；原始COPY总字节数由官方口径计算；
- 确定性计算拓扑序、弱连通分量、前向a/后向b、两类管道总工作量。

所有张量存储位置取 `pos`，不凭算子类型判断。计算前驱由生产—张量—消费关系生成；共享输入不生成操作先后边。多个张量造成同一操作前驱，只减一次前驱计数。

快速公式针对本批正式图的标准结构：中间张量在L1/UB、至多一个计算生产者、原始COPY规范。初始化时验证前提。若出现多计算生产者、非标准COPY形状或与题意不符的边，明确返回 `UNSUPPORTED_INPUT_MODEL` 或进入另行声明的官方后备路径；不能悄悄截取第一个生产者。DP遇到不能确定COPY归属的合法特殊形状可跳过该增强组件，不能伪称精确。

JSON解析须检查重复键；拒绝布尔值冒充整数、负数、缺失端点、非二部边和非DAG。输出键统一 `str(original_id)`，避免“011”和“11”转换后重复。全空计算集合单独处理并交官方验证；不能进入需要种子的循环。

## 5. 构造状态和试放事务

记 F 为已接受计算节点集合（包括当前子图），S 为当前子图，O_k 为核心k上已接受的全部计算。编码时用 `assigned_ops/current_subgraph/core_ops`，不要把核心Task与子图同名。

| 数学含义 | 建议字段 | 更新规则 |
|---|---|---|
| 节点是否已接受 | assigned[v] | 仅commit改变 |
| 计算前驱未接受数 | remaining_preds[v] | 接受每个前驱后减1 |
| 张量未接受计算消费者数 | remaining_consumers[t] | 同一个消费者只减1 |
| 当前核已有消费 | consumer_core_mask[t] | 位图，按实际计算消费更新 |
| 本核触及的张量 | touched_core_mask[t] | 生产或消费后置位 |
| 已生成基础输入/跨核对 | input_seen / transfer_seen | 区分原输入和 `(t,src,dst)` |
| 核内参考可用时间 | tensor_ready[(t,k)] | 缺失用None，不用0代替 |
| 四Pipe参考结束 | pipe_end[k][p] | 试放可同时影响多个核心 |
| 计算参考结束 | compute_end[v] | 只写新接受节点 |
| 核/子图M、V工作量 | core_work / subgraph_work | 分开维护 |
| 虚拟保留字节 | live_proxy_bytes[k][pos] | 非精确缓存模型 |
| 虚拟历史峰值 | peak_proxy[k][pos] | 不因close子图重置 |
| 基础COPY工作/字节 | copy_work / copy_bytes | 精确基础计数，非spill |
| 已触发计算后续尾长最大值 | tail_completion_max | 支持O(1)更新评分 |
| 计划版本 | state_version | 接受节点时递增，防止提交过期delta |

**试放函数必须只读基础状态：**

```python
trial = try_place(graph, state, op_index=v, core_id=k)
# TrialDelta: score, incremental_bytes, new clocks/maps/peaks/counters, version
commit_trial(state, trial)
```

试放可以使用少量覆盖字典/日志或临时4N时钟副本，不能每次deepcopy全图。远端输入的源核MTE3也会改变，不能只回滚目标核。候选都必须从同一个state_version试算；只有最终选中者提交一次，随后更新就绪集合和子图成员。未选中试放不能改变任一计数、映射、peak、ready堆有效标志。

在测试中另写一个慢的深拷贝状态试放作为oracle，逐字段比较快速事务。试放不变量是工程验收的核心，不是仅检查最后方案能否运行。

## 6. 精确基础搬运计数

以下t只遍历核内逻辑张量，不把它的原始DDR节点重复计数。p_t为单计算生产者或None，U_t为不同计算消费者，z_t为最终写出标记，D_t为消费核心集合。

单次工作：

\[
q_t=\max(1,\lceil s_t/60\rceil).
\]

本次官方代码对每个实际 `(tensor, source_core, target_core)` 生成独立COPY对，因此

\[
Q_B^0=\sum_{p_t=\varnothing}|D_t|q_t+
\sum_{p_t\ne\varnothing}\bigl(z_t+2|D_t\setminus\{k(p_t)\}|\bigr)q_t.
\]

换成s_t得到总基础字节V_B^0。最终原始写出与跨核写出分开计数。向两个目标核送同一产物要两次源端OUT和两次目标端IN；不能优化成单源OUT，官方不会这样结算。同一目标核多个子图共享该产物，基础IN只计一次。

试放就绪节点v到k的增量为：

\[
\delta Q(v,k)=
\sum_{t\in In(v),p_t=\varnothing}q_t\mathbf1_{k\notin D_t^F}
+\sum_{t\in In(v),p_t\ne\varnothing}2q_t\mathbf1_{k\ne k(p_t),\,k\notin D_t^F}
+\sum_{t\in Out(v)}z_tq_t.
\]

对应字节增量将q_t换成s_t。所有输入输出先去重，生产者核心由ready条件确保已经确定。

计算结束时必须断言：增量累积总工作/字节 = 完整映射公式 = 官方便宜基础核图的COPY计数。若不一致报模型/适配错误，不用官方结果覆盖错误以“修好”测试。

## 7. 核心级虚拟驻留与便宜四Pipe推进

### 7.1 虚拟驻留

K_k(F)表示核心k已触及、仍有全局未接受计算消费者的张量。未知消费者以后是否放本核尚不明确，这一保留策略是保守启发式。

试放v的申请前瞬态：

\[
m_{k,\ell}(F,v)=\sum_{t\in K_k(F)\cup In(v)\cup Out(v),\,pos(t)=\ell}s_t.
\]

更新该核虚拟历史峰值后，定义

\[
\widehat P(F)=2\sum_k\left[
\left\lceil\frac{(\widehat M_{k,L1}-524288)_+}{60}\right\rceil+
\left\lceil\frac{(\widehat M_{k,UB}-131072)_+}{60}\right\rceil\right].
\]

必须先统计输入和输出同时在场的瞬态，再递减消费者数/释放。接受最后一个计算消费者后，该张量在所有触及核心的虚拟保留集合中移除；可用holder位图避免扫描全图。输出无未来计算消费者也必须参与本次瞬态。全部峰值在封闭子图时保留。

K_k和tensor_ready用途不同：移除虚拟保留数据不删除“曾存在基础通路”的计数；便宜时间模型仍不模拟spill，不能把这个不完美代理称为准确resident cache。

### 7.2 试放推进

每核维护MTE2、MTE3、M、V预计结束A[k,p]。输入和输出均按原tensor id递增处理。

若tensor_ready[(t,k)]存在，直接使用。若是原始输入且该核没有基础副本：在A[k,MTE2]后排q_t周期，登记ready并增加一次读取。

若是远端产物，源核为a，首次送往k：

\[
\widehat e_{out}=\max(A[a,MTE3],\widehat r_{t,a})+q_t,
\]
\[
\widehat e_{in}=\max(A[k,MTE2],\widehat e_{out}+500)+q_t.
\]

同时更新两核时钟和transfer_seen。生产核的数据不能被替换成整个源Task完成时刻。500也不能从源计算完成就开始计，而要从OUT完成开始。

所有输入具备参考ready后：

\[
\widehat e_v=\max\{A[k,Pipe(v)],\max_{t\in In(v)}\widehat r_{t,k}\}+\max(1,c_v).
\]

更新计算Pipe和每个输出ready。需要原始最终写出的输出，按id顺序追加到本核MTE3；以后出现远端消费者时，再按每个目标核增加独立OUT/IN。

该推进只有Pipe级局部串行和COPY对同步，没有共享带宽事件仿真。不得把所有核心拉到一个统一“构造当前时刻”；更晚被程序接受的独立操作仍可在另一核的较早参考时刻运行。

### 7.3 评分

\[
a(v)=\bar c_v+\max_{u\in Pred(v)}a(u),\qquad
b(v)=\bar c_v+\max_{w\in Succ(v)}b(w),\quad \bar c_v=\max(1,c_v).
\]

\[
\widehat C(F)=\max\{\max_{k,p}A[k,p],\ Q(F),\
\max_{v\in F}(\widehat e_v+b(v)-\bar c_v)\},\qquad
\widehat J(F)=\widehat C(F)+\widehat P(F).
\]

所有空最大值取0。Q(F)是已承诺基础COPY独占参考周期之和。新增一个节点时，最后一项只需与新节点尾长比较，不重扫全部F。

本式不是官方Makespan，不用于“必然更好”的证明。加入的远端COPY是按发现顺序排布，官方可能按另一个确定性次序安排；这是明确接受的构造近似，不擅自把参考顺序传给官方替换Step1。

## 8. 子图与核心联动构造的精确规则

### 8.1 候选范围和粒度

模式顺序固定 `global`、`component_grouped`。后者对计算依赖图的弱连通分量分组，不对原始含共享输入图强行拆分。

设分量数r，分组数h=min(N,r)。按 `(-max(WM(C),WV(C)), min_original_op_id(C))` 排分量；依次放入使 `(max(new_group_M,new_group_V), new_group_M+new_group_V, group_id)` 最小的组。分组不是核心归属。按group_id处理候选范围，处理下一个范围时不清空核心状态。r=1时省略重复模式。

每种模式分别从全新状态构造β=1,2,3,4：

\[
H_\beta=\max\{1,\lceil \max(W_M,W_V)/(\beta N)\rceil\}.
\]

β不是Task数目；当前场景真实Task由核心决定。每份构造互不继承前一份状态。

### 8.2 开启子图

ready要求所有不同计算前驱已经被接受。当前范围B内选种子，键为 `(-b[v], a[v], original_id[v])`，取最小。然后试放所有核心，键为：

`(trial_J, added_basic_bytes, max(core_M_after,core_V_after), core_id)`。

接受最小者，申请一个单调递增的新sgid，追加到选中核列表；强制接受种子。设H_S=max(Hβ,bar_c_seed)。每个sgid仅此时追加一次，后面只是向其成员集加节点，已封闭的sgid永不重开。`commit_seed_trial`须在一次原子提交中校验trial版本、创建sgid/追加列表、应用trial并纳入种子，再增加state_version；不要先开子图修改状态后错误地提交一个过期trial。

### 8.3 非空子图的四名单

候选先满足全局ready、属于当前范围、未被接受，以及加入后子图WM、WV均不超过H_S。当前S的核心为k。

1. 后续路径：按 `(-b[v],id[v])` 取前K。
2. 搬运复用：按 `(-reuse[v,k],-b[v],id[v])` 取前K。原输入已在该核被使用记q_t；计算产物若本核生产或已首次基础传入记2q_t。每张量只计一次。它是排序信号，不是实际省时。
3. 直接延续：Pred(v)∩S非空，按 `(-b[v],id[v])` 取前K。
4. 管道平衡：按该核心全部已接受工作比较M/V，选较轻管道节点，按 `(-b[v],id[v])` 取前K；相等则此名单为空。

K默认8，合并去重后至多32个候选。名单外操作没有被删除，仍可在以后加入。并列顺序完全由数值和原id决定，不能依赖set遍历或线程完成先后。

对每个入围v，从同一快照试放所有核心，定义

\[
\widehat\Delta_{core}(S,v)=\min_{h\ne k}\widehat J(v@h)-\widehat J(v@k).
\]

选使 `(-delta_core,-b[v],added_bytes_on_k,id[v])` 最小者。最大优势非负则提交在当前核的trial；全为负则关闭S。N=1测试模式下定义delta_core=0，避免对空集合求min。

当前没有合粒度候选也关闭S。关闭操作只固定子图、清空S与子图工作量，不改变core_work、Pipe时钟、核心驻留参考或基础COPY缓存。

### 8.4 构造伪代码

```text
for mode in modes:
  for beta in [1,2,3,4]:
    state = fresh_state()
    for scope in scopes(mode):
      while scope contains unassigned ops:
        if no open subgraph:
          v = select_seed(ready_in_scope)
          trial = best_core_trial(v); commit_seed_trial(state, trial)
        else:
          candidates = build_four_shortlists(state, scope)
          if empty: close_subgraph(); continue
          trials = pure_try_each_candidate_on_each_core()
          best_v = select_by_delta_core(trials)
          if delta_core(best_v) < 0: close_subgraph()
          else: commit(current_core_trial(best_v))
      close_open_subgraph_if_any()
    assert exact_coverage_and_no_return()
    yield completed_plan_and_proxy_report
```

子图只能从前驱已接受的节点生长，形成顺序构成统一的子图先后编号；各核心列表也是形成次序的子序列，所以构造不会产生子图级循环等待。每个新S至少接受一个节点，确保有限终止。该论证不承诺其扩展操作全局执行一定成功，最终官方校验仍不可省。

## 9. 便宜完成候选评价与基础方案B0选择

最多8份完整构造先调用只到Step1的前缀适配器，取得各核真实基础图、raw_seq、seq及COPY链接。对子图覆盖和序列拓扑错误记为无效；只计算无换出曲线超限则不能拒绝，因为官方Step2可以换出。

每份候选记录三个独立量：构造参考时间 `proxy_J`、精确基础总字节 `base_copy_bytes`、全核无换出归一化峰值 `profile_peak_scaled`。

本实施稿冻结便宜排序键为：

```text
(proxy_J, base_copy_bytes, profile_peak_scaled, mode_order, beta, plan_signature)
```

`plan_signature`定义为按原操作id数值排序的(op_id,sgid)元组，以及按核心编号排列的各列表元组；日志可另存它的SHA-256，但不能把随机hash()用于排序或持久缓存。这是新增的工程并列约定，不宣称此序等价于按官方Makespan排序。为严格复现先按此键，不在内部临时增加不同权重。

先按实际边界图、真实seq、跨核链接和配置指纹去重；不要把不同原始COPY id/seq只凭“同核分配相同”认定为等价。内存中保留少量结果或按需重建，避免同时保留8份大型完整展开。

然后按便宜顺序运行官方完整评估，取得第一份实际可执行方案，记为B0。**不是对8份都运行完整评估后选真实最优**，这是本设计减少rollout的关键。第一份失败时才试下一份。记录所有尝试，包括失败/超时，不把失败计入“只有两次评价”的宣传。

若全部正常候选失败，后备构造为：计算节点按确定性拓扑序各自一个sgid，全部放核心0，其余核心为空；再运行正式评价。场景B中此法不会清空缓存，能否执行仍由官方判断。若后备也失败，只返回清楚的错误和已有诊断，不输出伪成功方案。

## 10. 无换出驻留证书：真实Step1上的线性扫描

输入是某核基础图与 `_prioritize_task_seq` 后的真实seq，必须包含所有基础COPY。对核内本地图中的张量t，取其生产操作和消费操作在seq中的所有触及位置，按操作去重，记最早和最晚位置为f_t、l_t。

DDR张量不占L1/UB，不计；没有任何触及的张量不计；COPY_OUT消费也算一次触及。不同核心本地副本的键使用 `(core_id, local_tensor_id)`，不能跨核心合并内存。

\[
M_{k,\ell}(i)=\sum_{pos(t)=\ell,\; f_t\le i\le l_t}s_t.
\]

用长度n+1的差分数组：f_t位置加size，l_t+1位置减size。取前n项前缀和得到“该操作分配之后、末次使用释放之前”的峰值。**闭区间两端都必须计入；不能先减最后一次输入，再加输出。**

若该核两类峰值均不超容量，则固定seq的官方Step2不会触发换出；若无换出模型超限，则Step2须换出或失败。该结论严格对应本次源码的alloc→检查→执行→free顺序。

输出 `peak_l1, peak_ub, first_overflow_index, peak_index, no_spill_certified`。不要把无换出曲线称为官方全局实时内存曲线。零换出证书也不能替代跨核执行检查。

固定核心归属下，若所有核都获得证书，则该方案全部搬运等于基础搬运V_B^0，达到这一固定归属下的最少搬运字节。只要还超限，峰值降低并不自动保证spill字节也下降。

## 11. 窗口选择与允许的排序空间

B0正式验证后，增强阶段固定g、子图成员、操作核心；只改变某核core_schedules的连续窗口内顺序。不新增/删除计算，不改跨核分配，不复制节点，不将窗口子图重新合并。

默认窗口宽度m≤8，总共至多处理32个窗口（含没有改善的窗口），每次只优化一个核心。该规模是运行预算，不是数学上的最优常数。

本实施稿的确定性选择约定：对每核优先取最早超限操作所在子图；没有超限则取峰值首次出现处所属子图。宽度取min(8,该核子图数)，以枢轴为中心，靠近边界时夹紧到合法范围。只有一个子图的核心无排序空间。多个核心同时有窗口时，超限优先，其次全核归一化峰值更大者，再按core_id、窗口左端排序。

同一状态下已经处理而无改善的窗口记为seen，再尝试由其他桶峰值/超限位置构成的不同窗口；仍按超限、峰值、位置排序。候选重复用 `(完整计划版本, core_id, left, right)` 排除。接受一次窗口后版本递增，重建受影响核曲线并更新全局依赖约束。预算有限保证终止，不无限次重做同一窗口。

### 11.1 保留经过窗口外和其他核心的先后路径

构造子图级安全约束图：保留原始子图数据依赖及其他核心的完整顺序；对目标核心，删除旧窗口内部的顺序边，但保留窗口外前缀、后缀顺序。窗口前最后一个子图是左锚点，后第一个是右锚点；为每个窗口子图加入左锚点→它、它→右锚点（锚点不存在则省略）。若某条边既是数据边又是旧顺序边，只删除“顺序”原因，不删除数据依赖。

在该图中求窗口节点间可达关系，形成 `pred_mask[j]`。经过区域外、其他核心、其他窗口节点的路径都要保留。DP只在pred_mask[j]已包含于mask时选择j。

这定义的是保留完整外部先后关系的安全排序空间，不是“所有可能运行的操作级全局方案”。基座原顺序必须在其中；若不在，报告约束构造错误。最终还要检查更新后的完整方案、核心序列和官方扩展执行关系。

不要把所有核心各缩成一个Task点再做无环判定；也不要给每个子图插入100周期或整体完成屏障。

## 12. 包含动态COPY桶的精确子集动态规划

### 12.1 必须冻结的输入

固定该核基础图、其raw_seq、原子图成员、核心分配、窗口外顺序。当前源码在单生产者标准结构下，固定这些内容后，窗口重排不改变基础图和COPY id，只改变COPY_IN的所属桶及稳定桶排序。

计算节点桶固定；COPY_OUT桶由其唯一计算生产者的sgid确定；COPY_IN桶归属于该核当前顺序中最先使用该输入的子图。因此重排时不能沿用旧op_subgraph中的COPY_IN桶。

对每个基础COPY_IN c，保存其在本核的消费子图集合。前缀已经有消费时，它在固定前缀执行；前缀没有而窗口中有消费时，它是可移动输入COPY；仅后缀有消费时，它留在固定后缀。标准COPY只有一个本地输出和一个DDR输入；若形状超出已实现推导，标记 `WINDOW_UNSUPPORTED` 并保留原序，不伪造桶归属。

所有固定桶内及每次加入的COPY，最后均按raw_seq的位置稳定排序。不得简单把COPY_IN全插到桶头，或让Codex自定义更优操作级顺序；那不是官方会执行的结果。

### 12.2 状态和准确触及计数

窗口m个子图按原窗口顺序标为0..m-1，用整数mask表示已完成子图集合X。固定前缀始终已完成，固定后缀始终未完成。

每个操作o的触及张量集合定义为 `local_inputs(o) ∪ local_outputs(o)`，排除DDR并去重。同一个张量同一操作多次出现只算一次，这和官方 `_build_tensor_uses` 一致。

记：n_all[t]为该核全部seq操作对t的总触及数；n_pre[t]为固定前缀触及数；u_fix[t,j]为桶j中固定操作触及数。对一个窗口可移动COPY c，M_c为其消费子图位掩码，u_copy[t,c]为它是否触及t（0或1）。

状态X已经发生的触及数为

\[
n_t(X)=n_{pre}[t]+\sum_{j\in X}u_{fix}[t,j]
+\sum_c u_{copy}[t,c]\mathbf1_{(M_c\mathbin{\&}X)\ne0}.
\]

因此状态入口的无换出驻留集合精确为

\[
Live(X)=\{t:0<n_t(X)<n_{all}[t]\}.
\]

即已经触及且还有未来触及。这个状态只依赖子图集合X，与X的到达顺序无关。不得把不同历史的真实Pipe时间、Step2换出状态或全局sim状态强行合成一个mask；这些并不由X唯一决定。

### 12.3 转移扫描

候选下一个子图j必须不在X中，且 `pred_mask[j] & ~X == 0`。

它的真实操作桶为：

```text
fixed_ops[j]
+ 尚未被前缀/X引入、且消费集合包含j的COPY_IN
```

将上述操作按raw_rank合并排序。用Live(X)和n_t(X)开始逐操作扫描：先对本次触及而尚未驻留的本地张量申请空间；更新两类瞬态峰值；再把每个本次触及张量计数加1；到达n_all时释放。

扫描结束的驻留集合必须等于Live(X∪{j})。此断言在小图测试中每个转移均检查。输入输出同时存在、最后一个COPY_OUT后才释放、共享输入COPY首次归属，是三个最常见错误源。

### 12.4 精确目标和递推

令容量最小公倍数为L，使用整数比例系数 `sL=L/cap_L1`、`sU=L/cap_UB`。默认L=524288，故归一化峰值可用

```text
scaled_peak = max(L1_bytes, 4*UB_bytes)
```

比较，实际归一化值为scaled_peak/L。不要在DP内用浮点epsilon。

令d[X]为完成X的最小历史scaled_peak。初值d[0]为进入窗口时Live(0)的scaled占用，其他状态为正无穷。转移扫描得到h(X,j)，包括入口、各操作瞬态及出口：

\[
d[X\cup\{j\}]=\min\{d[X\cup\{j\}],\max(d[X],h(X,j))\}.
\]

记录prev_mask与chosen_j。数值相同可按原窗口相对位置序列的字典序选路径，确保确定性；终点值等于原窗口峰值时整体保留原窗口，不为“不同但等价”次序改计划。

m=8时最多256个集合状态、1024个状态—动作转移。转移不是O(1)：需要扫描桶事件或使用等价预聚合。复杂度应报告状态数、转移数及扫描事件数，不仅报告2^m。

精确性理由：相同X有相同的已发生触及集合、相同Live和未来动作，只有历史峰值不同；较高历史峰值不能在相同后续中反超较低者。因此每个X只保留一个最小峰值充分。所有合法next_j都进入递推，搜索完成即得到此安全窗口空间内的标量峰值最优。

### 12.5 接受一次局部排序

只有DP完整结束、回溯顺序在安全域中、窗口目标严格降低时才考虑采用。更新真实COPY_IN桶，再调用官方 `_prioritize_task_seq`（或等价前缀构造）重建序列，断言与DP预测序列逐操作一致。用完整线性扫描复核该核曲线、入口/出口live和标量峰值。

**原始g和计算核心不改变。**新序列语义失败、输出不匹配，视为实现错误/不支持，不“修复”为另一套排序后继续声称DP精确。

窗口前后操作集合及窗口出口Live相同，窗口后曲线不变，因此整核归一化无换出最大峰值不会因这次接受而升高。不要扩大成每个内存分量都不升高，或真正spill字节必降；后者只有新序列获得全核无换出证书等额外条件时才有保证。

超时中止一个DP窗口时，默认不提交这个窗口，返回 `WINDOW_INCOMPLETE`；保留之前已经完整验证的窗口修改。不把中止的结果标为局部最优。

### 12.6 可选的释放优先交换

只有两相邻桶间无依赖、外部顺序允许、可变本地张量不相交、COPY_IN首次归属不改变，且两种缓存均满足前桶净变化≤0、后桶净变化≥0时，“先释放后增长”才保证峰值不增。它不保证全局时间变短。

本实施稿默认不启用单独交换模块，以DP为唯一正式排序优化器；该交换结论可用于DP分支访问顺序、单元测试或未来受控快速路径，不可未经门控就批量重排。这不减少DP覆盖的允许顺序。

## 13. 增强流水线、正式接受与结果保护

取得已正式验证的B0后，保存其Plan与官方结果为不可变对象。优化工作只在副本上进行。

```text
baseline_plan, baseline_result = first_valid_cheap_ranked_candidate()
trial_plan = copy_plan(baseline_plan)

repeat at most max_windows:
    prefix = rebuild_or_update_exact_step1(trial_plan)
    profiles = exact_no_spill_profiles(prefix)
    window = choose_next_not_processed_window(profiles, constraints)
    if none: break
    proposal = solve_window_dp(window)
    if complete and strictly better local peak:
        validate_and_commit_order_only(trial_plan, proposal)
        rebuild affected profile and invalidate stale window descriptions

if trial_plan == baseline_plan:
    final = baseline
else:
    run official full evaluator once on assembled trial_plan
    if valid and (makespan,added_bytes) lexicographically lower:
        final = enhanced
    else:
        final = baseline

write final candidate file; read it back; run unmodified official CLI;
verify identical metrics; atomically publish plan/result/trace/log.
```

正常路径正式评价两份不同方案：B0一次，组装后的B1一次。不是每个窗口一次，更不是每个DP状态一次。最终文件重新读取的正式复核额外计一次；失败基座尝试、重试、后备评价要另外记录。因此日志要同时记录“不同方案评价数”和“实际调用数”。

一连串峰值优化后B1仍可能时间变慢或出现全局执行死锁，返回B0是本设计的正常保护行为。不得将局部峰值奖励直接换成“必接受”的真实改进。

有完整B0结果时，算法预算结束返回B0或已经正式确认更好的结果。没有完整有效结果时，超时必须返回明确失败，不把部分g当作可提交方案。

## 14. 正确性不变量与边界情况

必须可在debug模式中逐步断言：

1. 接受节点前所有计算前驱已接受，接受后未分配数减少1，节点不能重复进入S。
2. `current_subgraph`内所有计算核心相同；每个sgid只在一个列表出现一次；close不修改任一core级状态。
3. 快速trial不改基础状态；commit结果逐字段等于slow_trial的预期结果。
4. 增量基础搬运与完整公式、官方基础核图字节完全一致。
5. 真实Step1覆盖全部基础核图操作且满足拓扑，COPY的桶归属每次基于新顺序重算。
6. 无换出峰值含申请前后瞬态；证书为真则官方Step2的spill_records为空。
7. 相同DP mask通过不同历史到达时n_t、Live、next block一致；DP值等于完整枚举最小值。
8. 窗口入口/出口Live及窗口外顺序不变；g/core归属始终不变。
9. 最终 `(makespan, added_copy_bytes)` 不劣于同次运行正式验证的B0；完全同值保留B0。
10. 方案输出前后官方源码/config哈希不变。

N=1、空核心、相同优先级、零cycles、零size、无前驱/无后继、共享输入跨多个子图和核心、同产物同时本核和远端消费、多个远端目标且原图最终输出、跨核阶段回返、窗口外穿越路径等都必须有测试。不要假定每个核都有任务或每个Task都有两类管道。

## 15. 可迁移严格组件的使用边界

合法性检查、精确基础通信、无换出证书和窗口峰值DP为默认组件。第一问B0/B1/DDR区间剪枝若以后移植，仅允许对已经完整确定的场景B方案使用重新构造的操作图和500同步，不把100/1000或整核Task等待带入。

本次复现不要求把这些时间剪枝再叠成额外的复杂搜索。现有8份便宜构造+一次组装增强已经决定主要调用数量。可选优化必须有独立开关和差分回归，不改变默认复现版本的候选空间、评分或正式评估语义。

对官方输入完全一致的结果缓存可复用；同核心归属不等于所有序列、spill和时间一样。缓存键至少含原图与源码/config哈希、规范化g、所有core_schedules；C1层还含实际局部图、raw_seq/真实seq及COPY映射。缓存值不可变，模拟游标与剩余工作量不得共享。

## 16. 性能和可复现性实施要求

先完成透明的正确参考实现与小图差分测试，再优化热点。构造内循环禁止官方完整评价；窗口DP内循环禁止Step2/3/D；禁止按每个trial整图deepcopy、整图序列化、创建进程或生成巨型日志。

原id稠密映射、核心位掩码、remaining计数、固定长度Pipe数组、局部delta都优先于大型对象图。基础数据流只读共享；多实例并行默认按case/N分进程，不在单节点trial中并行。默认workers=1，先测峰值内存后开放多进程，尤其不要同时展开多个超大核图造成OOM。

四短名单可先精确扫描ready并用固定K小堆；其最坏复杂度可能二次，不能宣称固定K就线性。优化复用名单时只更新消费张量受影响的ready节点，分核global负载变化另行处理。若改为近似截断ready扫描，将改变算法，须新版本记录，不能叫纯加速。

DP先预聚合每桶固定事件和每张量的窗口use mask；只对窗口相关张量处理状态变化，其他跨窗口长期驻留字节作为常量。禁止为256个mask各deepcopy整个核心图。`raw_seq`排序位置预建数组；桶合并沿raw_rank线性合并。

不允许为“更快”而修改原图cycles/size、扩大capacity、移除500、取消带宽竞争、跳过容量约束。时间不加权成字节目标；最终指标仍由官方给出。

计时使用monotonic墙钟；日志中的cycles是模拟时间，wall_seconds是求解耗时，不能混淆。默认墙钟配置600秒/每个case-N调用，属于工程预算不是完成时间保证；预留60秒用于输出复核，实际全局评价可能更慢，应动态估计而非硬称全例必在10分钟以内。基座尚未成功时不消耗全部预算做DP。

固定候选顺序和确定性计数预算可保证重复执行一致；墙钟中断位置可能因机器负载不同而不同。严格复现实验记录机器、Python版本、算法config、源码hash和实际终止原因。不要声称只固定随机种子就有跨机位级一致性。

## 17. 失败状态和产物管理

`INPUT_INVALID`、`REFERENCE_MISMATCH`、`UNSUPPORTED_INPUT_MODEL`、`PLAN_INVALID`、`OFFICIAL_ERROR`、`EVAL_TIMEOUT`、`RESOURCE_LIMIT`、`WINDOW_UNSUPPORTED`、`WINDOW_INCOMPLETE`、`SUCCESS_VERIFIED`必须区分。算法层只处理预期错误；IndexError/KeyError等实现bug保留traceback，不能except Exception后当作“没改进”。

官方评价可放独立worker进程，超时终止整个子进程并清理其临时目录；线程Future超时不代表CPU计算停止。Windows入口使用main保护，路径用Path，命令传列表而非拼接shell字符串。

先写case工作目录；最终方案序列化后重新读取、官方复核成功，再以os.replace原子发布。若验证失败，不覆盖旧的已验证结果。断点续跑仅在所有hash与配置一致时复用，不能仅凭已有filename就跳过。

`run.json`至少记录：case/N、graph/config/source/code哈希、所有算法参数、候选模式/β、proxy及基础字节、选定B0、增强窗数/接受数/DP状态转移数、B0/B1/final真实指标、不同方案评价数/实际调用数/失败数、分阶段墙钟/峰值内存、终止状态、是否最终复核通过。`windows.jsonl`记录窗口前后顺序、目标值、两类峰值、是否完整、跳过原因和序列一致性检查。

日志不默认逐节点打印全状态；提供debug_trace开关，只在需要时保留细粒度jsonl。

## 18. 实现顺序与验收门槛

阶段A：官方只读适配+输入输出+最小样例。先运行本包探针，再跑官方CLI，最小样例须为6周期、32字节、额外0。

阶段B：不可变图索引与精确通信计数。穷举小图core映射，和官方基础构图完全一致，特别核查两目标核COPY对。

阶段C：纯trial/commit、ready构造、四名单和8候选。与慢参考逐步对照；在小图上验证完整覆盖和确定性。此阶段还没有DP也必须能独立输出B0。

阶段D：真实Step1无换出证书。与Step2在随机小图及边界样例对照，不把超限误记为输入非法。

阶段E：动态COPY分桶与子集DP。先用暴力排列oracle验证，再开放m≤8生产版本；保持峰值目标限定，不全局rollout每个叶子。

阶段F：集成B0/B1保护、窗口预算、错误处理、原子输出、batch和报告。

阶段G：至少跑case_001、case_067、case_086的N=2,5冒烟测试，再扩展100×4完整实验。case选择仅为测试覆盖，主算法不能按case名分支特化。每个结果都来自正式评估；旧对话中的数值只作背景，不硬编码成这一新基座预期值。

最低验收测试详见 `docs/ACCEPTANCE_TESTS.md`。代码完成报告必须列出已运行的命令、实际结果、未完成范围、性能瓶颈；不得把本说明里的定理当成已经跑完大图实验的证据。

## 19. 单核基准、汇总和实验记录

正文平均加速比按每例 `T_single_fixed/T_N` 算术平均，单核点定义1。分母T_N是本算法最终正式结果；分子必须是题目固定的原图单核核内调度，不是本算法N=1优化结果。

可调用本附件 `singlecore_evaluate.py`/`evaluate_singlecore`；它将整图合并一个子图，在单核上按固定官方流程计算。按graph/config/source哈希缓存，不为每个N重复计算。

逐例附录含case、N、Makespan、scheduled/original/added字节、基本与增强是否被采纳、求解墙钟。汇总统计必须报告有效数量与失败数量，不把失败case静默剔除后声称全100例平均。主要绘图可留给论文流程，不要求核心调度引入pandas或绘图库。

## 20. 本包已核验与尚未完成的边界

已实际核对本次官方源函数签名与逻辑，并运行 `tools/probe_official.py` 的4份完整小方案。前缀适配器产物接回原Step2/3后，其图、序列、pipe_ops、跨核链接和搬运计数与原完整构造一致。

本次小型检查结果见 `minitest_results/summary.json`：minimal=6周期；cross_fanout为2个跨核链接、基础128字节；两个缓存控制图无换出峰值140000→100000字节，实际spill增加字节80000→0。当前控制图的实际时间为3736→2302周期；这是本包新生成样例，输入大小与前文控制例不保证相同，不要求照抄前文3735→2301。

本包不是已完成的新基座工程，未声称本次已实现快速trial、完整子图生长或生产版窗口DP，也未声称跑完100×4实验。Codex须按阶段实现并提供测试结果。附带前缀和探针是可运行的接口起点，不是替代主算法的随机/简单baseline。
