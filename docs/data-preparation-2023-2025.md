# 2023–2025 沪深数据准备与基线门槛

记录日期：2026-10-29。范围是准备和审计已存在的 BaoStock 沪深快照。没有运行多年回测，没有修改 20 日动量参数，没有生成个人交易计划或调用华泰交易功能。正式基线仍为 `NOT_READY`。

## 本机复核结果

### 快照冻结

本机存在 `data/private/` 私有快照。原始目录未改写；已在 `data/private/backups/preparation-20261029/` 创建只读备份。备份仍处于 `.gitignore` 覆盖范围。

- 股票池：`bs-universe-20230101-20251231-v1`。manifest 列出的 734 个文件均存在且 SHA-256 匹配；checkpoint 的三项受校验文件匹配，采集请求日志哈希与 checkpoint 一致。额外的 `checkpoint.json` 是操作状态，不在 manifest 数据文件目录内。
- 行情：`bs-bars-universe-20221201-20251231-v1`。manifest 列出的 16,999 个文件均存在且 SHA-256 匹配；177 个分块 ID 连续，批次代码并集 5,309 个且无重复，与根 manifest 一致；逐批文件目录、代码与根 manifest 对齐。
- 只读备份中与两个 manifest 清单对应的文件均再次校验通过。`batch_checkpoint.json` 是未纳入不可变数据文件清单的操作状态文件，单独保留在备份中。
- 私有校验明细：`data/private/audits/preparation-20261029/snapshot-integrity.json`；运行输入版本、摘要和哈希见 `data/private/audits/preparation-20261029/run-manifest.json`。这些文件不提交 Git。

### 未知类型

运行 `universe-unknown-types` 后，53 个代码、13,197 个未知成员日满足分类分区恒等式。原先已有 10 个代码（5,059 日）由交易所/基金公告支持为 ETF；本轮用上交所基金名录、深交所年度基金交易表和一份上交所基金公告，为剩余 43 个代码（8,138 日）逐一找到精确代码与基金名称证据。没有代码被确认是普通股票，故没有修改点时股票池成员或重建股票行情。没有按代码前缀、OHLC 或交易状态推断类型。

