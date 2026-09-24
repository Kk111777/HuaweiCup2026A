# 问题一初始化：模型、实现与运行

本模块输入原 DAG，输出 `node_to_subgraph` 和 `core_schedules`，通过官方问题一评估器计算总体时间和搬运量。**默认开启独立自适应 β 组件**：两种初始化从 β=1 开始各自递增，首次官方总体时间不再改善即停，最多试到 β=4，再取已评价的最好方案。可关闭组件以固定 β=1 做消融，或完整枚举 β=1,2,3,4 作对照。以下列出模型公式、实现对应和运行接口。没有第二阶段的瓶颈识别、迁移、合并或局部搜索。

## 使用

团队基础环境为 Python **3.12**；求解器、公共基线接口与测试仅使用标准库。此次迁移还在 Windows / Python 3.13.5 上执行了测试。

从项目根目录运行：

```bash
# 单例：校验并复用仓库已发布的单核基准
python src/solve_problem1.py solve official/data/case_001.json --cores 2 3 4 5

# 关闭自适应组件：两种初始化均固定 beta=1，其他构造和评价规则相同
python src/solve_problem1.py solve official/data/case_087.json --cores 5 --beta-search fixed --output-root outputs/q1/fixed

# 完整枚举对照；不改变模拟 NPU 核数或评分规则
python src/solve_problem1.py solve official/data/case_001.json --cores 2 --beta-search grid --output-root outputs/q1/grid

# 选定用例；jobs 是同时处理的用例数
python src/solve_problem1.py batch --cases case_001 case_086 --cores 2 --jobs 1

# 后续服务器全量实验，本次未执行此命令
python src/solve_problem1.py batch --cases all --cores 2 3 4 5 --jobs 1 --resume

# 必要测试
python -m unittest discover -s tests -v
```

`solve` 默认从计算图所在目录读取 `config.txt`；`batch` 默认读取仓库官方数据目录。两者都支持 `--config`、`--output-root`、`--cores`、`--beta-search`、`--candidate-jobs` 和 `--resume`；`batch` 另支持 `--data-dir`、`--cases`、`--jobs`。核数限于 2–5，单核基准默认复用 `results/problem_1_singlecore/`。父进程调用 `common.baseline.load_baseline_catalog` 一次，验证整个归档及本次请求的输入、配置、官方程序哈希，再将单条记录传给各用例进程；不重复运行单核或复制历史产物。硬件参数必须从配置读取，不静默使用代码默认值。

默认数据与输出路径相对于程序所在仓库定位，显式相对路径相对于调用目录解析。通常先 `cd` 到根目录运行上述命令；也可以从其他工作目录传入 `src/solve_problem1.py` 的绝对路径。模块调用 `python -m q1` 则需将 `src/` 加入 `PYTHONPATH`。默认 `--jobs 1 --candidate-jobs 2`：同时处理一个用例，两个工作进程启动两种模式；只有一个计算分量时固定/自适应均串行处理该模式。`--candidate-jobs 1` 可强制串行。`--jobs` 处理不同用例；两层并行同时增大会形成乘积级进程数。各例的不同核数仍顺序执行。最终方案重读复评保持串行。

候选并行采用独立进程，每个进程独立执行原算法和官方评估。候选完成顺序不影响最终按时间、搬运、模式顺序、β 择优；构造记录仍按固定模式和 β 顺序保存。并行数只影响运行方式，在来源记录中保存，不参与算法指纹。`events.jsonl` 增加每 1000 个操作的标注进度，以及核内预分析、核心分配、正式评估阶段记录。

服务器使用完整仓库，并另行准备同版 `official/data/case_*.json`；原图被 Git 忽略，不能只依赖克隆取得数据。不需要图形界面或 GPU。

自定义小图或新配置若没有匹配归档，可显式传入 `--recompute-baseline`，此时每例先调用官方整图单核评估一次。默认归档缺失、损坏或版本不匹配会明确报错并保存 `baseline_failure.json`，不会悄悄重算或使用不匹配的成绩。`--baseline-archive` 可指定仓库内的其他已验证归档，与 `--recompute-baseline` 互斥。

## 工程职责与 Python 接口

