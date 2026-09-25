# 第一问第二版：服务器运行与续跑

本文区分 Git 仓库与 2026-09-25 的独立服务器运行包。**上传到 Git 不会替换正在运行的服务器程序**。仓库整合了第三版的共享图模型和入口，源码指纹已变化；不可直接把当前 Git 源码覆盖服务器目录并复用旧继承清单。

## 从 Git 仓库启动第二版

按仓库 README 准备 `official/data/case_*.json`，使用 Python 3.12 及以上。完整 V1 初值已经在仓库归档，可直接读取：

```bash
python src/solve_problem1.py improve-batch --initial-root results/q1_adaptive_100cases_20260925 --baseline-archive results/problem_1_singlecore --data-dir official/data --config official/data/config.txt --search-config configs/q1/stage2.json --cases all --cores 2 3 4 5 --jobs 8 --output-root outputs/q1_v2 --resume
```

`--jobs 8` 是示例，不是算法上限；并发按实际 CPU 配额、内存和候选评价成本选择。命令中的核数为模拟 NPU 核数。第二版单组合通常为单线程计算，多个 case 通过进程池并发；第三版的单次求解串行约定不限制第二版的批量进程数。不同并发下真实墙钟截断可能导致不同搜索路径，不应承诺限时成绩一致。

以下第 1–4 节保留历史运行包的路径与操作。`check_server.py`、`release.json`、`release.sha256`、`handoff.py` 是本地发布包的辅助产物，不在 Git 源码目录中；不要在普通克隆中直接执行这些包专用命令。

历史运行包包含当时审阅版本代码、官方程序及百例输入、第一阶段最终选中的 400 份初值（100 个 case ZIP）、完整公共单核基线及默认参数。仅依赖 Python 标准库，使用 Python 3.12 或以上。源代码保留原始字节，版本以 `release.json` 和 `release.sha256` 为准；不是已上传 GitHub 的提交版本。

## 1. 解压和检查

将运行包上传到服务器自己的实验目录，解压到新目录，不覆盖其他实验。以下命令均在解压出的 `q1_stage2_server/` 中执行，`python` 应指向所用 Python 环境。

```bash
python --version
python -B check_server.py
python -B -m unittest discover -s tests -v
```

`check_server.py` 不调用调度或搜索：检查运行包文件哈希、100 例输入、单核基线及 400 份初值内部产物哈希和来源，并独立复算阶段一平均加速比。它会流式读取压缩 Trace，不展开到磁盘。检查报告在 `outputs/server_preflight.json`；完整成功应为 100 例、400 份初值、100 份单核基线。测试仅使用小图与临时目录，不执行百例搜索。本次打包前的执行环境是 Windows；服务器上的这两步也用于确认 Linux 环境可用。

## 2. 开始正式计算

```bash
mkdir -p outputs
nohup python -u src/solve_problem1.py improve-batch \
  --initial-root results/q1_initial \
  --baseline-archive results/problem_1_singlecore \
  --data-dir official/data \
  --config official/data/config.txt \
  --search-config configs/q1/stage2.json \
  --cases all --cores 2 3 4 5 --jobs 1 \
  --output-root outputs/q1_stage2_full --resume \
  > outputs/q1_stage2_full.log 2>&1 < /dev/null &
echo $! > outputs/q1_stage2_full.pid
```

并行按用例分配进程；一个进程依次处理该例的 2–5 核。`--jobs 1` 是默认串行，确认分配给本实验的 CPU 和内存后，可以显式改为 `--jobs 4` 等。`--cores` 是模拟的 NPU 核数，`--jobs` 是同时工作的 Python 进程数。无需 GPU，不会并行运行两个初始化模式或重新选择 β。

不要同时启动两个写入同一输出目录的批处理。若重复实验或更改源代码、参数、输入，请使用新输出目录。不要修改运行中的文件。

每例每核数默认搜索 1800 秒，每个局部域 120 秒、256 次成功完整评价的兼容预算计数上限。2026-09-26 的实现优化保留原四项缓存规则，新增二级缓存命中仍消耗预算名额；`official_evaluations` 是实际成功模拟次数，`budget_evaluations` 是预算计数。总计 400 个搜索任务，搜索预算名义合计 200 小时；这不是墙钟预测。初值完整评价、最终独立复评另计，已开始的官方调用允许结束，诊断/生成候选也未设置强制中断，因此 014、076、091 等慢例可能超出 30 分钟。单域失败评价未计入 256 次上限的已知限制见审阅文档。

## 3. 查看进度与恢复

```bash
tail -n 40 outputs/q1_stage2_full.log
```

每个正在处理的组合都有 `events.jsonl`，每 10 秒输出阶段心跳。心跳只表示进程仍在相应阶段，不等于算法有进展或已经完成；只有最终 `verified` 和 `complete.json` 表明该组合通过原版官方复评。

`--resume` 跳过同指纹且产物哈希有效的已验收组合；预算结束但正式验收通过的组合也会跳过，不继续搜索栈。进程崩溃留下的非空、未验收目录不会自动覆盖：确认相关进程已经退出后，把仅有的未验收 `case_XXX/Nn` 目录移至独立的中断备份目录，再用原命令继续。这会重新处理该组合；其他已验收组合保留。若要接续最近接受的方案，可将备份中的 `checkpoint_plan.json` 作为单例 `--initial-plan`，配合新的输出目录；这属于新的运行，不是恢复原分支栈。

