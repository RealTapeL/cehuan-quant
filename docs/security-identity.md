# 证券身份事件与公司行动边界

## 1:1 代码变更

`configs/security_identity_events.csv` 只接受 `event_type=CODE_CHANGE_1_TO_1`、`share_ratio=1`、`ratio_basis=old_shares_to_new_shares` 且状态为 `CONFIRMED` 的事件。当前登记的 `SZ300114 → SZ302132` 于 2025-02-17 生效；深交所实施公告说明原持股按 1:1 转为新代码。[深交所实施公告](https://disc.static.szse.cn/disc/disk03/finalpage/2025-02-15/cedb693a-f5ee-4463-9682-ea33d406b569.PDF)

回测将现有持仓数量、成本基础和持有记录作为单独账簿事件迁移，不产生买卖、费用、现金变化或已实现盈亏。新代码的动量历史从自身数据重新积累，不跨代码拼接。非 1:1 比例、包含并购的事件或不确定事件都不能进入这张表。

## SH601989 吸收合并

`configs/corporate_actions.csv` 将 `SH601989 → SH600150` 登记为独立的 `SHARE_SWAP_MERGER`，换股比例为旧股到新股 1:0.1339。时间点分开保存：旧股最后交易日 **2025-08-12**、换股登记日 **2025-09-04**、旧股摘牌日 **2025-09-05**、新股登记日 **2025-09-11**、新增股份上市流通日 **2025-09-16**。上交所公告确认旧股最后交易日和摘牌日；实施结果公告确认登记日、换股比例、整数股分配规则及新股流通日。[最后交易日公告](https://big5.sse.com.cn/site/cht/www.sse.com.cn/disclosure/listedinfo/announcement/c/new/2025-08-09/601989_20250809_7OO5.pdf) · [实施结果公告](https://static.cninfo.com.cn/finalpage/2025-09-12/1224653312.PDF) · [上交所摘牌公告](https://www.sse.com.cn/disclosure/announcement/listing/stock/c/c_20250829_10790128.shtml)

公告规定按换股后小数尾数排序分配整数股，尾数相同时可能由系统随机分配。现金选择权与整数股尾数分配是不同事项；本项目不把尾数记作现金补偿。个人最终获配股数必须以登记结算或券商结果确认。`corporate_action_phase()` 在新股实际可流通前不将换股权利当作可卖新股；获配股数未确认时保持未决。探索性上下界不能计入正式绩效。

模拟持仓按持仓批次保留旧股数量、成本和取得日期，不把既有 `SH600150` 持仓与换股批次合并。现金变化为零，但组合估值不能自动记零；目前公司行动账本、换股权利估值、分红送转/配股和退市结算尚未完整接入回测，因此含这些未决持仓的正式净值计算必须阻断。

预期账本字段、分红/送转/配股/换股/退市的合成穿透口径及 NAV 阻断条件，见[公司行动账务契约（设计稿）](corporate-action-accounting-contract.md)。该契约尚未由回测引擎实现。

## 点时名单证据

20 条 `outDate` 登记与快照的匹配由代码按 `(trade_date, symbol)` 校验；运行时不会下载或阅读公告。2026-10-29 已在私有目录归档对应原件，并在[公开证据索引](evidence/event-source-index.csv)及证据登记表写入标题、页码、SHA-256 和复核状态。归档文本已逐项比对预期代码和日期；这不等于维护者或用户人工签认，也不证明公告解释正确。`SZ000851` 现有深交所终止上市决定与发行人摘牌公告两份原始来源；港交所通告仅作交叉核对。

## 数据质量状态

- `raw_bar_quality`：规范行情字段、价格结构及来源文件校验。
- `universe_reconciliation_quality`：已分类 `type=1` 成员与日线、交易状态及名单证据的逐日对账。
- `point_in_time_quality`：以上两项及身份事件解释的组合结果；它不证明未知类型已分类，也不证明完整沪深历史成员覆盖。
- `baseline_ready`：由 `cehuan baseline-readiness` 重新审查输入后生成、绑定评价窗口和所有文件 SHA-256 的独立门控。不能由 TOML 布尔值或编辑 `healthy=true` 代替。当前规则/费率/公司行动账本尚未由模拟器实际消费，因此正式门控应保持关闭。

2023–2025 行情覆盖率分母只包括已分类的沪深 `type=1` 成员日。未知类型审计共 13,197 个成员日、53 个代码；交易所/基金公告来源现已逐码支持全部为基金类非股票，其中原 10 个代码为 5,059 日，本轮 43 个代码为 8,138 日。分类结果为 `UNRESOLVED=0`、`ADDED_AS_STOCK=0`，故股票池和股票行情无需重建；全部 13,197 日均不进入原 `type=1` 覆盖率分母。来源文字由 Codex 核对，用户人工签认仍待完成。快照没有北交所数据，不能称为“全 A 股历史完整率”。本机复核和阻断项见[三年数据准备记录](data-preparation-2023-2025.md)。该三年数据也不能支持 2026-10-29 的当日交易计划；计划另需近期行情、交易日历、证券状态和收盘可用时间核验。

## 本机复核

```bash
uv run cehuan universe-audit \
  --universe data/private/universes/bs-universe-20230101-20251231-v1 \
  --bars data/private/snapshots/bs-bars-universe-20221201-20251231-v1/bars.csv \
  --identity-events configs/security_identity_events.csv \
  --out-date-evidence docs/evidence/universe-outdate-boundary-audit.csv \
  --output data/private/audits/universe-audit-2023-2025-recheck
```

这条离线复核命令只重建数据质量与名单对账；它不会重跑基线或生成账户交易建议。
