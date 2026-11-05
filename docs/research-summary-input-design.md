# 数据审计脱敏摘要入口设计

**状态：设计稿；本轮未实现适配器、CLI 命令或模型发送。** 研究版 MVP 的完整 Agent 周期仍只使用合成数据。

## 目标与边界

未来可从策环已有的数据准备、点时股票池、输入证明及 readiness 审计报告中提取一个受控摘要，供研究者判断数据是否适合进一步研究。它只描述审计覆盖和缺口，不运行收益比较，不产生交易计划，也不把 `NOT_READY` 转成可用状态。

摘要必须保留原报告的状态词和评价窗口。特别是历史数据可得时点、供应商版本、有效日期交易规则、个人费用和公司行动覆盖，只要原报告为 `UNVERIFIED`、`FALLBACK_USED`、`INCOMPLETE` 或 `NOT_READY`，摘要就保留该状态，不用总体覆盖率或其他字段替代。

## 建议的只读接口

拟新增纯函数：`build_redacted_audit_summary(report_path, report_kind)`。它只接收明确支持的版本化审计 JSON，拒绝未知 schema；输出新的摘要对象，不修改原报告。未来 CLI 可采用 `cehuan research-summary --audit-report ... --output ...`，但在字段映射、脱敏和测试完成前不应暴露此命令。

建议的摘要结构：

```json
{
  "schema": "cehuan.redacted_research_summary.v1",
  "source": {
    "report_kind": "point_in_time_audit",
    "report_schema": "<原报告 schema>",
    "report_sha256": "<原报告字节散列>",
    "scope": "沪深；不含北交所",
    "evaluation_window": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
  },
  "program_facts": {
    "quality_status": "UNVERIFIED",
    "coverage_counts": {"members_checked": 0, "members_missing_bars": 0},
    "data_availability_status": "UNVERIFIED",
    "market_rule_coverage_status": "NOT_READY",
    "fee_coverage_status": "NOT_READY",
    "corporate_action_coverage_status": "NOT_READY",
    "formal_baseline": "NOT_READY",
    "known_gaps": ["... "]
  },
  "transformation": {
    "allowlisted_fields_only": true,
    "security_identifiers_removed": true,
    "account_data_included": false,
    "returns_or_performance_included": false
  }
}
```

`coverage_counts` 仅允许来自原审计报告中可核对的汇总值；源报告没有的计数保留缺失，不从原始行临时推算。摘要应使用报告 SHA-256 和报告 schema 绑定来源，不复制本地绝对路径、原始逐证券行或数据文件内容。

## 不得进入摘要或模型请求的内容

- 华泰成交/持仓、账户号、姓名、资金规模、委托状态或用户持仓。
- 逐证券代码、逐笔交易、原始行情行、私有文件路径、API Key 和环境变量值。
- 源审计未计算的收益、胜率、风险指标或策略优劣。
- 将 `UNKNOWN`、缺失或未审状态改写为通过的推断。

如果未来把摘要交给外部模型，必须显式选择 LLM 模式，并在请求前显示摘要字段清单和脱敏结果供用户检查。模型回答只作为文字研究意见；它不能改变程序事实或 readiness。

## 后续实现验收

后续实现需对每一种支持的原报告版本建立 fixture，验证字段白名单、状态保留、报告散列、缺字段处理、隐私字段删除和未知 schema 拒绝。还要确认生成的摘要不含证券代码、绝对路径、账户记录、收益结果或密钥。完成这些检查前，不向 LLM 提供真实数据审计内容。
