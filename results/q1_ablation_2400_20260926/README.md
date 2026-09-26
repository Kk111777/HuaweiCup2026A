# 问题一基座算法：2400 行消融明细

本目录归档已验收的 **100 案例 × 2/3/4/5 核 × 6 版本 = 2400 行**实验明细。CSV 与 JSON 保留原始导出字节，没有删列或重新计算指标；SHA-256 见 [archive_manifest.json](archive_manifest.json)。每核 600 行，每核每版本 100 行，唯一键为 `(case, num_cores, variant)`。

- [details.csv](details.csv)：含表头，共 2400 条数据，适合表格和数据分析工具。
- [details.json](details.json)：2400 个逐例对象，保留 JSON 数值与布尔类型。

## 版本和指标

| variant | 含义 |
| --- | --- |
| full | 原基础初始化算法全部组件开启 |
| no_structure | 仅全局就绪生长，关闭独立计算分支初始化 |
| no_communication | 关闭 B_copy 优先收益及复用候选短名单/次级排序，保留真实搬运评价 |
| no_cache | 构造时缓存压力启发式归零，保留官方容量检查及 Spill |
| no_adaptive_beta | 原 fixed 策略：两种初始化只评价 β=1，再择优 |
| no_smart_core | 固定对应 Full 切图，按最小就绪 Task 编号、最小累计官方核内时长分配；同负载取小核号 |

`makespan_cycles` 为官方最终事件模拟周期数；`speedup = singlecore_makespan_cycles / makespan_cycles`。`added_copy_bytes` 包含划分额外搬运和 Spill，单位为 bytes；换算 MiB 时除以 1,048,576。`task_count` 为最终方案 Task 数。

`solve_seconds` 为候选构造和择优墙钟时间，包含图/配置读取、索引和进度日志回调；最终产物写出和独立官方验收不计入。`validation_seconds` 为独立验收时间。Smart-Core 行满足 `solve_seconds = shared_full_solve_seconds + reschedule_seconds`，不能把重分核耗时当作完整求解成本，也不能将共享 Full 成本重复计入整批耗时。CPU 时间另见对应 `*_cpu_seconds` 列。

实验使用 20 个 worker，`candidate_jobs=1`；运行中启用每 worker 单核绑定。异构 CPU、并发竞争及绑定时点会影响墙钟时间。本批次曾发生输出替换异常，经来源和产物校验后续跑；这些时间并非统一独占硬件的性能基准。时间戳和 PID 为运行溯源字段。

## 验证与范围

原实验已完成 2400/2400 独立官方回放、400/400 Full 历史回归及 400/400 Smart-Core 精确分区配对。归档时重新核对完整组合覆盖、CSV/JSON 单元格一致、加速比/搬运分项/配对计时关系，并确认两个文件与最终交付清单中的哈希一致。本次上传没有重新求解实验。

这是原基座算法的历史实验数据，**不代表本分支最新 main 中其他模型版本的结果**。生成算法的源码快照哈希、实验清单哈希及原本地交付清单哈希保存在归档清单。本目录仅发布明细与说明，完整求解器、逐份方案、Trace 和运行日志仍在原本地实验包中。

在本目录执行以下只读校验可检查文件内容：

```python
import hashlib
import json
from pathlib import Path

manifest = json.loads(Path("archive_manifest.json").read_text(encoding="utf-8"))
for name, info in manifest["files"].items():
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == info["sha256"]
assert len(json.loads(Path("details.json").read_text(encoding="utf-8"))) == 2400
```
