# Python 环境说明

## 基础版本

本项目基础版本为 **Python 3.12**。每位成员使用自己的虚拟环境；虚拟环境不进入 Git。

三问求解器、单核基线和官方评估脚本仅使用 Python 标准库，无需安装第三方依赖。Q3 报告绘图可选安装根目录 `requirements-q3.txt`，或在支持该参数的入口使用 `--no-plots`。入口和测试已在 Python **3.12.14** 上验证。请先检查解释器版本，系统 `python` 可能指向其他版本。

## Windows PowerShell

安装 Python 3.12 后，确认版本并创建环境：

```powershell
py -3.12 --version
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip --version
```

如果没有 `py` 启动器，先用 `python --version` 确认 `python` 指向 Python 3.12，再执行 `python -m venv .venv`。如果命令打开应用商店，表示尚未找到可用的 Python 解释器，需要先完成安装或修正解释器路径。

可以直接调用虚拟环境中的 Python，无需调整 PowerShell 执行策略或激活环境：

```powershell
$env:PYTHONUTF8 = "1"  # 当前会话使用 UTF-8 输出中文日志
.\.venv\Scripts\python.exe src/problem_1_baseline.py
.\.venv\Scripts\python.exe src/problem_1_baseline.py --workers 8 --resume
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## macOS / Linux

```bash
python3.12 -m venv .venv
.venv/bin/python --version
.venv/bin/python -m pip --version
.venv/bin/python src/problem_1_baseline.py
.venv/bin/python src/problem_1_baseline.py --workers 8 --resume
.venv/bin/python -m unittest discover -s tests -v
```

## 复现材料与结果

原版题面、100 例原图和配置已在 `official/` 中提供，运行前按[数据说明](data.md)校验输入。批量入口会使用与自身相同的 Python 解释器启动官方单核程序，因此不依赖子进程的 `PATH` 设置。官方配置保持原始字节内容，缺少材料或配置校验失败时不会开始评估。

默认结果写入 `outputs/problem_1_singlecore/`；新实验选择新的输出目录，同一实验的中断续跑使用 `--resume`。记录在 `run_info.json` 中的 `python_version`、输入与源码 SHA-256 可用于确认复现环境。运行秒数随电脑负载变化，评估指标仍以模拟器输出的 cycles 为准。

## CPU 并行设置

每个 case 独立调用一个官方 Python 评估进程；`--workers` 指定同时运行的进程数，不改变模拟的 NPU 核数。默认取逻辑 CPU 数的一半，上限为 8。小内存电脑可手动降低并发；实际 Python 进程的内存占用与赛题中的 L1/UB 容量是两个不同概念。


## 已归档结果

完整结果保存在 [results/problem_1_singlecore](../results/problem_1_singlecore/README.md)。`run_info.json` 是原始运行记录，归档中的绝对解释器路径仅作为来源信息，不要求其他成员使用相同路径。为保证配置和来源哈希可核对，官方文件、基线入口及归档结果均按原始字节保存。
