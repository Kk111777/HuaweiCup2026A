# 第三版结构驱动求解与模块化发布验收

> 本页记录初次推送提交 `7bcfbc0` 的验收，源码指纹及198项通过结果保持原样。之后合并前修正了近似新增搬运量及报告口径；最终状态和新验证见 [merge_review.md](merge_review.md)。本页不能替代最终版本的验收记录。

本次更新问题一第三个模型“基于宏观结构感知的候选扩展与联合优化算法”。生产求解不再前置运行官方单核或用其成绩安排全图策略。图依赖、计算与搬运负载、分量和内存压力决定结构策略，单核归档只用于求解后的加速比报告。一张图的2/3/4/5核共用600秒目标，延长与保护机制保留。

公共编排移至 `src/scheduling`，Q1由四组接口绑定，旧Python和CLI入口保留。后续问题三需要实现相应问题语义、执行与预测、候选模块，再显式注册。当前没有可运行Q3后端。设计与调用见 [unified.md](unified.md) 和 [interfaces.md](interfaces.md)。

本次代码发布从 main 的 `dac7d0d` 建立分支，保留已合入的Q1第二版、Q2和官方程序。34个迁入的代码、测试和运行工具文件与已经验收的本地版本仅可能有LF换行及一个文件末尾空行清理的差异，后者已检查Python语法树一致。没有借发布改动候选接受公式、修复预测微小收益门槛或调整官方/近似评分规则。

## 本次发布目录的复验

- Python 3.12.14：199项测试，198项通过，1项因发布目录没有原始比赛附件ZIP而跳过。覆盖Q1旧入口、整case预算、问题接口、配置覆盖、独立spawn子进程，以及main已有的第二版和Q2回归。
- 从改造前快照到发布目录的16组固定搜索量差分全部一致：合成图 `tiny_independent`，2/3/4/5核，official/adaptive两种模式，standalone/shared_case两种预算作用域。
- 差分覆盖552个已调度候选、92条官方记录、56条近似记录、14次近似接纳和68次校准。固定逻辑时钟和搜索轮数，用于检查行为保持，不测量墙钟性能或证明所有输入等价。
- 从公共CLI对真实case001运行四种核数，共享20秒目标预算。求解摘要的 `baseline_seconds=0`，没有单核基准求解。4份最终方案均用原版官方CLI独立重放，除输入文件名元数据外，全字段与保存的官方结果一致。

| Case | 核数 | 官方Makespan（cycle） | 官方总额外搬运量（byte） |
|---|---:|---:|---:|
| case001 | 2 | 116868 | 1152 |
| case001 | 3 | 78545 | 2304 |
| case001 | 4 | 58984 | 3456 |
| case001 | 5 | 47502 | 4608 |

机器可读记录、方案与官方结果SHA-256见 [modular_validation.json](modular_validation.json)。本次发布代码指纹为 `89d285300a0571450b0e3d2be6f5911a242423640fc3d5fd5ac2351f50232e5d`。运行指纹覆盖整个src、相关配置和官方代码，不是Git提交号；合入main已有的其他模块和规范化换行也会改变指纹，不能将旧成绩重标为当前指纹的运行。

## 两次既有本地验收的范围

整case预算版本的5个耗时案例为014、025、056、077、095，每例覆盖2/3/4/5核，共20份最终方案通过独立官方重放。其求解指纹为 `83e84693897a72ee11ae9a0da49588d7d45dd2c1df12227d435cbe99e49f8d7a`。配对参照为此前统一管线100case中的对应组合，指纹为 `7260a7220dcf6b356bde08e97e6c02cf4e30983f3ad8e87f7e3a1ef06f183818`。

以下只展示配对变化。搬运变化按同组字节数求和后计算，加速比按相同case的加速比取算术平均。

| 核数 | 平均加速比变化 | 额外搬运量合计变化 |
|---|---:|---:|
| 2 | +0.12% | +88.86% |
| 3 | +0.47% | +69.37% |
| 4 | +0.01% | +83.87% |
| 5 | −0.93% | +71.36% |
| 20组汇总 | −0.17% | +78.08% |

平均整case求解10.33分钟，合计时长变化−77.22%；3例超过600秒建议目标，全部未超过900秒，无硬保护超时。时间比较已去掉历史单核补计成本，离线独立重放另外统计。新旧本地并发为5/20，不能将墙钟变化当成严格相同CPU负载下的效率结论。

额外搬运增量主要来自case014，各核数增加100.16%～144.17%；其余四例合计减少551228 byte。这20组新结果的缓存换入换出搬运均为0，但切分重建及重复输入搬运增加得更多。执行周期表现接近不代表次级目标不退化。case056/4核、case077/5核的官方周期分别变化+2.03%、+3.30%；后者还存在近似微小收益误排，尚未在此次发布中修复。

随后模块化验收的本地指纹为 `039de63c282a3c9debbe4513f7be6cf711199b47860c2de800a528618a56d500`。当时125项测试中124项通过、1项跳过，31个受保护算法/官方/默认配置文件逐字节不变。84组改造前后差分覆盖3684个候选、479条官方记录、274条近似记录、54次近似接纳和334次校准。

该84组差分包括3种合成图与真实case001/006的四核数、两模式、两作用域组合；case056仅覆盖official/standalone四核数，其他场景停止或未运行。另有case001/006/014/056共16份共享60秒目标的真实输出通过独立官方重放。缩减或中断的场景不计为通过。

这两次本地验收的摘要及来源哈希保存在本页JSON中，完整输出仍在本地实验目录，未随代码上传。本次没有重新完成100case或600秒全量性能测试。

## 复现入口

在仓库根目录准备同版本官方输入与Python3.12。Windows统一父子进程UTF-8，避免中文CLI帮助被不同默认编码解码。

```powershell
$env:PYTHONPATH='src;tests'
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
python -B -X utf8 -m unittest discover -s tests -q

python -B -X utf8 -m scheduling official/data/case_001.json --problem q1 `
  --config official/data/config.txt --cores 2 3 4 5 --seconds 20 `
  --output outputs/publication_smoke/case_001

python -B -X utf8 tools/audit_unified.py --run outputs/publication_smoke `
  --replay --require-current-source --local-test-workers 1
```

冻结快照不随发布上传。测试套件中的快照差分测试需要本地安装 `outputs/modular_engineering_20260926/before.zip` 及 `before_manifest.json`，否则该测试会额外跳过。已有快照时也可显式运行：

```powershell
python -B -X utf8 tools/verify_modular_equivalence.py `
  --before path/to/before.zip --manifest path/to/before_manifest.json `
  --after . --cases tiny_independent --cores 2 3 4 5 `
  --modes official adaptive --scopes standalone shared_case `
  --output outputs/publication_equivalence
```

公共接口通过Q1测试不等于Q3正确性验收。近似输出的结构合法性也不等于官方内存可行性；论文正式周期和搬运量应读取最终方案的独立官方结果，不能把预测分数改标为官方值。
