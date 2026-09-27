# Q3v3：完整方案继承、容量重排与时间/搬运折衷

本版本为 **Q3v3**，修订 `q1q2-memory-traffic-1`，独立入口 `src/solve_q3v3.py`，代码包 `src/q3v3/`。与已有 `src/q3/`、`src/solve_problem3.py` 并存，后者的百例500组归档和运行接口保持原样。

完整 Q1/Q2/可选旧 Q3 JSON 在问题3下重新评价；结合固定分核的顺序投影、L1/UB容量窗口DP、spill/FIFO反馈、跨核读取窗口及全局联合搜索。默认用最多1%的周期退让交换搬运减少，同时保存纯最快方案；两份都由官方CLI独立重放。

默认目标为 `T/Tref + 0.05 × (0.75 × DDR/Dref + 0.25 × added_COPY/Aref)`，分母由输入种子固定。此目标是工程偏好，官方评估和题目3的无L2/有L2周期比不变。

## 运行

Python 3.12，求解器仅使用标准库。准备同一赛题的 `official/data/case_*.json`；输入图不随Git归档。

```powershell
python src/solve_q3v3.py official/data/case_014.json --cores 3 --warm-start path/to/q1_plan.json --q2-warm-start path/to/q2_plan.json --objective cycles-traffic --seconds 120 --max-evaluations 64 --evaluation-timeout 600 --workers 1 --output-dir outputs/q3v3_new
python src/report_q3v3.py outputs/q3v3_new
python -m unittest discover -s tests -p "test_q3v3_*.py" -v
```

`--reference-start` 可加入旧Q3完整方案。传目录时须覆盖每个请求的case/N；`--workers 20`控制主机并发，`--cores`控制模拟NPU核数。默认完整方案模式不执行Q1/Q2求解、不读取旧评分或中间数据；无完整起点时可从本仓库的Q1纯图接口导出未评分候选。

新命令行默认 `cycles-traffic`，Python `solve_one()` 保留历史 `cycles-hit` 默认以兼容旧调用；API使用本版目标需显式指定。原 `cycles-hit`、`cycles-copy` 可继续选择。

## 单核范围

Q3v3内核支持已有完整单核JSON的N=1优化，但当前CLI不会自动生成单核起点。本次20组只覆盖N=2～5，**不是完整1～5核验收**。基础N=1方案可由官方 `build_singlecore_plan()` 生成，再分别执行官方Q2/Q3评估取得同方案无L2/有L2对照；单核Cache加速比不固定为1。

## 已验证结果

[20组验证说明](docs/q3v3_validation.md)记录本轮指标与验证范围。40份最终方案、60份完整起点、官方重放文件及来源哈希保留在本地，不随本次代码提交上传。20主机进程的有效批次墙钟173.01秒，起点重放另计53.32秒。相对旧Q3，平均逐例cycles降低0.792%，9快/6平/5稍慢；DDR合计下降48.490%，spill下降55.483%，字节命中率20.601%→39.786%。纯最快方案10快/10平/0慢；新折衷方案相对各自最快最多慢0.9423%。

上述为同一冻结样本的历史实测，不是百例结果或等预算冷启动对照。Q2、旧Q3继承收益与新增搜索收益分列；实验源码在本地冻结；发布只补齐本仓库Q1路径兼容，搜索算法未改变。

详细公式、容量约束、算法流程、模块接口与限制见[算法说明](docs/q3v3_memory_traffic.md)。

本次Git提交只包含代码、测试代码和说明文件，不包含原图、划分JSON、候选结果、实验归档、日志或虚拟环境。
