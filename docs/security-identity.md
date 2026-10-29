# 证券身份事件与退市边界审计

## 代码变更事实

`configs/security_identity_events.csv` 保存有来源支持的证券身份事件。当前登记：`SZ300114 → SZ302132`，生效日 `2025-02-17`，转换比例按旧股数到新股数记为 1:1。深交所披露的实施公告确认同一上市主体从该日开市起启用新代码，原代码持股转到新代码且股数不变：[中航成飞实施公告](https://disc.static.szse.cn/disc/disk03/finalpage/2025-02-15/cedb693a-f5ee-4463-9682-ea33d406b569.PDF)。

这条事件同时定义两种不同连续性：

- **持仓连续**：生效日把旧代码持仓、成本基础和首次持有日迁到新代码；1:1 股数不变，不记为买卖，不改变现金，不产生费用或已实现盈亏。转换比例产生非整数股时会拒绝迁移，等待明确的尾差处理规则。
- **信号不连续**：动量按代码分组计算，不跨代码拼接价格。新代码从生效日起独立积累配置中的观察期；观察期内迁移持仓冻结，不因缺少新代码信号而卖出。观察期后才执行新代码自己的信号。

代码迁移作为单独的 `security_events.csv` 组合事件输出，不进入 `trades.csv`。事件输入表哈希和事件编号写入实验指标。没有旧代码持仓时仅记 `NO_POSITION`，不伪造交易。未登记的旧代码缺行情仍是未解释缺口，点时股票池审计不会通过。

## 审计口径

行情覆盖分母使用日期股票池原始成员行，不因代码事件改写。对 2025-02-17 的原始事实仍保留为：旧代码在名单中，但旧代码没有有效 OHLC；同日新代码必须在名单中且有有效、非停牌行情，事件才可把该条差异标成 `EXPLAINED_SECURITY_CODE_TRANSITION`。原始缺行情计数仍为 1，另外报告已解释条数和未解释条数；不会制造旧代码行情。

20 条 `outDate` 与日期名单差异逐行保存在[独立证据登记表](evidence/universe-outdate-boundary-audit.csv)。审计代码将它与私有快照的 `reconciliation.csv` 按 `(trade_date, symbol)` 精确匹配，并要求基础资料 `outDate`、公告确认日期和差异日期完全一致。缺来源、日期不符或快照多出未登记记录都会留为未解决。`SH601989` 的公告是吸收合并：每股旧股换 0.1339 股 `SH600150`；这只说明终止上市日期已核对，不代表组合账务已实现，必须等专门的并购换股和零股处理。

证据登记表的公告由本项目维护者逐项核对；审计命令不在运行时下载公告。`EXCHANGE_CIRCULAR_CONFIRMED` 的 `SZ000851` 使用香港交易所通告中的深交所摘牌日期说明，证据类型与发行人公告分开标记。

## 本机复核命令

```bash
uv run cehuan universe-audit \
  --universe data/private/universes/bs-universe-20230101-20251231-v1 \
  --bars data/private/snapshots/bs-bars-universe-20221201-20251231-v1/bars.csv \
  --identity-events configs/security_identity_events.csv \
  --out-date-evidence docs/evidence/universe-outdate-boundary-audit.csv \
  --output data/private/audits/universe-audit-2023-2025-v2
```

审计会分别输出原始行情质量、点时名单对账、身份事件解释、20 条边界逐条结果，以及 `point_in_time_quality.json`。即使这些对账通过，当前配置仍将 `formal_point_in_time_baseline_enabled` 设为 `false`：现金分红/送转/配股与退市持仓记账、有效日期市场规则和历史费用校准仍未完成。本阶段只复核数据，不重跑基线，也不生成账户交易建议。
