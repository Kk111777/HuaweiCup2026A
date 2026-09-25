# 第一问划分方案、子图调度与加速比

N2、N3、N4、N5 各含 case_001_plan.json 至 case_100_plan.json，共 400 份方案。
方案保持原算法输出的官方格式：node_to_subgraph 是计算节点到子图的映射，core_schedules[k] 是核心 k 的子图执行顺序。核心和子图编号从 0 开始；空列表表示空闲核心。
同一方案已由原求解器写入后重读、完整调用官方评估器复评，并核对完整结果一致性。
subgraph_schedules.csv 提供每个子图的核心、队列顺序和官方模拟开始/结束 cycles，便于直接检查排布。
speedups.csv 保存每例、每个核数的结果；average_speedups.csv 对全部 100 例的逐例加速比取算术平均。
加速比 = 已校验单核基线 makespan / 本次方案官方 makespan。小于 1 表示该方案慢于单核，按原值保留。
case_timings.csv 的 wall_seconds 为本次每个 case 完成四个核数的总墙钟时间，cpu_seconds 为该进程 CPU 时间；不含队列等待。N2–N5_seconds 为原求解器的分核阶段计时。
Python 运行秒数不参与模拟加速比计算。单核基线复用已有归档，不把历史耗时算入本次求解耗时。
本次为协作者提交 04634a2400ce9fa0c45084bed06fb4ce72de917d 的 adaptive 算法；20 个 case 进程，每例顺序执行候选。

在协作者工作区中复评一个方案（路径按实际位置替换）：
python official/code/multicore_cut_evaluate_problem_1.py <case_001.json> <N5/case_001_plan.json> --config <config.txt>

此包包含待提交的方案与辅助结果，未向任何平台上传。