| 模块 | 职责 |
|---|---|
| `src/q1/graph.py` | 官方原图校验、计算依赖与张量索引、静态路径与分组 |
| `src/q1/partition.py` | Task 生长、参考费用、短名单和封闭条件 |
| `src/q1/schedule.py` | Task 级列表分配 |
| `src/q1/official.py` | 原附件接口适配，实际调用原调度器与模拟器 |
| `src/q1/solver.py` | 有限候选、核内修补、正式评价与择优，无文件 I/O |
| `src/q1/run.py` | 产物、指纹、续跑、按用例并行与汇总 |
| `src/q1/__main__.py` | 统一命令行 |

```python
# 脚本置于 src/ 下，或将 src/ 加入 PYTHONPATH
from q1 import Settings, initialize
from q1.official import read_json

if __name__ == "__main__":
    graph = read_json("official/data/case_001.json")
    settings = Settings.read("official/data/config.txt")
    answer = initialize(graph, num_cores=2, settings=settings,
                        candidate_jobs=2, beta_search="adaptive")
    plan = answer.plan
    evaluation = answer.evaluation
    selected = answer.selected
    candidates = answer.candidates
    search = answer.search
```

`InitializationResult` 返回最终方案、官方完整评价、选中模式/β/候选序号、各候选构造记录及 `search` 搜索记录。可选 `progress(event)` 回调报告候选进度、阶段与耗时及每种模式的停止原因；`candidate_jobs` 默认 2，`beta_search` 默认 `adaptive`。Python 脚本启用多进程时，应把调用放在 `if __name__ == "__main__":` 保护内，兼容 Windows 的 spawn。调用者若需要文件、复评与完成标记，应使用命令行入口。

## 可开关的自适应粒度组件

每个用例、每种核数独立选参。两种初始化方式为 `global`（全局范围）和 `components`（独立计算分量分组），自适应组件只决定是否继续尝试下一 β，不改变任一候选的构造与评估。

| `--beta-search` | 规则 | 正常候选上限（双模式 / 单模式） |
|---|---|---:|
| `adaptive`（默认） | 各模式独立从 β=1、2 开始；官方 Makespan 严格下降才继续，最多 β=4 | 8 / 4 |
| `fixed` | 关闭组件，两种模式均只运行 β=1，选较好方案；消融时使用 | 2 / 1 |
| `grid` | 两种模式均完整枚举 β=1,2,3,4，作为对照 | 8 / 4 |

自适应使用快速规则：对模式 $m$，β=b 完整评价成功后，仅当 $b=1$，或 $T_{m,b}<T_{m,b-1}$ 时继续试 b+1；β=4 后结束。时间相等也停止，不因搬运量减少而继续。某模式停止不会停止另一模式，下一 β 无须等待另一模式返回；不会在早停后额外探测 β=4。

最终在所有已成功评价的候选中按 `(makespan, added_copy_bytes, mode_order, beta)` 择优。时间持平而搬运减少的候选仍可成为最终结果，但不会触发继续搜索。任何时候都保留已找到的最好方案，因此正常评价时自适应结果不会劣于本例同核数的双模式固定 β=1 基线。

若一个候选发生明确的官方调度失败，自适应停止该模式并保留其已有成功方案；β=1 失败时，该模式无成功方案。两种模式均未取得成功方案时才运行单操作后备。输入错误、程序异常向外传播。固定和枚举模式也仅将明确的官方调度失败记为候选失败。

这是按实例评价反馈选择参数的启发式组件，不假设时间关于 β 单调或单峰，不能保证找到四种 β 的全枚举最优值。做消融时，对同一批图、核数和配置分别运行 `fixed` 与 `adaptive`，只改变此开关；用不同输出目录保存两组结果。`grid` 可用于核对早停漏掉的收益，不能把候选数减少直接当作实测墙钟加速。

## 图与静态量：式 (1)—(5)

计算操作集合 $O_c$ 排除 `COPY_IN/COPY_OUT`。直接 op→op 依赖与张量中转依赖统一由官方函数构造，再收缩 COPY 中间节点。共同读取输入的操作之间不增加依赖。

记 $c_v$ 为原始计算周期，$s_t$ 为张量字节数，$p_t$ 为计算生产者（无则为空），$U_t$ 为不同计算消费者集合。张量若有原始 COPY_OUT 或没有计算消费者，标记为必须输出。索引保留 `In/Out/Pred/Succ`。按原始 `pipe` 区分 M/V，按张量 `pos` 区分缓存，不能按操作名称推测。直接与计算相连的 DDR 张量按官方 Task 构造转为 UB 副本。

