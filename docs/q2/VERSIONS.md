# 问题二工程版本与独立入口

v3 是基座优化版的工程版本号，对应问题二的第二个数学模型。v2.1 保留为修复新基座的组件接口版本；两者并存，不替换原入口，不移动或删除原配置。

| 版本 | 算法标记 | 入口 | 源码与配置 | 固定标签 |
|---|---|---|---|---|
| v2.0 | `Q2-Repair-2.0` | `src/solve_problem2.py` | 原修复基座及其已完成的全量实验 | `q2-baseline-2.0` |
| v2.1 | `Q2-Repair-2.1` | `src/solve_problem2.py` | 原 `src/q2` 基座模块、`configs/q2/baseline.json` | `q2-baseline-2.1` |
| **v3** | **`Q2-Optimized-3.0`** | **`src/solve_problem2_optimized.py`** | **`src/q2/optimized/`、`configs/q2/optimized.json`** | **`q2-optimized-3.0`** |

v2.1 固定于发布前 `main` 的提交 `fd0c97834d14cbba0cda8a1ee73b541086f229ae`，该提交已包含合并后的 2.1 源码。v3 通过任务分支和 PR 交付；版本标签不代表已合并到 main。

## 保留与兼容

本次没有修改任何已有 v2.1 的 `src/q2` 文件、原基座入口或已有 Q2 配置。v3 通过独立的 `q2.optimized.preparation` 继承原准备接口，扩展缓存分类和执行准备。共同调度框架增加可选实例策略与 `q2` 优化后端注册，Q1 默认路径不变。v2.1 的完整运行环境还可从固定标签独立检出。

```powershell
# 原 v2.1 基座
python src/solve_problem2.py solve official/data/case_004.json --cores 2 --output-dir outputs/q2-v21

# 新 v3 优化模型
python src/solve_problem2_optimized.py solve official/data/case_004.json --cores 2 --output outputs/q2-v3
```

不同版本使用不同输出目录。v3 默认启用基座保底，先完成当次基座候选，再局部改进并以精确成绩择优；若基座预算不足，`baseline_guard.complete=false`，不能宣称完整基座不退化保证。

## 发布前名称与实验记录

本次优化器在本地开发及四组试跑时使用内部标记 `Q2-Optimized-1.0`，按用户发布要求改为 `Q2-Optimized-3.0`。此处是优化实现的首次公开到项目仓库，旧的“1.0”不是有缺陷的 `Q2 1.1` 新基座，也不是问题一模型一。

四组旧试跑的计划哈希、版本字段和成绩保持原样，见 [有限验收](OPTIMIZED_ACCEPTANCE.md)。v3 不将旧优化内部版本或 v2.1 的输出当作自身 `--resume` 的已完成实例。改名和缓存隔离不修改数学规则，本轮只重跑组件、随机小图差分和现有回归，没有重新跑全量、大图试跑或消融。

发布核验记录见 [v3 发布清单](validation/v3_publication.json)，运行和独立复评命令见 [接口说明](OPTIMIZED_INTERFACES.md)。

发布前最终回归共 283 项：281 通过、2 因外部历史归档未安装而跳过；40 张随机小图的 120 份方案、4478 个操作时序差分通过。既有 v2.1 的 30 份源码、配置及入口文件逐一核对 Git 内容哈希一致。编译与 `git diff --check` 通过。本次没有新增大图或全量实验成绩。
