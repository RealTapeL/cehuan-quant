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

目前适配 AKShare 的两个独立日线来源：东方财富 `stock_zh_a_hist(symbol, period="daily", start_date, end_date, adjust, timeout)` 与腾讯 `stock_zh_a_hist_tx(symbol, start_date, end_date, adjust, timeout)`。官方文档将两者都描述为盘后更新；东方财富成交量单位为手，腾讯成交量单位为股。两者经不同字段映射进入同一规范表，`data_source` 保留具体接口名。用 `cehuan fetch --source tencent ...` 或 `fetch-pool --source tencent ...` 可选择腾讯；默认来源为东方财富，不会静默切换上游。

2026-10-27 对贵州茅台（SH600519）进行小窗口实测：本机 AKShare 1.19.3 的东方财富接口在一次测试中两种复权请求均返回 5 行、字段符合文档；稍后的复测出现 `push2his.eastmoney.com` 代理断连。腾讯接口首次因测试脚本误传其不支持的 `period` 参数而失败，按实际签名修正后返回 2026-10-19 至 2026-10-26 共 6 行，字段与单位符合文档。曾在同一批 2026-10-19 至 2026-10-23 请求中核对末日原始 OHLC、成交量换算和成交额相符；两源当日后复权收盘价却分别为 8313.74 与 9316.38。复权绝对水平不可跨源拼接；完整逐日交叉核验未能完成。这证明腾讯适配器可工作，也证明单次成功不足以建立长期稳定性或全市场数据质量结论。

同一证券的行若包含多个 `data_source`，质量检查会报错，避免将不同供应商的价格/复权序列直接拼接。可以用 `--max-age-days` 加一个粗略的日历日新鲜度阈值；未提供官方交易日历时，这个检查不能替代“距最近开市日”的判断。

## 交易日历与点时主数据

`examples/trading_calendar_template.csv` 接受 `trade_date,is_open,previous_trade_date,source,retrieved_at_utc`，也识别 Tushare 风格的 `cal_date` 和 `pretrade_date` 列。`quality --calendar` 会检查每个行情日期是否为已知开市日；`plan --calendar` 会校验历史行情覆盖，并标注日历给出的下一个开市日期。请保留文件版本和来源，模板本身不包含真实交易日。

目前没有接入需要 Token/积分的历史状态供应商。Tushare 文档列出股票基础信息的上市/退市日期和状态、交易日历、每日停复牌、每日涨跌停价及每日 ST 名单；其接口权限分别要求积分（如股票基础信息、交易日历和涨跌停价为 2000 起，历史 ST 名单为 3000 起）。这是一条可评估的可选数据路径，不能假定当前账户已有权限或覆盖满足历史研究要求。证券主数据仍通过有效日期 CSV 输入，数据缺项保持未知。

参考文档：[AKShare A 股历史行情与复权说明](https://akshare.akfamily.xyz/data/stock/stock.html)、[AKShare 数据风险提示](https://github.com/akfamily/akshare/blob/main/docs/data_tips.md)；[Tushare 股票基础信息](https://tushare.pro/document/1?doc_id=25)、[交易日历](https://tushare.pro/document/2?doc_id=26)、[每日停复牌](https://tushare.pro/document/2?doc_id=214)、[每日涨跌停价](https://tushare.pro/document/2?doc_id=183)、[历史 ST 名单](https://tushare.pro/document/2?doc_id=397)、[复权因子](https://tushare.pro/document/2?doc_id=28)。

## 防泄漏规则

1. t 日特征只读取 t 日收盘及之前数据；任何订单最早放在下一交易日。
2. 每日接口的 `available_at` 按当日 15:00 Asia/Shanghai 记录为可用时间假设；接口实际到达时间另记 `retrieved_at_utc`。计划命令遇到未来的 `available_at` 会停止，避免收盘前用未完成日线生成计划。
3. 前复权价格可能随未来除权事件回改历史；AKShare 数据说明还展示了个别证券后复权历史价出现负值的风险。当前只有每只证券的后复权收盘完整且全为正时才用作信号；整只证券没有复权值时回退原始收盘；部分缺失或非正时该证券动量暂不计算，避免在一个收益序列中混用价格口径。严谨历史复现仍需保存每次获取的复权因子/数据快照。
4. 不能用当前 ST 名单或当前股票列表代替历史状态/历史成分。没有状态记录时，计划动作标成 `VERIFY` 或 `HOLD`，回测跳过未知状态行。
5. 交易所日历与停牌不能从缺失行情行准确推出。应提供时点证券主数据和交易所日历文件后再扩大股票池。数据行还会记录预期收盘可用时间和实际拉取时间；若最近交易日快照是在收盘可用时间前拉取，计划生成会要求收盘后重新获取。

## 需要定期检查的质量项

空字段、重复 (trade_date, symbol)、OHLC 不一致、非正价格、负成交量、代码映射、最后可用日和拉取时间。现有行情接口不支持完整的退市证券/历史股票池质量审计；这项不通过当前单股验证就被默认为已解决。
