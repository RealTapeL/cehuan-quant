# 数据规范和时点边界

规范行情字段：

| 字段 | 含义 |
|---|---|
| `trade_date`, `symbol`, `name` | 交易日、交易所前缀代码（如 `SH600519`、`SZ000001`、`BJ830799`）、可选名称；六位代码和 `.SH/.SS/.SZ/.BJ` 输入会统一 |
| `open`, `high`, `low`, `close` | 原始未复权价格；用于参考价与初版成交约束 |
| `adjusted_close` | 后复权收盘价；用于技术基线连续性较好的收益特征 |
| `volume_shares`, `amount_cny` | 股、元；AKShare 返回成交量单位为手，适配器乘 100 |
| `is_suspended`, `is_st` | 点时上市/停牌/ST 状态；AKShare 单股日线不提供，缺失保留未知 |
| `has_price_limit`, `price_limit_pct` | 当日是否有限价及其阈值；需来自有效日期证券主数据，IPO 等无涨跌幅限制日期可设 `has_price_limit=false` |
| `buy_min_shares`, `buy_step_shares`, `sell_min_shares`, `sell_step_shares` | 按板块和当日规则配置的数量约束；不可把全市场假设成同一手数 |
| `board`, `market_rule_source`, `market_rule_valid_from`, `market_rule_valid_to` | 板块、规则证据来源及有效期，用于追溯点时规则 |
| `data_source`, `retrieved_at_utc`, `available_at` | 来源、实际拉取时间、按收盘后可用假设的时间 |

目前适配的公开接口是 AKShare `stock_zh_a_hist(symbol, period="daily", start_date, end_date, adjust)`。官方文档说明成交量单位为手；原始、后复权两次查询返回同一日期区间。实测接口可返回所需 OHLCV，但网络端点偶发代理连接失败，因此命令保留明确错误并不写入半份股票池数据。

## 防泄漏规则

1. t 日特征只读取 t 日收盘及之前数据；任何订单最早放在下一交易日。
2. 每日接口的 `available_at` 按当日 15:00 Asia/Shanghai 记录为可用时间假设；接口实际到达时间另记 `retrieved_at_utc`。计划命令遇到未来的 `available_at` 会停止，避免收盘前用未完成日线生成计划。
3. 前复权价格可能随未来除权事件回改历史。当前用后复权收盘算信号、原始价做初版执行参考，仍需点时公司行动/因子版本才能做严谨历史复现。
4. 不能用当前 ST 名单或当前股票列表代替历史状态/历史成分。没有状态记录时，计划动作标成 `VERIFY` 或 `HOLD`，回测跳过未知状态行。
5. 交易所日历与停牌不能从缺失行情行准确推出。应提供时点证券主数据和交易所日历文件后再扩大股票池。

## 需要定期检查的质量项

空字段、重复 (trade_date, symbol)、OHLC 不一致、非正价格、负成交量、代码映射、最后可用日和拉取时间。现有行情接口不支持完整的退市证券/历史股票池质量审计；这项不通过当前单股验证就被默认为已解决。
