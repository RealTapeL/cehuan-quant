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

`snapshot` 支持 `--symbols-file`、`--batch-size`（每批 1–30 只）、`--max-batches` 和 `--resume`。大任务的每批行情与复权因子独立保存；批次清单记录超时、最小请求间隔、重试数、退避和日请求预算。中断后用相同的代码清单、日期、基准、批大小和 `--snapshot-id ... --resume` 接着运行。最终快照以有序分块归并生成 `bars.csv`，避免把全历史行情一次性装入内存，并保留每批原始响应。未指定 `--max-batches` 时会顺序完成全部批次。

`cehuan universe-snapshot --start ... --end ... --snapshot-id ...` 按交易日请求 `query_all_stock(day)`，每个日期的原始列表单独保存，支持 `--batch-size N` 与 `--resume`。最终 `securities_by_date.csv` 按日追加写出，避免将全历史成员记录长期保存在内存；同时保留日期列表成员、最新基础资料类型、挂牌区间核对、当日 `tradeStatus`、ST 状态来源以及候选资格状态。`reconciliation.csv` 列出基础资料挂牌区间认为在市、但日期列表未出现的证券；`daily_coverage.csv` 汇总证券类型、交易状态、ST 未知、行情覆盖和已退市样本覆盖。`universe-audit` 也按日期流式对齐行情和成员名单。可用 `--bars data/private/snapshots/.../bars.csv` 核对行情覆盖。股票池成员由日期列表确定，但类型来自单独查询的基础资料；基础资料类型缺失的证券保留为 `UNKNOWN_TYPE` 并排除，不会推断成股票。

`universe-audit` 分别报告 `raw_bar_quality`（原始规范行情质量与来源 manifest 哈希）和 `universe_reconciliation_quality`（逐日成员覆盖、交易/停牌状态及 ST）。缺日线、状态冲突或关键状态未知时，对账健康状态为 false。只有 `configs/security_identity_events.csv` 登记且生效日旧/新代码成员与新代码有效行情均核验通过，旧代码缺行才可标为“已解释”；原始成员数和缺行情数保持不变。`docs/evidence/universe-outdate-boundary-audit.csv` 为 20 条 `outDate` 差异提供逐行登记。2026-10-29 已归档对应来源、补齐原件 SHA-256 并完成代码/日期文本匹配；这仍不等于用户人工签认或运行时打开公告复核。`SZ000851` 的深交所决定与发行人摘牌原件已分别登记；代码和最终摘牌日期由发行人公告确认，港交所通告仅作交叉核对。详见[三年数据准备记录](data-preparation-2023-2025.md)。

点时数据状态分层报告：`raw_bar_quality` 表示规范行情结构与来源哈希检查；`universe_reconciliation_quality` 表示已分类沪深 `type=1` 日成员与日线及状态的对账；`point_in_time_quality` 汇总前两项与身份事件解释，不证明未知类型或全市场历史完整；`baseline_ready` 由 `baseline-readiness` 审计生成，绑定评价窗口、输入 SHA-256、审计代码提交和工作区状态。编辑 TOML 或质量 JSON 不能替代该审计。当前回测引擎还没有实际消费审核过的有效日期交易规则、历史费率及完整公司行动账本，因此正式门控应保持 `NOT_READY`。正式 `backtest` 唯一接受同次 `universe-audit` 生成的过滤行情 `--data <point_in_time_bars.csv>` 与 `--prevalidated-quality <point_in_time_quality.json>`；`--formal --universe` 会被拒绝。`--readiness-universe` 是重新审计所用的原始股票池快照目录，不参与行情过滤。需要直接用股票池过滤原始行情时，应显式使用 `--diagnostic --universe`；`baseline-diagnostics` 也需显式 `--diagnostic`，其回退场景不属于正式基线。身份事件只处理 1:1 代码变更；新代码信号不接续旧代码价格历史。详情见[证券身份事件与公司行动边界](security-identity.md)。

