# Python 环境说明

## 基础版本

本项目基础版本为 **Python 3.12**。每位成员使用自己的虚拟环境；虚拟环境不进入 Git。

当前尚无比赛程序或第三方依赖，因此没有预设依赖清单，也没有安装 NumPy、pandas 等计算库。本说明给出准备步骤，不代表本机已安装或验证了 Python。

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
# 当对应程序文件存在后，替换为实际入口
.\.venv\Scripts\python.exe src/your_program.py
```

## macOS / Linux

```bash
python3.12 -m venv .venv
.venv/bin/python --version
.venv/bin/python -m pip --version
```

## 加入实际算法后

引入第三方库的 PR 应同时添加或更新根目录的 `requirements.txt`，记录经过验证的版本。依赖清单存在后，Windows 可运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS / Linux 使用 `.venv/bin/python -m pip install -r requirements.txt`。如需 Jupyter，将它作为明确的开发依赖记录，不要求所有成员默认安装。

每个程序应说明输入文件、执行命令、参数、随机种子（若使用随机过程）、输出路径以及用于验证的小样例。实际测试加入后记录对应命令，不把安装成功当作算法运行正确。
