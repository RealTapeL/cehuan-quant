# 真实行情摘要研究

`cehuan real-summary-research` 从现有 BaoStock 短区间快照和审计文件生成脱敏研究摘要，再要求结构化研究 Agent 对摘要提出一条假设。该命令不运行回测、不改策略、不连接券商，也不生成委托。

## 生成摘要并尝试调用模型

在已经配置好 `CEHUAN_LLM_MODEL`、`CEHUAN_LLM_BASE_URL` 和 `CEHUAN_LLM_API_KEY` 的 shell 会话中运行：

```bash
uv run cehuan real-summary-research --model-mode llm
```

本命令只接受显式 `llm` 模式。配置缺失时仍会保存本地摘要和阻断报告，调用次数为零，不会退回 stub。API Key 只从环境变量读取，不进入输入、日志或产物。

默认摘要输入来自本机 `data/private/` 和现有 `outputs/` 审计文件。原始行情不发送给模型；Agent 输入仅包含哈希绑定的聚合统计、少量事后重建排名和明确限制。产物保存在 Git 忽略的 `outputs/real-market-research/`。

## 重用已冻结摘要

摘要首次生成后，可用它固定模型输入。复用时程序会重验所有来源 SHA-256，并用现有适配器从这些来源重建摘要，与冻结文件逐字段比较：

```bash
uv run cehuan real-summary-research \
  --model-mode llm \
  --summary outputs/real-market-research/<research-id>/market_summary.json
```

如来源文件缺失或哈希改变，程序拒绝复用。摘要和模型运行各自保存来源、输入、报告及 SHA-256 清单。

## 数据与结果口径

- 本地摘要会区分审计文件记录、当前程序重新计算结果与事后重建排名。
- `available_at` 是适配器赋值，不证明供应商的历史发布时间；供应商 vintage 和历史时点可得性保持 `UNVERIFIED`。
- 市场规则存在配置回退；费用不等于历史费率或个人华泰佣金；公司行动覆盖不完整。
- 真实摘要假设必须引用摘要中的 `fact_id`、来源哈希和证据状态，且状态为 `NEEDS_DATA`。
- 不支持的规则或参数被标为 `PENDING_IMPLEMENTATION`，不会应用。
- 正式基线保持 `NOT_READY`。本流程不进行策略收益评价，也不表示假设具有盈利能力。