BaoStock 0.9.4 原始 `socketutil.send_msg` 在 `recv()` 返回空字节时不会退出，可能永久空转；适配器在单连接会话中安装 EOF 防护，并设置 20 秒 socket 超时。进程内线程锁和跨进程文件锁共同避免本机并发会话。默认请求最小间隔 1 秒；连接错误最多重试 1 次并指数退避，明确的 `10001001`“用户未登录”响应最多重新登录 1 次后重试原请求；其他 API/权限错误不自动重试。默认本地每日预算 49,000 次（包含分页请求），低于官方 50,000 次限制。断点逐日记录 CSV 哈希和行数；续跑先校验成员、日历、基础资料和追加请求日志。哈希不同或结构不符即停止，不覆盖原文件。原有无哈希断点首次续跑时仅在结构、代码唯一性检查通过后登记本地哈希，并标明迁移来源。具体限制与源码行为见[数据源审计](data-provider-audit.md)。

若 BaoStock 在探测后仍不可用，可用 `cehuan universe-import` 导入另一供应商的历史成员 CSV。必须包含 `trade_date,symbol,source,source_record_date,source_retrieved_at_utc`；`source_record_date` 必须逐行等于 `trade_date`。可选字段为 `security_type,daily_trade_status,is_suspended,is_st,st_status_source,name`。缺失状态保持未知，重复 `(trade_date,symbol)` 拒绝导入。命令保留原文件与哈希，不会查询今天的股票列表或回填历史。模板与示例见[历史股票池 CSV 导入规范](historical-universe-import.md)。

点时名单全部验证后，`cehuan universe-symbols --universe <snapshot-or-partial> --output data/private/.../symbols.csv` 可导出整个历史窗口中至少出现过一次的 `type=1` 证券并集，原始日名单和基础资料哈希会写入 `.manifest.json`。股票行情快照按此并集查询；股票池保持 partial，待 `snapshot` 取得行情/复权因子后，再用 `universe-snapshot --resume --bars <bars.csv>` 对齐停牌、ST 和缺失行情并封存。`--defer-finalize` 用于在成员日期采集完成后暂停自动封存。

大快照核验通过后，可将生成的点时行情 CSV 交给 `backtest --prevalidated-quality <point_in_time_quality.json>`。该报告由 `universe-audit` 生成，绑定过滤后文件的 SHA-256，并继承源快照质量结果。该模式按类别编码日期、代码和低基数元数据节省内存；不得用它跳过质量或股票池校验。

BaoStock 的 `volume` 映射为股、`amount` 映射为元；`tradestatus=0` 映射为停牌，日线 `isST` 映射为 ST。`query_all_stock(day)` 不提供 ST 字段，缺少匹配历史日线的 ST 状态保持未知，不会被认作可交易。`ipoDate/outDate` 用于挂牌区间交叉核对，不覆盖日期列表成员事实。日线不含逐日涨跌停阈值或交易单位，快照保持这些字段未知。快照保存在 Git 忽略的 `data/private/`，拒绝覆盖已完成的快照 ID；部分任务保留在隐藏的 `.partial` 目录供续跑。

实测覆盖、权限状态、接口限制和本地快照版本见 [数据源审计](data-provider-audit.md)、[真实数据基线报告](real-data-baseline.md) 与[点时股票池核验记录](point-in-time-universe-validation.md)。

## 交易日历与点时主数据

`examples/trading_calendar_template.csv` 接受 `trade_date,is_open,previous_trade_date,source,retrieved_at_utc`，也识别 Tushare 风格的 `cal_date` 和 `pretrade_date` 列。`quality --calendar` 会检查每个行情日期是否为已知开市日；`plan --calendar` 会校验历史行情覆盖，并标注日历给出的下一个开市日期。请保留文件版本和来源，模板本身不包含真实交易日。

BaoStock 日线可为取得的日期提供 ST/停牌状态，日期成员接口单独提供成员与 `tradeStatus`。2023–2025 点时股票池与行情快照已完成，但已分类成员内部对账已通过，原始代码迁移缺行仍保留且另列解释；这不证明未分类记录和全市场历史成员完整。全历史股票池仍未完成。Tushare 文档列出上市/退市、交易日历、停复牌、涨跌停及历史 ST 接口，权限按端点分别控制；本机没有 Token，账户积分和实际覆盖未知。证券交易规则仍通过有效日期 CSV 输入，缺项保持未知。详见[数据源审计](data-provider-audit.md)与[三年数据审计](three-year-data-audit.md)。