本版本不应直接把第二阶段输出目录中的 `plan.json` 再作为初值：会触发阶段一归档格式检查。需要继续优化时，使用上述 `checkpoint_plan.json` 或复制为新目录的独立方案文件。已知边界统一见 [验收记录第 8 节](stage2_acceptance.md#8-历史审阅发现与保留边界)。

## 4. 加速比和结果

每例自动读取同输入、同配置、同官方版本的整图单核时间，不重新运行单核。对用例 i 和核数 N：

`speedup[i,N] = singlecore_makespan_cycles[i] / makespan_cycles[i,N]`

每个核数的平均加速比是逐例比值的算术平均，不是时间总和之比。阶段二相对阶段一的时间减少比例另存为 `improvement_ratio`，不要将其与单核加速比混淆。时间均是官方模拟周期，不使用 Python 运行秒数作为分子或分母。

输出文件：

- `summary.json`、`summary.csv`：逐例、逐核数的官方时间、搬运分解、阶段一初值、改善及单核加速比；包含 100 条单核参考行和 400 条优化结果行。
- `average_speedup.csv`：各核数平均加速比、成功数、预期数及 `complete/partial`。正式百例结果要求 N=2、3、4、5 每组均为 100/100。
- `case_XXX/Nn/metrics.json`：单组合指标与单核基线引用。
- `case_XXX/Nn/plan.json`、`result.json`、`trace.json`：最终方案、官方完整评价及时间线。
- `case_XXX/Nn/optimization.json`：诊断、候选、剪枝和接受历史。
- `case_XXX/Nn/complete.json`：最终独立复评及文件哈希；`search_status=INCOMPLETE` 允许与验收通过同时存在。
- `case_XXX/Nn/attempt_failure.json`：失败原因；单例失败后继续其余组合，批量最终返回非零状态。

汇总随整个用例的 2–5 核处理完成而更新，个别核心已经完成时可先看其 `metrics.json`。不要把尚未汇总的组合误判为未运行。全量结束后执行：

```bash
python -c 'import json; r=json.load(open("outputs/q1_stage2_full/summary.json")); print("complete:",r["complete"]); [print(x) for x in r["average_speedup"]]'
```

`pruned_candidates` 在当前版本混合了分支和完整方案排除记录，不能用于宣称节省了同样数量的官方评价；细分分析应按 `optimization.json` 的 `scope` 分开统计。

## 5. 实现优化版接手旧任务（2026-09-26）

直接替换源码后使用旧目录 `--resume` 会因源码指纹变化而拒绝旧结果。升级应使用独立代码目录和输出目录，保留旧代码、日志和全部结果。批处理新增可选参数 `--inherit-completed inheritance.json`，仅在显式指定已验证版本对并同时使用 `--resume` 时启用：

```json
{
  "previous_output": "/absolute/path/to/old/outputs/q1_stage2_full",
  "previous_solver_sha256": {"src/...": "old SHA-256"},
  "current_solver_sha256": {"src/...": "new SHA-256"}
}
```

清单必须包含 `source_identity` 涉及的完整源码哈希映射。继承逐项校验图、配置、阶段一来源、单核基线、搜索参数、官方代码、Python 版本、新旧源码、完成标记和全部产物哈希；任何已完成组合校验失败都会在启动搜索前终止。只接受 `official_replay_verified=true` 的结果。`search_status=INCOMPLETE` 是正常的限时搜索结束状态，不等于缺少完成标记。

通过校验的完整目录复制到新输出中，原 `provenance.json`、`complete.json` 及其哈希保持原样，增加 `inherited.json` 记录来源。汇总中的继承行增加 `inherited`、`inherited_from`、`producer_fingerprint`。旧结果不会伪装成新源码的结果。后续重启保留相同继承清单和旧目录；新版本已经完成的组合继续走原有 `--resume` 校验。

正在搜索的组合没有完整的搜索栈检查点，不能逐候选恢复。其旧目录保留供分析，新程序从原阶段一方案重新处理该组合，使用原搜索预算。单独拿 `checkpoint_plan.json` 重新启动会改变起点和预算，不属于本次等价续跑方式。粒度是 `case/N`，已完成 N2/N3 时只补 N4/N5。

本次交付的小型升级包包含一次性部署辅助脚本 `handoff.py`，不引入新的算法模块。`prepare` 验证并复用服务器上的不可变输入，复制已完成结果，写出进程及进度报告；不会停旧进程或启动搜索。`start` 必须在旧主进程和工作进程都停止后执行，重新扫描最终完成标记，再启动原批处理入口；发现旧进程或已有新进程时拒绝启动。不要同时启动两套全量任务，也不要修改共享的输入文件。

本地验证：109 项测试通过，覆盖混合版本汇总、重复续跑、未完成目录保留、损坏产物、未授权源码、参数变化及输出冲突。实际服务器进程停止和启动须以服务器日志为准。
