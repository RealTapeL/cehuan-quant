# 数据规范和时点边界

规范行情字段：

| 字段 | 含义 |
|---|---|
| `trade_date`, `symbol`, `name` | 交易日、交易所前缀代码（如 `SH600519`、`SZ000001`、`BJ830799`；指数如 `SH000300`）、可选名称；六位股票代码和 `.SH/.SS/.SZ/.BJ` 输入会统一 |
| `open`, `high`, `low`, `close` | 原始未复权价格；用于参考价与初版成交约束 |
| `adjusted_close` | 后复权收盘价；用于技术基线连续性较好的收益特征 |
| `volume_shares`, `amount_cny` | 股、元；AKShare 返回成交量单位为手，适配器乘 100 |
| `is_listed`, `is_suspended`, `is_st` | 点时上市/停牌/ST 状态；AKShare 单股日线不提供，缺失保留未知；BaoStock 快照可映射上市日期及日线停牌/ST 字段 |
| `has_price_limit`, `price_limit_pct` | 当日是否有限价及其阈值；需来自有效日期证券主数据，IPO 等无涨跌幅限制日期可设 `has_price_limit=false` |
| `buy_min_shares`, `buy_step_shares`, `sell_min_shares`, `sell_step_shares` | 按板块和当日规则配置的数量约束；不可把全市场假设成同一手数 |
| `board`, `market_rule_source`, `market_rule_valid_from`, `market_rule_valid_to` | 板块、规则证据来源及有效期，用于追溯点时规则 |
| `data_source`, `retrieved_at_utc`, `available_at` | 来源、实际拉取时间、按收盘后可用假设的时间 |

目前适配 AKShare 的两个独立日线来源：东方财富 `stock_zh_a_hist(symbol, period="daily", start_date, end_date, adjust, timeout)` 与腾讯 `stock_zh_a_hist_tx(symbol, start_date, end_date, adjust, timeout)`。官方文档将两者都描述为盘后更新；东方财富成交量单位为手，腾讯成交量单位为股。两者经不同字段映射进入同一规范表，`data_source` 保留具体接口名。用 `cehuan fetch --source tencent ...` 或 `fetch-pool --source tencent ...` 可选择腾讯；默认来源为东方财富，不会静默切换上游。

2026-10-27 对贵州茅台（SH600519）进行小窗口实测：本机 AKShare 1.19.3 的东方财富接口在一次测试中两种复权请求均返回 5 行、字段符合文档；稍后的复测出现 `push2his.eastmoney.com` 代理断连。腾讯接口首次因测试脚本误传其不支持的 `period` 参数而失败，按实际签名修正后返回 2026-10-19 至 2026-10-26 共 6 行，字段与单位符合文档。曾在同一批 2026-10-19 至 2026-10-23 请求中核对末日原始 OHLC、成交量换算和成交额相符；两源当日后复权收盘价却分别为 8313.74 与 9316.38。复权绝对水平不可跨源拼接；完整逐日交叉核验未能完成。这证明腾讯适配器可工作，也证明单次成功不足以建立长期稳定性或全市场数据质量结论。

同一证券的行若包含多个 `data_source`，质量检查会报错，避免将不同供应商的价格/复权序列直接拼接。可以用 `--max-age-days` 加一个粗略的日历日新鲜度阈值；未提供官方交易日历时，这个检查不能替代“距最近开市日”的判断。

## BaoStock 私有快照适配器

BaoStock 是显式可选适配器，不改变 AKShare 默认数据源。运行 `uv sync --extra baostock` 后，可执行 `cehuan snapshot --symbols SH600519,SZ000001 --start 20100101 --end 20261026`。命令保存未复权日线、`adjustflag=1` 后复权日线、复权因子、全量股票基础表、原始交易日历和规范化交易日历，并生成 `bars.csv`、质量报告、`manifest.json` 和 SHA-256 文件清单。指定的指数基准另存为 `benchmark.csv`，不会加入选股池；回测使用 `--benchmark-data` 显式加载。

`snapshot` 支持 `--symbols-file`、`--batch-size`（每批 1–30 只）、`--max-batches` 和 `--resume`。大任务的每批行情与复权因子独立保存；中断后用相同的代码清单、日期、基准、批大小和 `--snapshot-id ... --resume` 接着运行。最终快照以有序分块归并生成 `bars.csv`，避免把全历史行情一次性装入内存，并保留每批原始响应。未指定 `--max-batches` 时会顺序完成全部批次。

`cehuan universe-snapshot --start ... --end ... --snapshot-id ...` 按交易日请求 `query_all_stock(day)`，每个日期的原始列表单独保存，支持 `--batch-size N` 与 `--resume`。最终 `securities_by_date.csv` 按日追加写出，避免将全历史成员记录长期保存在内存；同时保留日期列表成员、最新基础资料类型、挂牌区间核对、当日 `tradeStatus`、ST 状态来源以及候选资格状态。`reconciliation.csv` 列出基础资料挂牌区间认为在市、但日期列表未出现的证券；`daily_coverage.csv` 汇总证券类型、交易状态、ST 未知、行情覆盖和已退市样本覆盖。`universe-audit` 也按日期流式对齐行情和成员名单。可用 `--bars data/private/snapshots/.../bars.csv` 核对行情覆盖。股票池成员由日期列表确定，但类型来自单独查询的基础资料；基础资料类型缺失的证券保留为 `UNKNOWN_TYPE` 并排除，不会推断成股票。

