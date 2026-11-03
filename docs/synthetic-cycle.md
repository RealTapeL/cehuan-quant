# 纯合成研究到复盘闭环

此示例用虚构的 `SZ000001` / `SZ000002` 价格先运行技术基线，再生成待审核计划，写入一条**虚构的部分成交**和虚构持仓快照，复用真实项目 CSV 读取与复盘函数，并生成结构化的下一轮研究假设。它不包含真实行情、不读取账户、不连接华泰、不发送委托，也不验证策略效果。

```bash
uv run python examples/run_synthetic_cycle.py --output outputs/synthetic-cycle
```

每次运行都会在输出根目录中创建唯一子目录，拒绝覆盖已有产物，内容包括：

- `plan.csv`、`plan.md`：独立待审核计划，醒目标注 `SYNTHETIC ONLY`；现金金额是演示输入。
- `synthetic_bars.csv`、`baseline_metrics.json`、`baseline_equity_curve.csv`、`baseline_trades.csv`：冻结输入及基线产物；指标被标为合成闭环演示。
- `fictitious_fills.csv`、`fictitious_positions.csv`：虚构成交和持仓输入，不是华泰导出。
- `reconciliation.csv`、`position_reconciliation.csv`、`review.md`：调用策环的 CSV 导入和计划对账流程。
- `next_hypothesis.json`：包含证据、置信边界和下一次验证建议；不自动修改策略参数。
- `manifest.json`：产物索引和合成结果类型。

同一 `cycle_run_id` 写入计划、假成交、复盘表、基线元数据、假设和 manifest；manifest 同时保存行情 CSV 的 SHA-256。

## 一次实测输出

2026-11-03 在项目环境运行后，合成基线累计收益为约 6.87%，共 5 笔模拟交易；随后生成一个 `BUY` 计划和虚构部分成交：计划 1,300 股，演示成交 600 股，未成交 700 股，成交价相对参考价约高 0.132%，CSV 费用为 ¥2.00。复盘状态为 `PARTIAL_FILL`。

这些数字只检查计划字段、CSV 映射、数量/VWAP/费用算术及假设 JSON 的串接。示例同时说明：仅凭成交差异无法解释未成交原因，因此下一假设要求积累委托回报、报价与人工记录；本次结果不支持任何参数调整。

## 账务事件研究诊断

`run_backtest(accounting_mode="ledger_v2")` 的 Python API 可选接收类型化 `CashDividendEvent` 与 `CodeChangeEvent`：

- 现金分红的登记日、除息日、派息日都必须存在于回测交易日中。权利快照在该日模拟开盘成交后、收盘估值前记录；除息日产生应收，派息日转现金。分红税额需明确传入；未知时应收不能可靠估值，回测停止。
- 1:1 代码变更在新代码生效日验证有效行情、上市和非停牌状态，再迁移账本持仓。事件本身不产生普通成交、现金变化或费用；批次数量、成本和取得日由账本保留。信号不跨代码拼接，新代码重新积累动量窗口。
- 事件日期若未包含在输入交易日、ID 重复、类型不受支持或新代码行情缺失，运行会拒绝继续。事件字段和分红税额会写入实验数据哈希。

该入口仅用于带明确标签的研究诊断。当前事件是 Python 类型化输入，不是现实证券事件的 CSV 适配器；调度范围不包含送转、配股、非 1:1 合并、退市和事件覆盖审计。因此它不满足正式多年基线门槛，`baseline_ready` 仍为 `NOT_READY`。完整的边界见[公司行动账务契约](corporate-action-accounting-contract.md)和[正式基线就绪审计](baseline-readiness.md)。
