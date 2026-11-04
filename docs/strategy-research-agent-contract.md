# 策略研究 Agent 接口契约

本接口用于把数据状态、冻结策略和实验摘要交给研究 Agent，并接收**待审核研究假设**。当前没有连接 RD-Agent、LLM 或券商；schema 与校验函数只定义项目边界。

## 输入

输入包括研究目标与时间范围、数据可信度标签、冻结策略 ID 和配置散列、既有实验摘要、复盘摘要以及已知缺口。Agent 只可提出假设、解释证据或设计验证；每条证据应标记为原始记录、代码重建、未核实或合成材料。

文件契约见 [Agent 输入 JSON Schema](../examples/strategy-research-agent-input.schema.json)，合成输入见 [输入示例](../examples/strategy-research-agent-input.synthetic.json)。`data_trust` 沿用回测 `metrics.json` 中的结构化 `research_trust` 对象。当任何可信度字段为 `UNVERIFIED`、`FALLBACK_USED` 或 `NOT_READY`，Agent 必须在输出限制中保留该状态，不得将它改写为通过。

## 输出

输出是一个独立的研究假设对象，包含问题、候选配置变更、理由、证据摘要及散列、所需数据、已知限制和验证计划。输出必须关联来源实验及输入数据摘要，并固定：

- `status` 只能表示 `PROPOSED`、`NEEDS_DATA` 或 `REJECTED_FOR_REVIEW`；它不是策略通过或实盘状态。
- `human_review_required` 必须为 `true`。
- `may_modify_active_strategy` 和 `may_generate_orders` 必须为 `false`。
- 输出不得包含委托、券商命令或执行指令字段。

文件契约见 [假设输出 JSON Schema](../examples/strategy-research-hypothesis.schema.json)，示例 [仅使用合成数据](../examples/strategy-research-hypothesis.synthetic.json)。本地 `validate_strategy_hypothesis()` 会检查必要字段和这些边界，但不会应用配置或启动实验。

这只是本地格式契约与验证原型，尚未连接大语言模型、RD-Agent 或其他自动研究服务。研究假设仍需由用户审核、另行冻结配置并运行实验。

## 审核与研究闭环

Agent 输出先保存为候选假设，再由用户审阅、冻结实验配置、确定时间顺序和验收门槛。实验结果回写到假设记录时应保留原始假设、数据散列、代码提交、运行产物和人工结论。只有经批准的研究变更才创建新策略版本；无论 Agent 或回测结果如何，均不生成华泰委托。

## 版本兼容

`schema_version=1` 的字段含义稳定。新增字段需升版或显式提供兼容规则。示例中的数据均为合成内容，不构成市场结论或策略建议。
