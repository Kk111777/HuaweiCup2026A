# 原版赛题与数据 · Original Problem and Data

[返回首页](../README.md) · [English overview](../README.en.md) · [文档索引](README.md)

本仓库保存华为杯 A 题《通用神经网络处理器下的多核调度问题》的原版题面和建模计算输入。PDF、计算图、配置和原有官方代码保留原始字节；不重新排版题面、重写 JSON 或修改官方参数。

The original statement and modeling inputs are included in the repository. The PDF, graphs, configuration, and existing official code retain their original bytes. Run the verification snippet below from the repository root before reproducing results.

## 材料与来源

| 材料 | 仓库位置 | 范围 |
|---|---|---|
| 原版题面 | [题面 PDF](../official/通用神经网络处理器下的多核调度问题.pdf) | 原文件名，811,735 bytes |
| 计算图 | [official/data/](../official/data/) | `case_001.json` 至 `case_100.json`，共 252,791,457 bytes |
| 原始参数 | [config.txt](../official/data/config.txt) | 341 bytes |
| 官方程序与说明 | [官方入口](../official/README.md) | 与原附件中的 14 份代码／配置／说明一致 |
| 输入校验清单 | [inputs.sha256](../official/inputs.sha256) | 102 项：PDF、100 个 JSON、配置；路径相对于仓库根目录 |

计算图和配置来自原始“通用神经网络处理器下的多核调度问题  附件.zip”；原包 SHA-256 为 `3112331344df7dcf2dcef2ff5f789669c0419e32cefbbc6e2742b9e36d5c43c9`。PDF SHA-256 为 `0f9d7e2a004142951889cf19376840047180a1b46989fd610bde359cb81fb0fb`。本次入库逐份与原包核对，并与已发布 Q2 Repair、Q2 v3 和 Q3 归档中的 100 例输入指纹核对一致。

已直接提供题面 PDF 和展开后的输入，因此不重复保存 DOCX 或整包 ZIP。现有官方代码和说明不作修改，解读文档置于 `docs/`。

## 工作负载与单位

- `ops`：操作 ID、名称、执行 Pipe 与计算周期。`pipe` 决定所用硬件管道，不能仅从操作名称猜测。
- `tensors`：张量 ID、存储位置及字节数。张量与操作共享唯一 ID 空间。
- `edges`：带 `source` / `target` 的有向依赖。
- `config.txt`：L1/UB 容量、DDR 带宽、Task／跨核 COPY 等待、Q3 L2 容量与带宽。

每份 JSON 是一个调度实例，不是模型权重或训练数据；案例编号不表示已识别的神经网络架构。周期由模拟器计算，主机运行秒数另行记录。完整合法性及官方流水线见[官方说明](../official/README.md)。

## 逐字节校验 / Byte verification

在仓库根目录，将下面代码交给所用 Python 解释器执行。所有路径均来自已发布清单，不依赖原机器目录；校验只读取文件，不修改材料。

```python
import hashlib
from pathlib import Path

root = Path.cwd()
manifest = root / "official" / "inputs.sha256"
entries = manifest.read_text(encoding="utf-8").splitlines()
assert len(entries) == 102, "Unexpected input manifest size"
for line in entries:
    expected, relative = line.split("  ", 1)
    actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    assert actual == expected, f"SHA-256 mismatch: {relative}"
print("Verified 102 original files.")
```

Existing Git attributes disable line-ending conversion for `official/**`. Keep that rule when copying or checking out the inputs. A mismatch should be investigated before evaluation; do not regenerate hashes to conceal changed data.

直接调用原版评估脚本时，请用 `-o`、`--trace-output` 和 `--log-output` 分别将结果、Trace 和日志写入新的 `outputs/` 子目录；只设置 `-o` 不会改变其他产物的默认位置。输入目录旁生成的结果、Trace 和日志已由 Git 忽略。

## 与历史实验的关系

输入入库不构成新的实验，也不修改已有成绩。`results/` 的各份档案仍以原始源码、参数和哈希说明自己的运行身份；其中“原图未随本次结果包上传”的历史表述保持原样。现在从当前仓库读取 `official/data/` 即可提供这些原图，但历史标签检出可能仍需显式指定当前数据目录。