当前模型要求非 COPY 操作属于 M/V 两条计算管道、张量至多有一个计算生产者，且单个操作的输入输出张量并集不超过相应缓存容量；违反时明确报错。100 个原始用例的计算管道和生产者结构满足前两项，容量条件仍逐次检查。零周期/零字节保留原值；搬运参考最少一周期，实际时长由官方解释。

\[
a(v)=c_v+\max_{u\in Pred(v)}a(u),\tag{1}
\]
\[
b(v)=c_v+\max_{w\in Succ(v)}b(w).\tag{2}
\]

所有空集最大值取 0。计算工作量为

\[
W_M(S)=\sum_{v\in S,\,pipe(v)=M}c_v,\qquad
W_V(S)=\sum_{v\in S,\,pipe(v)=V}c_v.\tag{3}
\]

模式依次为 `global`、`components`。前者以全部操作为一个范围。后者把计算依赖图的弱连通分量按 $\max(W_M,W_V)$ 降序、最小操作 ID 升序排列，装入 $h=\min(N,r)$ 个范围。分量 $C$ 选择字典序最小的

\[
\left(\max_{p\in\{M,V\}}(W_p(B_k)+W_p(C)),\ W_M(B_k)+W_V(B_k),\ k\right).\tag{4}
\]

分组只是候选范围，不绑定核心。只有一个分量时省略第二模式。范围按编号处理；可选粒度及其限制为：

\[
\beta\in\{1,2,3,4\},\qquad
H_\beta=\max\left(1,\left\lceil\frac{\max(W_M(O_c),W_V(O_c))}{\beta N}\right\rceil\right).\tag{5}
\]

默认各模式自适应选择 β；每个用例、每个核数最多 8 份候选，均从原图重新构造。`fixed` 模式仅使用 β=1。

## 就绪生长与费用：式 (6)—(18)

$F$ 是已封闭 Task 的节点，$S$ 是当前 Task，$B$ 是当前候选范围。

\[
R_B(F,S)=\{v\in B\setminus(F\cup S):Pred(v)\subseteq F\cup S\}.\tag{6}
\]

用未标注前驱计数维护就绪集合；接受后才递减后继计数。这是标注条件，不代表硬件已经执行或数据已驻留。

\[
T_{in}(S)=\{t:U_t\cap S\ne\varnothing,\ p_t\notin S\},\tag{7}
\]
\[
T_{out}(S)=\{t:p_t\in S,\ U_t\setminus S\ne\varnothing\text{ 或必须输出}\}.\tag{8}
\]
\[
q_t=\max(1,\lceil s_t/b_{DDR}\rceil),\tag{9}
\]
\[
Q(S)=\sum_{t\in T_{in}(S)}q_t+\sum_{t\in T_{out}(S)}q_t,\tag{10}
\]
\[
B_{copy}(S,v)=Q(S)+Q(\{v\})-Q(S\cup\{v\}).\tag{11}
\]

按张量而非消费边计数。共享输入只省去一次重复读取；内部产物只有在最后一个外部消费者也纳入且无需最终输出时，才省去生产侧写回。实现通过消费者计数和边界增量计算，测试另按集合定义直接重算。

\[
h_v(S)=c_v+\max\left(\max_{u\in Pred(v)\cap S}h_u(S),
\sum_{t\in In(v)\cap T_{in}(S)}q_t\right),\tag{12}
\]
\[
d_0(S)=\max\left(W_M(S),W_V(S),Q(S),
\max_{v\in S}\left[h_v(S)+\sum_{t\in Out(v)\cap T_{out}(S)}q_t\right]\right).\tag{13}
\]

有依赖的 M→V 沿路径累计时间，不能因使用不同管道就视为完全重叠。就绪追加不会改变之前节点的内部前驱和外部输入，因此已有 $h_v$ 可保留，但原节点的输出边界可能减少，需要更新跨度。

