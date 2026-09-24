# 问题一单核 baseline：100 例结果归档

这里保存 2026-09-23 已完成的正式运行结果。**100/100 成功**；评测指标为结果 JSON 顶层的 `makespan`，单位 cycles。每个 case 模拟一个 NPU 核心、一个 Task；`--workers` 仅控制电脑上同时执行的评估进程数。

- [全部 100 例汇总](summary.csv)
- [原始运行与续跑记录](run_info.json)
- [本次上传核验记录](validation.json)
- [原始产物 SHA-256 清单](manifest.sha256)

每个 `case_XXX/` 包含官方格式的 `*_singlecore_res.json`、`*_singlecore_trace.json` 和 `*_singlecore_log.txt`，共 300 份。另有汇总和运行记录，原始有效产物共 **302 份、492,114,994 bytes**。未归档运行时锁 `.run.lock`。文件内容与 `outputs/problem_1_singlecore/` 中原始文件逐份 SHA-256 一致；归档未重新计算或改写成绩。

参考：`case_001=233110` cycles，`case_003=602212` cycles；共有 **40** 例产生 Spill。前 57 个成功用例来自串行阶段，后 43 例以 8 个进程续跑，续跑阶段约 466.793 秒；此秒数是计算机运行时间，不是模拟器 cycles。

## 来源与核验

`run_info.json` 保留当时真实的解释器路径、来源哈希和两次调用记录；其中 `output_dir` 是原运行位置，当前 Git 归档位置为本目录。最初串行入口的旧版本哈希作为历史记录保留，归档程序与最终续跑记录中的入口哈希一致。

本次检查全部 100 份输入哈希、10 个官方模块和配置哈希、入口哈希，核对 100 行汇总与结果、Trace、日志，并逐项核对 Trace 的操作 ID、Pipe、起始时间和持续时间、同 Pipe 操作不重叠。具体范围见 `validation.json`。这次上传未重跑全量调度；现有 17 项基线测试已通过，其中包含真实最小图官方执行与串行/并行一致性验证。

## 复现与恢复

按[根目录说明](../../README.md)从比赛附件准备 `official/data/case_001.json` 至 `case_100.json`。仓库包含官方程序和配置，原始计算图、附件 ZIP 和赛题 PDF 需从同一比赛附件获取；可用 `run_info.json` 的输入 SHA-256 确认版本。

```powershell
# 全新运行，写入未使用过的目录
python -X utf8 src/problem_1_baseline.py --workers 8 --output-dir outputs/singlecore_reproduction

# 单核程序与集成测试；Windows 也可用 .\.venv\Scripts\python.exe
python -X utf8 -m unittest discover -s tests -p test_problem_1_baseline.py -v
```

从仓库根目录执行上述命令。完整归档保留为参考；如需使用 `--resume`，将本目录中的 `summary.csv`、`run_info.json` 和 100 个 case 目录复制到一个新的本地输出目录，再指定该目录续跑。不要直接以本归档目录作为运行输出，以免改写历史运行信息。
