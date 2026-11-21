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

## 固定池采集入口

`lookback-pair-capture` 负责采集，不运行排名。它只接受登记的 2026-12-17 信号日、冻结配置、冻结的 10 只证券及身份事件文件；固定使用 BaoStock 适配器。完整首轮包含 52 个数据查询：1 次日历、10 次单证券基础资料、21 次逐日成员名单、20 次日线（每只证券原始与后复权各一次）。日线只请求冻结的 10 只证券，基础资料请求也按证券代码限定。BaoStock 的 [Python API 文档](https://www.baostock.com/baostock/index.php/Python_API%E6%96%87%E6%A1%A3)将 `query_all_stock(day)` 描述为指定交易日的全部证券列表；该接口不能按股票代码缩小响应，因此其 21 份原始响应会完整留档，规范化的 `point-in-time-universe.csv` 只保留冻结的 10 只证券，不对其他证券采集行情或生成排名。

成员响应完整性有额外限制：项目适配器会遍历 SDK 分页，并在 `next()` 失败但当前页仍满页时报告传输/分页失败；不过 BaoStock 响应没有独立的预期总行数或内容校验和，适配器不能证明一个“成功结束但行数偏少”的响应完整。因此每个成员响应必须包含冻结池全部 10 个规范代码。非空响应若缺任一固定代码，将作为校验失败按登记时点有限重试；最终仍缺失时，该日期固定池的 `pool_status` 全部记为 `UNKNOWN`（缺席代码另标 `observed_in_response=false`），绝不推断为 `NOT_STOCK_MEMBER`，并以 `NEEDS_DATA` 阻断。每次原始响应及哈希仍保留。日历、成员或行情缺失、字段/证券代码冲突、重复日期、未知上市/ST/停牌状态都会保留证据并阻断 `CAPTURED_INPUTS_READY`。

仅在 2026-12-17 当日按以下固定窗口执行。首采应在 15:06 启动；在 15:20 和 15:35 仅对之前明确失败的请求重试。每项最多三次，单连接、串行、每次请求至少间隔一秒；库内自动重试关闭。15:45 后不发请求，16:00 后不封存为按时结果。每次重试都使用同一目录和 `--resume`，不得创建新目录来绕过失败日：

```bash
uv run cehuan lookback-pair-capture \
  --signal-date 2026-12-17 \
  --output-dir data/private/experiments/lookback-10-vs-20-forward-20261217-v1/capture-20261217-v1
```

如果输出状态为 `PARTIAL_CAPTURE_RETRY_2_PENDING` 或 `PARTIAL_CAPTURE_RETRY_3_PENDING`，在相应登记时点运行相同命令并加 `--resume`。成功时会封存 `capture-manifest.json`、`.seal.json`、规范行情/日历/逐日状态表、逐次 BaoStock SDK DataFrame 响应 CSV、会话及请求日志和文件哈希；这里的“原始响应”是 SDK 返回表格的序列化文件，不是协议层网络字节。运行入口不会抓取其他供应商，不生成排名、收益、股数或订单。

`CAPTURED_INPUTS_READY` 仅表示信号排名所需的输入通过采集校验，不代表整个 20 日结果观察期可用。`calendar_outcome_horizon_status` 单独说明信号日之后 20 个开市日是否完整；缺少时仍是结果期阻断，不能据此计算后续表现。

### 2026-11-20 的日历预检

本机于 2026-11-20 09:12:43—09:12:44（Asia/Shanghai）只请求了一次 BaoStock `query_trade_dates`，请求区间为 2026-01-01 至 2027-03-31；没有请求行情或证券状态。SDK 返回 365 行、字段 `calendar_date,is_trading_day`，实际末日为 2026-12-31。与登记日历重叠 365 行全部匹配，但 2026-12-17 之后只有 10 个开市日，故 20 日结果日历覆盖状态为 `NEEDS_DATA`。没有用其他来源补接或猜测剩余日期。

本次私有审计产物位于 `data/private/experiments/lookback-10-vs-20-calendar-20261120-v1/`：原始 SDK 响应 SHA-256 `c398c159134333d8d4f526f313f1db4fc162eb98bd330afc1bfc157df97880b8`，规范日历 SHA-256 `0bae148812b818d0ede8f4e70bb9ab8b9c2a1b6a7d13eaf5ac0d4b290226ee43`，请求日志 SHA-256 `3fb8c759a27345f3bde0ac806668014e239d1324fe7a7a3dada3e98800863b61`。供应商发布时间仍为 `UNVERIFIED`。这些文件是此次日历预检，不是 2026-12-17 的实验采集输入。

## 输入契约

- `registration-freeze.json` 及同目录 `.sha256` 必须有效，实验 ID、固定信号日、10 只证券、冻结配置和原研究池哈希必须匹配。
- 若运行日历比冻结参考日历长，必须覆盖参考日历所有日期；重叠部分的开市状态逐日相同。结果期需要的 20 个后续开市日仍由之后单独核验；本命令不以信号排名入口代替结果期门控。
- `capture-manifest.json` 必须含实验 ID、信号日、BaoStock provider、带时区的 `captured_at_utc`、`input_sha256` 和 `trusted_external_timestamp="UNVERIFIED"`。`interfaces` 必须精确为 `{ "bars": "query_history_k_data_plus", "calendar": "query_trade_dates", "security_basic": "query_stock_basic(code=<fixed symbol>)", "universe": "query_all_stock" }`；同时记录 `signal_price_adjustment="adjustflag=1"` 与 `raw_price_adjustment="adjustflag=3"`。`input_sha256` 必须逐项绑定 `bars`、`calendar`、`universe`、`pool`、`identity_events`、`registration_freeze` 文件。另记录 `provider_published_at_utc` 与 `provider_publication_evidence`；未知时填 null/`UNVERIFIED`。这些清单字段和哈希用于输入来源追溯，不等于独立证明供应商发布时间或公告内容。
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