\[
K(S)=\{t:t\text{ 被 }S\text{ 触及，且 }U_t\cap(B\setminus(F\cup S))\ne\varnothing\},\tag{14}
\]
\[
m_\ell(S,v)=\sum_{\substack{t\in K(S)\cup In(v)\cup Out(v)\\pos(t)=\ell}}s_t,\tag{15}
\]
\[
\widehat M_\ell(S\cup\{v\})=\max(\widehat M_\ell(S),m_\ell(S,v)),\tag{16}
\]
\[
\widehat P(S)=\sum_{\ell\in\{L1,UB\}}2\left\lceil
\frac{(\widehat M_\ell(S)-C_\ell)_+}{b_{DDR}}\right\rceil,\tag{17}
\]
\[
\widehat d(S)=d_0(S)+\widehat P(S).\tag{18}
\]

虚拟保留集按当前范围中的未来消费者维护，每封闭一个 Task 就重置。虚拟峰值不是真实缓存峰值，压力项不是实际 spill 费用；$\widehat d$ 没有严格上下界保证，不能据此拒绝原问题的可行方案。

## 选择与封闭：式 (19)—(30)

新 Task 的首节点按下式字典序最小选择，并强制接受：

\[
(-b(v),r(\{v\}),\widehat d(\{v\}),id(v)),\tag{19}
\]
\[
r(S)=\max_{u\in(\bigcup_{v\in S}Pred(v))\setminus S}a(u),\tag{20}
\]
\[
H_S=\max(H_\beta,c_{seed}).\tag{21}
\]

后续先筛选

\[
R_{fit}=\{v\in R_B(F,S):W_M(S\cup\{v\})\le H_S,\ W_V(S\cup\{v\})\le H_S\}.\tag{22}
\]

从中各取至多 $K=8$ 个：后续路径最长、搬运复用最多、与当前 Task 有前驱关联且路径最长、属于较轻计算管道且路径最长。两条管道工作量相等时第四类为空。第二类按 $B_{copy}$ 降序，再按 $b$ 降序、ID 升序；其他类按 $b$ 降序、ID 升序。合并去重后每轮最多试算 32 个节点。

实现中，$B_{copy}$ 直接按候选输入张量计算：已读外部输入省一次读取，内部输入省一次读取，且最后一个消费者纳入并无需最终输出时再省一次写回。第二份短名单只对触及现有输入/输出边界的节点计算正复用收益；如果正收益节点不足 K 个，其余按 $b$ 排名补入的零收益节点已经包含在第一份名单里。因此合并去重后的候选集合与完整扫描四份名单严格相同；测试对照未过滤的原定义检查这一性质。

令 $S^+=S\cup\{v\}$：

\[
u_S(v)=\max_{w\in Succ(S)\setminus(S\cup\{v\})}b(w),\tag{23}
\]
\[
u_v=\max_{w\in Succ(v)}b(w),\tag{24}
\]
\[
\widehat f_S=r(S)+\widehat d(S),\qquad
\widehat f_v^{sep}=\max(\widehat f_S+\delta_s,r(\{v\}))+\widehat d(\{v\}),\tag{25}
\]
\[
\widehat f^{join}=r(S^+)+\widehat d(S^+),\tag{26}
\]
\[
H_{sep}(S,v)=\max(\widehat f_S+u_S(v),\widehat f_v^{sep}+u_v),\tag{27}
\]
\[
H_{join}(S,v)=\widehat f^{join}+\max(u_S(v),u_v),\tag{28}
\]
\[
\widehat\Delta(S,v)=H_{sep}(S,v)-H_{join}(S,v).\tag{29}
\]

按以下四项字典序最大选择：

\[
(\widehat\Delta(S,v),B_{copy}(S,v),b(v),-id(v)).\tag{30}
\]

最大收益非负则接受；范围耗尽、没有合粒度候选或最大收益为负则封闭。`TaskGrowth.trial` 不修改状态；`accept` 才更新成员、前驱计数、消费者计数、边界和虚拟缓存。构造记录保存加入序列、接受收益、封闭原因和最终参考量。短名单和负收益封闭都是启发式策略，不是最优性剪枝。

## 核内预分析、列表分配与正式择优

每份完整划分先按形成顺序全部排在核心 0，其他核心为空，调用官方 `_build_scene_a_tasks`，执行边界构造和 Step1/2/3，读取每个 Task 的 `step3.makespan` 为 $d_j^C$。