- 公开复核表：[unknown-type-top10-audit.csv](evidence/unknown-type-top10-audit.csv)
- 剩余 43 个代码逐项证据（代码、成员日区间、证券简称、官方原件 URL、PDF 页码、SHA-256）：[unknown-type-remaining-43-audit.csv](evidence/unknown-type-remaining-43-audit.csv)。批量来源为[上交所统计年鉴（2023 卷）](https://big5.sse.com.cn/site/cht/www.sse.com.cn/aboutus/publication/yearly/documents/c/10791349/files/85c1e61617f44332ac2f04dbbe5cd530.pdf)、[深交所市场统计年鉴（2023）](https://docs.static.szse.cn/www/market/periodical/year/W020240814360443176703.pdf)、[深交所市场统计年鉴（2024）](https://docs.static.szse.cn/www/market/periodical/year/W020250912366937867370.pdf)；`SH562260` 另以[上交所基金公告](https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-05-07/562260_20240507_BPW5.pdf)确认基金代码与交易型开放式运作方式。原件归档在忽略目录，仅提交来源、页码和散列，避免提交大型私有证据文件。
- 全部 53 个代码的私有登记和机器摘要位于 `data/private/audits/preparation-20261029/unknown-types.csv` 及其 `.summary.json`；本机复跑输出 `explained_non_stock_rows=13197`、`added_as_stock_rows=0`、`unresolved_rows=0`、`unresolved_symbol_count=0`。
- 本次私有分类登记 SHA-256 为 `acb0484942e1e2393f0940741ab2bd330a46c77a84a012a1db09981d50f1fe92`；公开 43 代码证据 CSV SHA-256 为 `ee9002eb4421ac0cb1fe9a5fa6f52ac4c3162108179cff9bf75c044014fded34`。未知类型分类命令没有重建点时股票池或行情，因为 `ADDED_AS_STOCK=0`。
- `reviewed_by` 记录的是 Codex 对归档来源文本和表格上下文的核对，**不是用户人工签认**。来源原件哈希只证明引用文件字节未改变；用户仍需人工签认来源含义与适用期间。
- 首次出现日 BaoStock 原始记录、日文件 SHA 与基础资料查找结果位于私有 `unknown-type-top10-raw-observations.csv`。这些是来源字段观察，不是类型判断。
- 事件公告文本检查由程序完成；`reviewed_by` 明确标记“Codex archive-content check; user sign-off pending”。这不是用户人工签认。未知类型分类中的 `UNRESOLVED` 已为零，但事件证据签认、交易规则、费用、公司行动和执行门槛仍阻止正式基线。

### 公告证据

本地私有目录中归档 25 份原始来源：20 项 `outDate`、`SZ300114 → SZ302132` 代码变更、`SH601989` 吸收合并实施结果和最后交易日两份材料，以及 `SZ000851` 的深交所终止上市决定与发行人摘牌公告两份补充材料。下载文件 SHA-256 与公开登记相连；文本提取检查了预期证券代码和日期。合并实施结果公告的提取文本确认新代码与各关键日期，旧代码的退市日期另由终止上市公告核对；索引逐行公开预期值与实际提取值。原始 PDF/HTML 留在忽略目录，不提交到 Git。

- 公开索引：[event-source-index.csv](evidence/event-source-index.csv)
- `outDate` 登记：[universe-outdate-boundary-audit.csv](evidence/universe-outdate-boundary-audit.csv)
- `SZ000851` 现有[深交所终止上市决定公告](https://www.szse.cn/disclosure/notice/company/t20251103_617044.html)及[发行人摘牌公告](https://static.cninfo.com.cn/finalpage/2025-11-11/1224797210.PDF)：前者确认公司与决定日期，后者确认证券代码及 2025-11-11 摘牌日期；港交所通告保留为交叉核对。公告原件的归档散列、页码和文本核对状态见索引。
- `SH601989` 证据表分开记录登记日、旧股最后交易日、摘牌日、新股登记日和新股可流通日。公告规则是按换股后小数尾数排序分配整数股，尾数相同可能随机分配；这不等于现金补尾。个人最终获配数仍未确认，事件状态保持 `UNRESOLVED_ACCOUNT_ALLOCATION`。
- SHA-256 只能确认被引用的归档字节未改变，不能证明公告结论真实完整。Codex 已对登记字段与归档文本作核对；维护者/用户对全部来源的人工签认仍待完成。

### 市场规则与费用来源状态

私有来源状态表：`data/private/audits/preparation-20261029/market-fee-source-status.csv`。该表是缺口清单，**不是**可用于回测的 `market-rules.csv` 或 `fee-schedule.csv`。

- SSE/SZSE 2023 主板注册制规则的关键生效边界为首批主板注册制股票上市日 2023-04-10；这不表示所有板块、证券和 2023 年 1 月至 4 月 9 日都可套用同一套规则。[上交所首批上市安排](https://big5.sse.com.cn/site/cht/www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20230404_5719112.shtml) · [深交所 2023 交易规则说明](https://investor.szse.cn/institute/rules/t20230621_601279.html)
- 2026 年修订规则自 2026-07-06 生效，晚于本研究窗口；不用于回填 2023–2025。[上交所规则通知](https://big5.sse.com.cn/site/cht/www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml) · [深交所规则通知](https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html)
- 证券交易印花税按卖出方承担；本窗口按 2023-08-27 前 1‰、2023-08-28 起 0.5‰登记，依据《印花税法》和减半公告。[印花税法](https://gtm-cn-uqm3dbhc20k.guangdong.chinatax.gov.cn/gdsw/jysw_swzcjjd/2023-03/06/content_416fccf0a02443dbd6a4f0d24e04ddd42.shtml) · [2023 年减半公告](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html)
- 中国结算 2022-07 官方收费表列出 A 股过户费 0.01‰、买卖双方收取；2022-04-29 的降费起始日尚待将原始通知归档并核验，故该有效日期仍标为待补证。[中国结算收费表](https://www.chinaclear.cn/zdjs/editor_file/20220701154723234.pdf)
- 没有华泰个人佣金/最低佣金合同或脱敏交割单；`configs/default.toml` 费率只是示例。没有逐证券逐日板块、ST、停牌、涨跌停价/比例及买卖数量规则表。

本轮进一步完成了公开费用来源的本机归档与文本核对：法定印花税及 2023-08-28 减半公告、沪深交易经手费 2023-08-28 调整、调整前经手费基准。相关公开来源与归档 SHA-256 见[费用证据表](evidence/market-fee-evidence.csv)，费用口径和阻断状态见[费用审计说明](market-fee-audit.md)。中国结算官网费用表显示 A 股过户费 0.01‰ 双向；官网大事记确认 2022-04-28 发布降费通知，但本机下载费用表和原始通知均返回 HTTP 404。通知转载写明 2022-04-29 生效，因此仍把“原始公告归档/有效日核验”留为阻断，不伪装成完整官方原件链。

逐日底层方面，流式扫描 v3 过滤行情的 3,700,243 行、727 个交易日，BaoStock is_suspended 与 is_st 字段缺失均为 0；日期证券列表的 tradeStatus 与行情停牌状态对账未知/冲突均为 0。点时行情的 board、has_price_limit、price_limit_pct、买卖最小数量和递增单位、规则来源及有效期字段则在全部 3,700,243 行中为空。is_listed 有值但来自采集时基础资料的 ipoDate/outDate 派生，并不是每日独立挂牌来源；ST 字段也仍需独立来源抽查。只凭日线、证券代码前缀或当前交易规则不能生成可信的历史完整规则文件。华泰个人佣金未知；公费率核对完成不代表个人账户成本校准。

## 最新点时审计

更新公开证据登记、完成 53 个未知类型代码分类并补入 `SZ000851` 原件后，在私有目录重新运行 `universe-audit`。最新输出、质量报告和过滤后行情保存在 `data/private/audits/preparation-20261029/universe-audit-v3/`。本机结果：`raw_bar_quality.healthy=true`、`universe_reconciliation_quality.healthy=true`、`point_in_time_quality.healthy=true`、`baseline_ready=false`；3,700,244 个已分类沪深成员日中 3,700,243 有日线，1 行仍是旧代码 `SZ300114` 缺行情但由已登记 1:1 代码事件解释，未解释缺行 0；20/20 条 `outDate` 行的登记、证据字段与快照匹配。它只表示该分类范围内的结构/对账通过，不证明全市场历史成员完整。过滤行情 SHA-256 为 `d9cf4505a4b1565edda2b5dbf0e40841fad2fac900e4bb623e0f7685e5a8a393`，v3 质量 JSON SHA-256 为 `d80daf39361d5e65fd8dda565a22977af539322cbb465a769c2c928dca905ac8`。

本机实跑结果：

```text
universe-unknown-types:
  unknown_rows=13197
  unknown_symbols=53
  explained_non_stock_rows=13197
  added_as_stock_rows=0
  unresolved_rows=0
  unresolved_symbol_count=0
  classification_partition_balances=true
```

重跑命令：

```bash
uv run cehuan universe-unknown-types \
  --universe data/private/universes/bs-universe-20230101-20251231-v1 \
  --bars data/private/snapshots/bs-bars-universe-20221201-20251231-v1/bars.csv \
  --output data/private/audits/preparation-20261029/unknown-types.csv

uv run cehuan universe-audit \
  --universe data/private/universes/bs-universe-20230101-20251231-v1 \
  --bars data/private/snapshots/bs-bars-universe-20221201-20251231-v1/bars.csv \
  --identity-events configs/security_identity_events.csv \
  --out-date-evidence docs/evidence/universe-outdate-boundary-audit.csv \
  --output data/private/audits/preparation-20261029/universe-audit-v3
```

## 阻断项与验收

| 阻断项 | 需要补充 | 验收方式 | 状态 |
|---|---|---|---|
| 未知证券类型 | 无未解决代码；保留 53 个代码的来源记录；若后续证据推翻分类并确认普通股票，更新点时股票池和行情 | 分类分区平衡；当前 `UNRESOLVED=0`、`ADDED_AS_STOCK=0`；用户人工签认来源 | 代码证据完成，待签认 |
| 证据人工复核 | 25 份身份/退市来源与 53 个未知类型分类来源已完成 Codex 文本/表格核对；`SZ000851` 深交所决定和发行人摘牌原件已登记 | 用户人工签认来源标题、内容和适用日期；保存审核人/日期/页码 | 待人工签认 |
| 有效日期交易规则 | 每个评价证券日唯一的板块、上市/停牌/ST、价格限制、数量单位规则及权威来源 | 逐证券日无缺口/重叠；专项核对上市首日、ST、停牌、除权、涨跌停、退市日 | 未完成 |
| 历史费用 | 华泰脱敏费率或交割单；补齐过户费正式生效通知；计算口径 | 每个交易日有一条适用费率；买卖方向、最低佣金、税费与样例账单相符 | 未完成 |
| 公司行动覆盖/账本 | 全股票池分红、送转、配股、吸收合并、退市与结算事件；`SH601989` 个人最终获配数 | 现金、持股、应收、可交易数量和权益逐日守恒；未决事件阻断正式净值 | 未完成 |
| 执行引擎 | 让正式回测实际读取规则、费率、公司行动账本 | 缺行、旧哈希、冲突、未决权益均阻断；正式入口零回退 | 未完成 |

**本轮状态：**剩余 43 个未知代码已逐项获得交易所基金/ETF来源，分类审计中的 `UNRESOLVED` 已清零；点时股票池及行情没有需要重建的普通股票。`SZ000851` 原始证据已补充并触发 v3 审计。用户人工签认、逐证券日交易规则、历史费用、公司行动账本及执行引擎门槛仍未清零。正式基线保持 `NOT_READY`，不能据此运行多年正式基线或输出个人交易建议。
