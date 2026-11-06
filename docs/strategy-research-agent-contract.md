# 策略研究 Agent 接口契约

本接口用于把数据状态、冻结策略和实验摘要交给研究 Agent，并接收**待审核研究假设**。项目提供本地 stub 与 OpenAI-compatible Chat Completions 适配器；它们只生成结构化假设，不运行任意代码、策略回测或券商操作。受控实验由策环本地代码执行。

## 输入

输入包括研究目标与时间范围、数据可信度标签、冻结策略 ID 和配置散列、既有实验摘要、复盘摘要以及已知缺口。运行时的 `experiment_capabilities` 由受控实验器的 `SUPPORTED_HYPOTHESIS_PATHS` 动态生成，展示参数路径、类型、含界限范围和冻结配置当前值，并声明每次实验的改动数上限。提示词不维护第二份参数清单；受控实验器仍独立验证模型提出的每项变化，未支持的路径保留为待实现状态。

文件契约见 [Agent 输入 JSON Schema](../examples/strategy-research-agent-input.schema.json)，合成输入见 [输入示例](../examples/strategy-research-agent-input.synthetic.json)。`data_trust` 沿用回测 `metrics.json` 中的结构化 `research_trust` 对象。当任何可信度字段为 `UNVERIFIED`、`FALLBACK_USED` 或 `NOT_READY`，Agent 必须在输出限制中保留该状态，不得将它改写为通过。

## 输出

输出是一个独立的研究假设对象，包含问题、候选配置变更、理由、证据摘要及散列、所需数据、已知限制和验证计划。输出必须关联来源实验及输入数据摘要，并固定：

- `status` 只能表示 `PROPOSED`、`NEEDS_DATA` 或 `REJECTED_FOR_REVIEW`；它不是策略通过或实盘状态。
- `human_review_required` 必须为 `true`。
- `may_modify_active_strategy` 和 `may_generate_orders` 必须为 `false`。
- 输出不得包含委托、券商命令或执行指令字段。

文件契约见 [假设输出 JSON Schema](../examples/strategy-research-hypothesis.schema.json)，示例 [仅使用合成数据](../examples/strategy-research-hypothesis.synthetic.json)。本地 `validate_strategy_hypothesis()` 检查 schema、禁止字段和安全布尔值。输入继续使用 [Agent 输入 JSON Schema](../examples/strategy-research-agent-input.schema.json)，`validate_strategy_research_input()` 在请求前检查输入字段与研究边界。

## 本地研究版 MVP

```bash
uv run cehuan research-cycle \
  --objective "检查调仓间隔对换手的影响" \
  --model-mode stub \
  --output outputs/research-cycles
```

命令使用合成行情，调用结构化 Agent 生成初始假设；只允许有限的单参数映射进入候选配置；复用 `run_backtest()` 跑相同输入的基线与候选；随后复用 `synthetic_cycle` 的成交/持仓对账并基于审计摘要请求下一轮假设。默认预算是每周期两条假设、一个候选实验、每次调用最多一次重试。未知配置或规则会登记为 `PENDING_IMPLEMENTATION`，不会被忽略。候选配置只写入本周期目录，不更改 `configs/default.toml`。

下一轮输入从已保存的 `candidate_experiment.json`、基线/候选指标及成交和持仓对账文件重建 `program_facts`，并逐项列出来源文件名和 SHA-256。Agent 初始假设等文字位于独立的 `agent_authored_context`，不会混入程序事实。实验未完成、失败或候选指标缺失时，相应状态和空值会原样保留，不补造比较值；下一轮假设摘要通过输入摘要绑定这些事实和来源。