仅捕获明确的 `Step2SchedulingError` / `Step3SchedulingError` 做修补：将每个非单操作 Task 沿加入顺序在 `len(task)//2` 处二分，依次重新编号并预分析。最多 $\lceil\log_2\max_j|S_j|\rceil$ 轮到达单操作。正常 spill 保留；单操作仍失败则该候选失败。输入校验错误和程序异常向外传播，不通过拆分掩盖。

\[
b_j^{Task}=d_j^C+\max_{s\in Succ(j)}b_s^{Task}.\tag{31}
\]

每次从全部前驱已安排的 Task 中选 $b_j^{Task}$ 最大者，同分取较小子图 ID。试放到各核心末端：

\[
A_k=\begin{cases}0,&\pi_k\text{ 为空},\\
\widehat f_{p_k}+\delta_s,&\text{否则},\end{cases}\tag{32}
\]
\[
\widehat r_{jk}=\max\left(\{A_k\}\cup
\{\widehat f_i+\delta_c:i\in Pred(j),k_i\ne k\}\right),\tag{33}
\]
\[
\widehat f_{jk}=\widehat r_{jk}+d_j^C.\tag{34}
\]

选择 $(\widehat f_{jk},A_k,k)$ 最小的核心。来自不同跨核前驱的等待取最大值，搬运已包含在核内时间中，不再另加。后安排的独立 Task 可从另一空核的参考时刻 0 开始。

配置原值为 L1=524288、UB=131072 bytes，DDR=60 bytes/cycle，同核等待 100、跨核前驱等待 1000 cycles，均从配置读取。

随后对每个候选调用官方 `evaluate_scene_a`，按 `(makespan, added_copy_bytes, mode_order, beta)` 最小选择。正式评价重新执行官方核内与全局流程；没有复制或改写评估器，也没有通过 monkey patch 注入预分析缓存。预分析用完只保留持续时间，避免长期持有扩展图。

只有所选策略实际评价的正常候选全部发生官方调度/模拟失败时，才尝试按计算拓扑序、每个计算操作一个 Task、全排核心 0 的后备方案。后备仍须正式评价；失败返回非零状态。正常候选的程序错误不触发后备。

## 三项可行性保证及其边界

1. **就绪生长。** 新纳入操作所需计算结果只来自已封闭 Task 或当前 Task 已纳入部分，因此当前 Task 不会等待未来形成的 Task。候选范围包含完整独立计算分量，原图无环，只要还有节点就存在可标注节点；强制首节点保证持续前进。形成顺序因此满足计算依赖，并有限终止。
2. **预分析可先于分核。** 场景 A 不跨 Task 保留私有缓存，不论同核或跨核，边界数据都经 DDR；固定 Task 成员与编号就固定边界构造和核内展开。同构核心的归属只影响同核顺序、跨核等待和全局 DDR 竞争。二分修补保留加入顺序，前段也不会等待后段。
3. **列表分配。** 数据前驱和所选核心的前序 Task 都在当前 Task 之前完成离线安排，两类等待均指向更早安排的 Task，因此不能形成循环等待。最终仍调用官方组合依赖校验和模拟确认执行可行性。

这些保证不证明评分正确预测收益，也不证明初始化最优。只有通过官方评价的完整方案才是有效结果。

## 产物、指标与续跑

默认输出位于 `outputs/q1/initialization/`，每例每核数目录如下：

```text
case_001/N2/
  plan.json            # 可直接交给官方问题一评估器，严格只有两个字段
  construction.json    # 已评价候选、加入序列、修补、核内时长、分核参考量及搜索停止原因
  result.json          # 官方完整模拟结果
  result.log           # 官方格式简短日志
  trace.json           # 官方 Perfetto Trace
  metrics.json         # 汇总所需原始指标
  provenance.json      # 输入/配置/代码/参数及运行来源
  events.jsonl         # 带 UTC 时间戳的进度
  official.log         # 官方诊断 stderr
  complete.json        # 全部验证通过后最后写入的完成标记
```

默认归档模式不生成 `N1/`，只在汇总中记录其 Makespan 和 `baseline_ref`；原始单核搬运、Trace 等保留在公共归档。各核数的 `metrics.json`、汇总及来源指纹均包含基线引用。只有 `--recompute-baseline` 才在 `N1/` 保存新算的完整官方单核产物。单核基准与临时核心 0 排布不同；Task 的 `local_makespan` 也不代替官方总体结果。

