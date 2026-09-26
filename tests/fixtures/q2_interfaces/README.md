# Q2 2.0接口迁移与行为保持夹具

来源提交：`11501c5137d949514724baed3f6d0d3bd9e38896`，算法 `Q2-Repair-2.0`。
2026-09-26在独立解包的该提交源码上，用Python 3.12.14运行三个已有小图，N=2、wall=60秒、reserve=10秒，无单核分母，全部完成搜索及最终独立官方复评。

* `v20_behavior.json` 保存 minimal、cross_fanout、memory_interleaved 的输入SHA-256、最终计划、正式目标、搬运量、请求数和β轨迹，作为2.1行为保持测试的独立旧版对照。
* `v20_minimal_publication.zip` 保存最小图发布目录中的 `complete.json` 及其 `artifacts_sha256` 列出的原始文件，用于验证只读导入；保留历史溯源路径，路径不作为运行依赖。

ZIP SHA-256：`c6c7d85390fcf3bb843c7e2155af3eac08a61f6fcea195191334c1c2f8c5ffa3`。
压缩包只有根目录文件，不包含源码、凭据、大图或嵌套路径。测试在临时目录解包，不修改夹具。
若需重建，必须从上述旧提交运行，不能以当前版本重新生成期望值。实际对照结果见[验证报告](../../../docs/q2/interfaces_validation.md)。