参考文档：[AKShare A 股历史行情与复权说明](https://akshare.akfamily.xyz/data/stock/stock.html)、[AKShare 数据风险提示](https://github.com/akfamily/akshare/blob/main/docs/data_tips.md)；[BaoStock API](https://www.baostock.com/api/)、[证券基本资料](https://www.baostock.com/mainContent?file=stockBasic.md)、[复权因子](https://www.baostock.com/mainContent?file=factorInfo.md)；[Tushare 股票基础信息](https://tushare.pro/document/1?doc_id=25)、[交易日历](https://tushare.pro/document/2?doc_id=26)、[每日停复牌](https://tushare.pro/document/2?doc_id=214)、[每日涨跌停价](https://tushare.pro/document/2?doc_id=183)、[历史 ST 名单](https://tushare.pro/document/2?doc_id=397)、[复权因子](https://tushare.pro/document/2?doc_id=28)。

## 防泄漏规则

1. t 日特征只读取 t 日收盘及之前数据；任何订单最早放在下一交易日。
2. 每日接口的 `available_at` 按当日 15:00 Asia/Shanghai 记录为可用时间假设；接口实际到达时间另记 `retrieved_at_utc`。计划命令遇到未来的 `available_at` 会停止，避免收盘前用未完成日线生成计划。
3. 前复权价格可能随未来除权事件回改历史；AKShare 数据说明还展示了个别证券后复权历史价出现负值的风险。当前只有每只证券的后复权收盘完整且全为正时才用作信号；整只证券没有复权值时回退原始收盘；部分缺失或非正时该证券动量暂不计算，避免在一个收益序列中混用价格口径。严谨历史复现仍需保存每次获取的复权因子/数据快照。
4. 不能用当前 ST 名单或当前股票列表代替历史状态/历史成分。没有状态记录时，计划动作标成 `VERIFY` 或 `HOLD`，回测跳过未知状态行。
5. 交易所日历与停牌不能从缺失行情行准确推出。应提供时点证券主数据和交易所日历文件后再扩大股票池。数据行还会记录预期收盘可用时间和实际拉取时间；若最近交易日快照是在收盘可用时间前拉取，计划生成会要求收盘后重新获取。

## 需要定期检查的质量项

空字段、重复 (trade_date, symbol)、OHLC 不一致、非正价格、负成交量、代码映射、最后可用日和拉取时间。现有行情接口不支持完整的退市证券/历史股票池质量审计；这项不通过当前单股验证就被默认为已解决。


## 正式基线就绪报告

`cehuan baseline-readiness` 不执行回测，只检查点时质量报告、股票池快照、身份事件、公司行动覆盖、有效日期规则、费率、基准日历和配置，并将它们的 SHA-256 与评价窗口写入报告。正式运行还必须在相同输入下匹配此报告。现阶段回测器仍未将独立规则、费率和公司行动账本用于组合模拟，故程序会明确报告该阻断项，不能因 CSV 字段完整就放行。命令格式和空模板见[正式基线门控说明](baseline-readiness.md)。

三年 2023–2025 快照不是 2026-10-29 的当日计划数据。生成近期计划前必须重新核验最近行情是否在收盘后可用，并使用对应交易日历、证券状态与交易规则。


可以用 `cehuan universe-unknown-types --universe <snapshot-dir> --bars <bars.csv> --output <review.csv>` 按唯一代码汇总未知类型记录、出现窗口及对应行情行数。CSV 初始分类全为 `UNRESOLVED`；改为 `EXPLAINED_NON_STOCK` 或 `ADDED_AS_STOCK` 时必须提供 HTTPS 证据、归档文件 SHA-256、复核人和日期。代码前缀与是否有 OHLC 只作为观察字段，不用于推断类型。命令输出 CSV 与 `.summary.json`，并核对 `unknown_rows = explained_non_stock_rows + added_as_stock_rows + unresolved_rows`。2026-10-29 本地审计覆盖 13,197 个未知成员日：53 个代码均由交易所基金名录/基金交易表或具体基金公告支持为非股票，结果为 `EXPLAINED_NON_STOCK=13197`、`ADDED_AS_STOCK=0`、`UNRESOLVED=0`。剩余 43 个代码的代码、页码、链接和 SHA-256 见[逐码证据表](evidence/unknown-type-remaining-43-audit.csv)。来源文本已由 Codex 核对，用户人工签认仍待完成。详见[2023–2025 数据准备记录](data-preparation-2023-2025.md)。