最终提交文件写入后重新读取，完整调用官方评估并比较整个结果，完全一致才写完成标记。JSON 产物先写同目录临时文件再原子替换。失败保存 `failure.json`，没有成功标记；旧结果即使残留，也不能按成功结果续跑。

续跑同时验证内容指纹与关键产物 SHA-256。指纹包括图、配置、自有、复用的公共接口及官方 Python 源码、单核归档清单与结果引用、Python 版本与实现、核数、四种 β、搜索策略、停止规则、K、模式顺序及零收益规则。`fixed`、`adaptive`、`grid` 的完成标记不能互用，β=1,2,4 的旧记录也不能复用。绝对目录路径不参与指纹；基线引用使用仓库相对路径，便于迁移；Python 版本改变会保守重算。

`construction.json` 的 `search.modes` 记录每种模式的 `evaluated_betas`、`best_beta`、`best_makespan` 和 `stop_reason`；自适应各候选另有 `search_decision` 记录前一 Makespan、是否严格改善及下一 β。`metrics.json` 和汇总保存 `beta_search`、`evaluated_candidates` 和 `fallback_used`；候选计数不包含单操作后备，后备记录仍保存在候选列表末尾。列表按模式顺序和 β 升序保存，`selected.candidate_index` 对应该紧凑列表。

输出根目录的 `summary.json`、`summary.csv`、`average_speedup.csv` **仅统计本次命令要求的用例和核数**，每完成一个用例更新一次。另一次命令会更新这些汇总文件；需保留不同验收批次时使用不同 `--output-root`。用例目录中的独立结果仍然保留。

| 字段 | 单位和含义 |
|---|---|
| `makespan_cycles` | 官方总体完成时间，cycles |
| `original_graph_copy_bytes` | 原图 COPY 流量，bytes |
| `scheduled_copy_bytes` | 调度后总流量，bytes |
| `added_copy_bytes` | 总额外搬运，等于调度后总量减原图总量 |
| `partition_added_copy_bytes` | 切图新增搬运，bytes |
| `spill_added_copy_bytes` | 换入换出新增搬运，bytes |
| `task_count` | 完整执行的 Task 数 |
| `memory_peak_L1_bytes` / `memory_peak_UB_bytes` | 官方返回的各核心峰值之最大值 |
| `speedup` | 同例官方单核 Makespan / 多核 Makespan |

`cross_task_traffic` 不是总额外搬运，不用它代替 `added_copy_bytes`。平均加速比为逐例加速比的算术平均，单核定义为 1。汇总同时报告成功例数、可计算加速比例数、预期例数；失败、未完成或分母为零时不给虚构比值，明确标为部分结果。`elapsed_seconds` 是求解和验证的墙钟时间，与模拟周期不同。

原命令可附加 `--resume` 恢复；失败的用例或核数会重算，完整且指纹匹配的结果跳过。批量入口保留每例错误、继续其他用例，若本次汇总不完整则最终非零退出。

## 验收与性能边界

标准库测试涵盖公式独立重算、共享输入/扇出、无消费者输出、零字节、管道依赖、缓存位置与重置、试算隔离、β=3、确定性、二分修补、真实 spill、后备与异常传播、输入校验、空计算图、多进程 CLI、官方独立 CLI、续跑和产物篡改。随机检查使用固定种子的 24 个小 DAG，不能代替理论证明或全量性能实验。

初始化阶段已在 case_001 的 2–5 核、case_086 的 2 核及 case_087 的 5 核做有限验收，详见 [验收记录](acceptance.md)。没有执行全部 100 例多核初始化。团队已发布的 100 例单核基线是独立的既有归档。

短名单最多 32 个不意味着全算法为 $O(n\log n)$。当前仍扫描就绪集合；Task 跨度缓存已有最大值，仅当候选减少该最大值且新节点跨度不足以覆盖旧最大值时，才重扫 Task。该缓存不改变公式或选择规则，最坏仍可达二次量级。官方预分析和正式评价亦有独立成本。用例并行和候选并行不改变每个候选的计算过程，单个候选内部尚未多线程化。
