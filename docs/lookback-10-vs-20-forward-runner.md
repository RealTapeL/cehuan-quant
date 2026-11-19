# 10 日与 20 日动量配对排名入口

本文件记录独立入口的执行契约。研究规则仍以冻结登记 [lookback-10-vs-20-forward-registration.md](lookback-10-vs-20-forward-registration.md) 为准；本实现不改写登记，也不使用 H1 排名或 H1 结果。

## 命令

未来仅在登记的信号日收盘后、输入齐备时运行：

```bash
uv run cehuan lookback-pair-rank \
  --data data/private/experiments/lookback-10-vs-20-forward-20261217-v1/capture/bars.csv \
  --calendar data/private/experiments/lookback-10-vs-20-forward-20261217-v1/capture/trading-calendar.csv \
  --universe data/private/experiments/lookback-10-vs-20-forward-20261217-v1/capture/point-in-time-universe.csv \
  --pool data/private/experiments/forward-small-20261106-akshare-v1/selected_pool.csv \
  --capture-manifest data/private/experiments/lookback-10-vs-20-forward-20261217-v1/capture/capture-manifest.json \
  --signal-date 2026-12-17 \
  --output-dir data/private/experiments/lookback-10-vs-20-forward-20261217-v1/runs/20261217T160000+0800-v1
```

输出目录必须是 `data/private/` 下尚不存在的新目录。命令不抓取数据；它只读取、校验和生成排名。前瞻模式要求信号日当天本机时间晚于 15:05、行情逐行 `retrieved_at_utc` 不早于该时点且不晚于运行时间，并验证采集清单绑定的文件哈希。BaoStock 的适配器 `available_at=15:00` 不用于证明供应商发布时间；供应商发布时间没有独立证据时保留 `UNVERIFIED`。

## 输入契约

- `registration-freeze.json` 及同目录 `.sha256` 必须有效，实验 ID、固定信号日、10 只证券、冻结配置和原研究池哈希必须匹配。
- 若运行日历比冻结参考日历长，必须覆盖参考日历所有日期；重叠部分的开市状态逐日相同。结果期需要的 20 个后续开市日仍由之后单独核验；本命令不以信号排名入口代替结果期门控。
- `capture-manifest.json` 必须含实验 ID、信号日、BaoStock provider、带时区的 `captured_at_utc`、`input_sha256` 和 `trusted_external_timestamp="UNVERIFIED"`。`interfaces` 必须精确为 `{ "bars": "query_history_k_data_plus", "calendar": "query_trade_dates", "universe": "query_all_stock" }`；同时记录 `signal_price_adjustment="adjustflag=1"` 与 `raw_price_adjustment="adjustflag=3"`。`input_sha256` 必须逐项绑定 `bars`、`calendar`、`universe`、`pool`、`identity_events`、`registration_freeze` 文件。另记录 `provider_published_at_utc` 与 `provider_publication_evidence`；未知时填 null/`UNVERIFIED`。这些清单字段和哈希用于输入来源追溯，不等于独立证明供应商发布时间或公告内容。
- 成员 CSV 对固定池每只证券、信号日及此前 20 个开市日必须恰有一行，字段为 `trade_date,symbol,pool_status`；`pool_status` 只能为 `STOCK_MEMBER`、`NOT_STOCK_MEMBER` 或 `UNKNOWN`。缺行、重复、未知状态均阻断整日。
- 规范 bars 必须给每个已确认成员日提供唯一行、原始 OHLC、正数 `adjusted_close`、`is_listed`、`is_suspended`、`is_st`、单一非空 `data_source` 和有效 `retrieved_at_utc`。adjusted close 必须在完整共同 21 日窗口内始终可用；不回退到 raw close，不混用供应商。
- 身份事件表按配置路径读取并计入采集清单哈希。窗口内触及固定池代码的事件当前不自动拼接，整日 `NEEDS_DATA`。事件表无记录不构成“无事件”证明；人工核实覆盖仍是结果期门槛。

## 处理与产物

程序复用 `load_config()`、规范 bars/calendar 适配器、`add_momentum()` 和 `rank_precomputed_day()`。基线使用登记冻结的 20 日参数；候选在内存副本中只改 `strategy.lookback_sessions` 为 10。程序递归比较两份有效配置，差异不是且仅不是这一项时拒绝运行；不会写回 `configs/default.toml`。运行清单记录实际代码文件哈希，并校验登记冻结的动量实现哈希。

两组共享同一固定池、信号日和共同资格集合。已确认的非成员、未上市、信号日 ST/停牌及共同 21 日窗口内停牌按登记规则从两组共同资格中排除并逐证券记录；成员、上市、ST 或停牌任何必需状态未知，或行情缺日/重复/价格无效/身份事件未解决，均阻断整日并且不产出排名。20 日与 10 日分别按共同配置中的 `min_momentum` 生成候选，故候选数量可因信号值不同而不同；差异文件仍逐一列出全部共同资格证券及其信号、入选状态和名次。

成功产物包括：

- `ranking-20d.csv` 与 `ranking-10d.csv`：两组完整候选排序及 Top 5 标记；
- `ranking-difference.csv`：共同资格内逐证券的双周期信号、候选资格、名次差和 Top 5 变化；
- `per-security-audit.csv`：资格状态、阻断或排除原因及 21 日窗口摘要；
- 两份有效配置、`config-difference.json`、`manifest.json` 和 `manifest.sha256`：登记/输入/产物哈希、执行时间和状态。

被阻断时保留 `NEEDS_DATA` manifest 与逐证券审计，不写排名文件。代码不计算未来收益、NAV、回撤或模拟成交，不输出股数、目标仓位或订单；正式基线保持 `NOT_READY`。

截至该实现，日历完整 20 日结果尾段、12 月 17 日实际采集可用性和固定池公司行动/身份事件覆盖仍未通过核验。单日信号排名入口的存在不会解除这些阻断。
