# Q2 2.0：顺序保真、分量局部性与容量修复

默认版本为 `Q2-Repair-2.0`，Python 3.12，仅标准库。默认配置是
[`baseline.json`](../../configs/q2/baseline.json)。[1.1 对照配置](../../configs/q2/legacy_1_1.json)
保留原自适应规则与峰值增强，用同一个构造内核复现旧版。

原1.1版本已标记为存在已知问题并[完整归档](../../archive/q2/baseline_1_1_known_issues/README.md)；
归档保留其原始源码字节和提交身份，当前默认入口使用2.0修复版。

本次依据对话 `6ab6d2a7-4f50-83e9-bdd3-34fa007ab292` 的最终修复约定，
以及两份修复包的共同规范和参考实现。参考代码中的 trace 重建、无硬超时的评价驱动、
全结果内存缓存不作为生产实现；旧构造快照也没有复制进源码。
官方程序、固定配置、Q1 三版算法和公共调度框架均未修改。

## 表示、目标与流程

记计算节点集为 V，计算节点 v 的核心为 k(v)，构造器成功 commit 的全局拓扑序为 σ。
`ConstructionDraft` 保存稠密索引下的 `operation_core`、`commit_order`、原逻辑
`search_blocks`、旧编码和代理诊断。搜索块是后续邻域的结构单位，不是执行屏障，
也不要求与提交 sgid 一一对应。关闭子图只重置当前生长块，不重置核心时钟与持续状态。

一次运行依次执行：共享 DAG 索引 → 两个整分量候选 → 联动构造的各 β／编码流 →
已正式评价的最好 B0 → 必要的容量修复候选 → 正式指标择优 → 文件重读与独立官方复评。
正式目标始终为 `(makespan, added_copy_bytes)`，严格字典序改善才替换 incumbent；
完全相同保留先到方案。代理 score、Step1 驻留峰值都不冒充正式时间。

### 三种联动构造编码

* `legacy`：原子图成员及核心列表，接受官方桶内重排；日志记录与接纳序的首次差异。
* `commit`：按 σ 中位置给每个计算操作独立 sgid，逐核列表为 σ 的核心投影。
* `tail`：保持核心映射，按就绪节点 `(-b(v), 原始ID)` 生成全局拓扑序，再进行独立 sgid 编码。

其中 b(v) 是现有图索引的后续计算路径优先量。编码后检查
`ComputeProjection(Step1(plan)[k]) == intended_order|k`。
这同时确定各计算 Pipe 的投影，但不保证代理 COPY 时序、空间复用依赖或共享 DDR 时钟准确。
本轮没有做 sgid 压缩。旧编码获胜时，搜索窗口按实际 Step1 和核心队列重建连续块。

### 整分量分核

忽略计算生产—消费边的方向获得弱连通分量 C；共享原始输入不连通两个计算分量。
W_M(C)、W_V(C) 分别是分量中两种计算 Pipe 的周期总和。按
`(-max(W_M,W_V), 最小原始操作ID)` 排序，给 C 选择以下元组最小的核心 k：

```
(max(L[k,M]+W_M, L[k,V]+W_V),
 L[k,M]+W_M+L[k,V]+W_V,
 k)
```

L 表示已经分配的计算负载。分量整体固定在选中核心，内部不会再次逐节点换核。
同一映射产生每分量一个子图的 `block` 候选和保真 `tail_atomic` 候选，β 记为 null。
因此此家族没有计算中间产物的跨核 COPY 对；原始输入读取和最终结果写出仍然存在。
联动生长家族仍允许拆分大连通图，不把整分量同核强制用于所有候选。

### β 的独立反馈

每条流的身份是 `(family, mode, encoding)`。默认各流轮转访问 β=1,2,3,4：

* T(β+1) ≤ T(β)：继续；T 是本流相邻 β 的官方 makespan。
* T(β+1) > T(β)：停止本流，保留历史最好正式目标。
* 同时间搬运更多仍继续，缓存命中的相同计划也反馈持平。
* INVALID 清除比较起点，下一成功 β 重新建立起点。超时／资源失败结束本流并标记不完整。
* 预算结束与 β 上限分别记录，不声称未访问的参数没有改进。

`fixed` 只访问 β=1，`grid` 访问全部配置档位。`strict` 继续规则只用于单因素对照。
同一 `(mode,β)` 共享一个草案，同一完整方案共享成功官方结果，但编码保真检查不能被缓存跳过。
默认至多 `2×4×3+2=26` 个基础请求；不承诺只有两次全局评价。
这次修改没有改变 Q1 的 β 规则。

## 容量修复 DP

先读官方 `spill_added_copy_bytes`。无实际 spill 则跳过。对仍需检查的核心，
取真实 Step1 序列，令 f_t、l_t 为本核张量 t 的首次／末次触及位置，s_t 为字节数。
COPY_OUT 也触及其输入。位置 i 的无换出驻留量是

```
M_p(i) = sum(s_t for t in memory p if f_t <= i <= l_t), p in {L1, UB}.
```

闭区间包含当前操作的输入尚未释放、输出已经申请的瞬态。这是固定序列的容量证书，
不是全局并发时间线。已经满足两类容量的核心不排序。

从真实连续核心队列重建逻辑块，原逻辑块遇到其他块则分段；单操作编码下每段至多32个
计算操作。已有不可分 sgid 即使超过32个操作也不拆开。最多8个连续块组成窗口，默认最多8个窗口域。
固定块内、窗口外和其他核心的次序；全图依赖路径及两侧锚点保留，只释放窗口内可调块间的旧队列顺序。
动态输入 COPY_IN 归入新顺序中最早消费它的 sgid，而不是一律放到块头。