模型设置位于 `configs/research_agent.toml`。`mode="auto"` 只有在模型名、服务地址和 API Key 都可用时才发起外部调用，否则使用明确标记的 stub。外部服务通过 `CEHUAN_LLM_MODEL`、`CEHUAN_LLM_BASE_URL` 和 `CEHUAN_LLM_API_KEY` 提供；Key 只放在环境变量中，不写入配置或产物。可选的 `CEHUAN_LLM_REASONING_EFFORT` 会原样作为兼容 API 的 `reasoning_effort` 请求参数发送；未设置时不发送该参数。对阿里云 `qwen3.8-27b`，可用 `none` 关闭默认思考，缩短结构化 JSON 请求时间。默认请求超时为 120 秒。网络/API 传输失败不会自动重试，因为服务端可能已处理超时请求；可在确认调用状态后手动重试。运行输入只有研究目标、合成数据摘要、参数和合成复盘事实，不包含账户资料或原始成交文件。切换真实模型前应避免把私密信息写入研究目标。

真实 LLM 验收必须显式使用 `--model-mode llm`，例如在安全配置好环境变量后运行：

```bash
uv run cehuan research-cycle \
  --objective "检验合成研究闭环的结构化假设输出" \
  --model-mode llm \
  --output outputs/research-cycles
```

任何模型配置缺失都会失败并说明缺少的变量名，不会自动退回 stub；stub 验收应显式使用 `--model-mode stub`。

每个研究周期目录保存 Agent 输入/输出、提示词版本和摘要、候选配置/结果、合成成交复盘、研究报告及散列 manifest。若 provider 返回 usage，会记录 token 数；没有 provider 费率配置时不估算费用。网络/API 传输失败记录为单次尝试，不能据此断定服务端未计费；结构校验失败仍遵循配置的有限重试数。`stub` 运行绝不标记成真实 LLM 调用。结果只验证软件流程，不代表策略表现；正式 baseline 仍为 `NOT_READY`，研究假设需要人工审核，不自动修改生效策略。

该 MVP 的完整周期已用 stub 验证；仓库尚无一次成功的真实 LLM 周期验收记录。真实数据的受限摘要入口目前只有设计文档，尚未连接模型；Qlib/RD-Agent 自动搜索和多参数/新交易规则实验也需独立评估。

## 从成交审计生成假设包

`scripts/build_audit_hypotheses.py` 将已封存的成交审计目录作为只读输入，逐项核对其 artifact inventory，并要求实际配置文件 SHA-256 与审计摘要登记值一致。它会把配置参数和摘要都绑定到同一份实际配置；缺哈希、错误配置或证据散列不匹配时拒绝生成。

输出必须指定新的独立目录；目录已存在或位于来源审计目录内时拒绝。脚本先在临时目录写完产物，再复核来源清单及文件哈希，最后发布新目录。来源清单、摘要、证据和配置不会被写回。旧版假设包只做只读一致性检查，结论写入新包的 `source-audit-reference.json`；`UNVERIFIED`/`MISMATCHED` 不会覆盖旧包或自动变成通过。

```bash
uv run python scripts/build_audit_hypotheses.py \
  --audit-dir data/private/experiments/<run>/<trade-audit-package> \
  --config data/private/experiments/<run>/config.json \
  --output-dir data/private/experiments/<run>/research-hypotheses-<unique-id>
```

脚本仅证明本地字节、登记哈希和旧包中保存的参数/证据引用之间的对应关系，不证明公告解释正确、历史数据当时可得或策略有效。输出假设仍为 `NEEDS_DATA`，当前正式基线仍 `NOT_READY`。

## 审核与研究闭环

Agent 输出先保存为候选假设，再由用户审阅、冻结实验配置、确定时间顺序和验收门槛。实验结果回写到假设记录时应保留原始假设、数据散列、代码提交、运行产物和人工结论。只有经批准的研究变更才创建新策略版本；无论 Agent 或回测结果如何，均不生成华泰委托。

## 版本兼容

`schema_version=1` 的字段含义稳定。新增字段需升版或显式提供兼容规则。示例中的数据均为合成内容，不构成市场结论或策略建议。