大快照核验通过后，可将生成的点时行情 CSV 交给 `backtest --prevalidated-quality <point_in_time_quality.json>`。该报告由 `universe-audit` 生成，绑定过滤后文件的 SHA-256，并继承源快照质量结果。该模式按类别编码日期、代码和低基数元数据节省内存；不得用它跳过质量或股票池校验。

BaoStock 的 `volume` 映射为股、`amount` 映射为元；`tradestatus=0` 映射为停牌，日线 `isST` 映射为 ST。`query_all_stock(day)` 不提供 ST 字段，缺少匹配历史日线的 ST 状态保持未知，不会被认作可交易。`ipoDate/outDate` 用于挂牌区间交叉核对，不覆盖日期列表成员事实。日线不含逐日涨跌停阈值或交易单位，快照保持这些字段未知。快照保存在 Git 忽略的 `data/private/`，拒绝覆盖已完成的快照 ID；部分任务保留在隐藏的 `.partial` 目录供续跑。

实测覆盖、权限状态、接口限制和本地快照版本见 [数据源审计](data-provider-audit.md)、[真实数据基线报告](real-data-baseline.md) 与[点时股票池核验记录](point-in-time-universe-validation.md)。

## 交易日历与点时主数据

`examples/trading_calendar_template.csv` 接受 `trade_date,is_open,previous_trade_date,source,retrieved_at_utc`，也识别 Tushare 风格的 `cal_date` 和 `pretrade_date` 列。`quality --calendar` 会检查每个行情日期是否为已知开市日；`plan --calendar` 会校验历史行情覆盖，并标注日历给出的下一个开市日期。请保留文件版本和来源，模板本身不包含真实交易日。

BaoStock 快照现在可以为明确指定的证券提供日线 ST/停牌状态以及上市、退市日期，但本项目尚未建立完整全市场的历史证券状态表，也没有接入 Tushare。Tushare 文档列出上市/退市、交易日历、停复牌、涨跌停及历史 ST 接口，权限按端点分别控制；本机没有 Token，账户积分和实际覆盖未知。证券交易规则仍通过有效日期 CSV 输入，缺项保持未知。

参考文档：[AKShare A 股历史行情与复权说明](https://akshare.akfamily.xyz/data/stock/stock.html)、[AKShare 数据风险提示](https://github.com/akfamily/akshare/blob/main/docs/data_tips.md)；[BaoStock API](https://www.baostock.com/api/)、[证券基本资料](https://www.baostock.com/mainContent?file=stockBasic.md)、[复权因子](https://www.baostock.com/mainContent?file=factorInfo.md)；[Tushare 股票基础信息](https://tushare.pro/document/1?doc_id=25)、[交易日历](https://tushare.pro/document/2?doc_id=26)、[每日停复牌](https://tushare.pro/document/2?doc_id=214)、[每日涨跌停价](https://tushare.pro/document/2?doc_id=183)、[历史 ST 名单](https://tushare.pro/document/2?doc_id=397)、[复权因子](https://tushare.pro/document/2?doc_id=28)。

## 防泄漏规则

1. t 日特征只读取 t 日收盘及之前数据；任何订单最早放在下一交易日。
2. 每日接口的 `available_at` 按当日 15:00 Asia/Shanghai 记录为可用时间假设；接口实际到达时间另记 `retrieved_at_utc`。计划命令遇到未来的 `available_at` 会停止，避免收盘前用未完成日线生成计划。
3. 前复权价格可能随未来除权事件回改历史；AKShare 数据说明还展示了个别证券后复权历史价出现负值的风险。当前只有每只证券的后复权收盘完整且全为正时才用作信号；整只证券没有复权值时回退原始收盘；部分缺失或非正时该证券动量暂不计算，避免在一个收益序列中混用价格口径。严谨历史复现仍需保存每次获取的复权因子/数据快照。
4. 不能用当前 ST 名单或当前股票列表代替历史状态/历史成分。没有状态记录时，计划动作标成 `VERIFY` 或 `HOLD`，回测跳过未知状态行。
5. 交易所日历与停牌不能从缺失行情行准确推出。应提供时点证券主数据和交易所日历文件后再扩大股票池。数据行还会记录预期收盘可用时间和实际拉取时间；若最近交易日快照是在收盘可用时间前拉取，计划生成会要求收盘后重新获取。

## 需要定期检查的质量项

空字段、重复 (trade_date, symbol)、OHLC 不一致、非正价格、负成交量、代码映射、最后可用日和拉取时间。现有行情接口不支持完整的退市证券/历史股票池质量审计；这项不通过当前单股验证就被默认为已解决。