以 X 表示已放置的块集合，d[X] 是最小逆序对数。若 j 的前驱已经在 X 中，且精确扫描
本次桶事件的 L1、UB 峰值均不超容量，则

```
d[X union {j}] = min(d[X union {j}], d[X] + popcount(((1 << j) - 1) & ~X)).
```

同值按块编号路径字典序取解。集合 X 唯一决定触及计数、驻留和动态 COPY 是否出现，
因此此 DP 对固定窗口的容量可行最小扰动是精确的；X 不唯一决定 Pipe 时钟，不能声称时间最优。
入口驻留超限或全部排列不满足容量时返回 `NO_FEASIBLE_ORDER_IN_FIXED_WINDOW`，不判定原图非法。

所有候选基于同一个正式 B0，重建 Step1 并与预测的完整含 COPY 序列比较，核图、跨核链接、
其他核心序列必须不变。整核曲线再次检查，窗口外超限仍可存在。每个完整候选接受官方评价，
最终仅保留正式目标更好的一个；不叠加未正式确认的局部变化。

## 组件接口与产物

| 接口 | 含义 |
|---|---|
| `construct.construct_candidate(...)` | 一次联动生长，返回不可变 ConstructionDraft |
| `encoding.encode_candidate(...)` | 草案派生独立 EncodedCandidate |
| `Settings.prefix(...)` / `encoding.validate_fidelity(...)` | Step1 专用展开与当前编码的顺序核验 |
| `affinity.component_candidates(...)` | 两个真正整分量同核候选 |
| `candidates.capacity_repair_candidates(...)` | 冻结 B0 上的容量候选生成器 |
| `candidates.evaluate_candidate(service, candidate)` | 共享预算、缓存、落盘评价服务 |
| `PlateauBeta.next()/observe()/state_dict()` | 流调度、正式反馈与可序列化状态 |
| `pipeline.load_baseline_result(directory)` | 核验文件哈希后加载 BaselineResult |

`BaselineResult` 保存方案、官方结果、所选草案、实际搜索块、Step1 与最终官方时间线、
候选摘要和 β 轨迹。JSON 属性返回独立副本；后续优化器可以修改核心、块和顺序后调用同一套组件。
没有注册未实现的 Q2 公共后端，也没有实现下一轮基座专用优化主循环。

正式目录继续提供 `plan.json/result.json/trace.json/run.json/provenance.json/complete.json`，
新增 `baseline_state.json/beta_state.json`，保留构造草案和每次官方调用的证据目录。
候选结果路径相对正式目录，原子发布后仍有效。官方成功结果缓存只存目标、摘要和磁盘路径，
不同时保存所有完整模拟结果。候选对象不共享可变计划。

输入、固定配置、官方代码、Q2 源码、完整方案和算法版本均进入缓存或运行身份。
保留硬超时、回收本次子进程、输出锁、原子发布和最终全字段重放检查。
预算默认每 case/N 600秒，初始预留60秒独立复核；worker 默认1。
`--resume` 是组合级已验证结果复用，不恢复任意中断指令；异版本或篡改产物不能复用。
`SUCCESS_VERIFIED` 只表示已发布方案经复核，`search_complete` 另行表示搜索策略是否完成。

旧代理的历史最大项、源 MTE3 追加近似、全局剩余消费者驻留估计保留并明确标注，
本轮不同时重写这些评分公式。关闭原因、工作量/上限、代理 Pipe 时钟与峰值保存在草案中。

## 运行与验证

```powershell
# python 指向 Python 3.12；原始图目录可用实际本地路径替换
python src/solve_problem2.py solve official/data/case_086.json -n 5 --output-dir outputs/q2_v2_one

# 默认仅跑 086/N5 修复版，用于另一台机器的入口检查
python scripts/validate_q2_repair.py --data-dir official/data --output-root outputs/q2_repair_validation

# 可选：32组旧版与修复版；本轮本地已提前停止，不要求现在补齐
python scripts/validate_q2_repair.py --suite acceptance --data-dir official/data --output-root outputs/q2_repair_validation --resume

# 可选后续研究：4个代表组合 × 4种单因素修复；本轮未执行
python scripts/validate_q2_repair.py --suite ablations --data-dir official/data --output-root outputs/q2_repair_validation

# 只整理已完成结果；历史结果仅在这个离线步骤读取
python scripts/validate_q2_repair.py --report-only --output-root outputs/q2_repair_validation --reference-zip "问题2_修复规范与参考代码_轻量包.zip"

# 100×4正式运行入口，本次没有执行
python scripts/validate_q2_repair.py --suite full --data-dir official/data --output-root outputs/q2_repair_full --workers 1

# Windows 父子进程统一编码，仅设置当前 PowerShell 会话
$env:PYTHONUTF8 = '1'
python -m unittest discover -s tests
```

消融配置为 `ablation_order/affinity/beta/capacity.json`，分别只改变顺序候选、整分量候选、
β 持平继续、spill 门控与容量 DP；构造公式不变。每组仍按正式指标选优。
输出中的实际墙钟时间包含工程监督、文件读写与最后复评，不能直接与附件 Linux 多进程耗时比较。
`wall_seconds` 在最终元数据与快照导出前采样；RSS 是进程生命周期峰值，同一 worker 连续处理多个
组合时可能继承此前高水位，不能作为各用例独占的峰值内存。2.0 的 Step1 总开销使用
`stages.step1_profile_seconds`，旧 `c1_profile_*` 字段只计量1.1路径。
本轮按用户最新要求仅保留部分本地验证；消融只用于后续归因，不作为当前工程交付的前置条件。
本轮实际成绩、参考复现差异和局限见[验收报告](repair_validation.md)。
