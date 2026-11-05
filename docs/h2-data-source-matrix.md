# H2 行情时点数据来源矩阵

核对日期：2026-11-05。此矩阵核对公开接口文档、策环当前本地适配器和一次本机只读 AKShare 小样。单次成功不证明接口持续稳定、历史版本完整或数据在事件时刻可得。

## 时间字段必须分开

每条观察至少区分：

- **事件时间**：行情或状态对应的交易时刻。
- **供应商发布时间**：供应商实际向客户端发布该条数据的时刻。历史接口通常返回事件时间，不一定返回发布时间。
- **本机抓取时间**：策环收到响应并写入私有归档的 UTC 时间。

事件时间不能替代发布时间。本机抓取时间能证明这台机器何时收到一份响应，但不能证明历史上更早时点已有相同数据，也不能证明该价格可以成交。

## 可查来源

| 来源/接口 | 文档所述数据与范围 | 权限/限制 | 时间证据与当前判断 |
|---|---|---|---|
| AKShare `stock_zh_a_hist_min_em`（东方财富） | 沪深京 A 股分钟线；1/5/15/30/60 分钟。文档说明只提供近期分钟数据；1 分钟数据只返回近 5 个交易日，且不复权；示例提醒只有最近一个交易日有开盘价，较早日期的开盘字段为 0。 | 文档未列出单独权限或费用；依赖上游网页接口，稳定性需实测。 | 输出含分钟时间，但文档没有供应商历史发布时间或不可变版本字段。一次本机小样已成功，不足以直接重建多年历史 09:31 可用信息，也不能说明持续稳定。 |
| Tushare Pro 历史分钟（`pro_bar` / 分钟数据权限） | 1/5/15/30/60 分钟；按证券和时间段获取。 | 需单独开通分钟权限；单次最多 8,000 行；文档称日终处理通常在收盘后 17–21 点完成，并限制用途为策略研究和学习。具体权限和费用以账户实际页面为准。 | 返回分钟事件时间；日终更新意味着历史下载结果并不能证明当日 09:31 时客户端已经收到同一版本。供应商发布时间和历史修订版本未由已核对页面承诺。策环尚未验证账户权限。 |
| Tushare Pro A 股实时分钟（`rt_min`、`rt_min_daily`） | 当前交易日分钟数据；`rt_min` 描述为分钟周期闭合数据，`rt_min_daily` 可取当日开市以来分钟数据。返回证券、分钟时间、OHLC、成交量和成交额等字段。 | 需单独开通“A股分钟RT”权限；`rt_min` 每次最多 1,000 行。账户是否可用未验证。 | 可用于未来前瞻采集演练；需自己记录每次响应的本机抓取时间。文档中的 bar 时间不是本机接收时间，也未证明市场可成交。当前接口不提供策略所需的独立上市、停牌、ST、可买状态时间证据。 |
| BaoStock（策环当前适配） | 当前项目使用其日线和证券列表能力；本轮没有发现策环已接入分钟/09:31 快照适配器。 | 现有本机权限只说明已经取得既有数据；分钟服务能力、权限与覆盖未验证。 | 不能用现有日线适配器写入的 15:00 `available_at` 推导 09:31 数据。此处只表示项目尚未验证，不断言 BaoStock 一定没有相关能力。 |
| 单独的证券状态来源 | H2 需要逐证券、逐阶段的上市、停牌、ST 与可买状态。公开分钟行情接口的 OHLC 字段本身不等于状态时间证据。 | 需逐项核实供应商字段、历史覆盖和使用权限。 | 当前 68 日快照没有独立的状态发布时间/抓取记录，也没有足够的有效日期交易规则；因此 H2 真实历史执行仍为 `NEEDS_DATA`。 |

## 当前决策

1. 不通过重写日线 `available_at` 把日线开盘价变成已验证的 09:31 行情。
2. H2 的历史表现比较先保持阻断；当前数据只支持输入覆盖和程序行为诊断，不支持价格可得或可成交结论。
3. 优先用本机可访问账户做少量未来日期试取：同一证券、同一交易日分别保存行情、状态响应和本机抓取时刻；确认失败后记录权限/限流原因，不静默补值。
4. 若供应商不提供发布时间，前瞻记录仍可以累积“本机在某时收到某版本响应”的证据，但历史回填状态继续为 `UNVERIFIED`。
5. `can_buy` 需要结合有效日期交易单位、停牌、涨跌停和现金规则判断。行情端点返回的 OHLC 不会自动证明订单可执行。

## 一次本机只读 AKShare 小样

2026-11-05 对 `SH600000`、`2026-11-05` 调用 AKShare `stock_zh_a_hist_min_em` 一次（`akshare==1.19.3`，SDK 锁定版本）。请求收到 121 条该日分钟记录，事件时间覆盖 09:30–11:30（Asia/Shanghai）；本机请求开始和响应接收时间记录在私有归档中。请求发生在这些事件时间之后。AKShare 返回对象被保存为 SDK 序列化 JSON，并由版本化解析器重新构建成 121 条规范记录，摘要一致。

这只验证了这次 SDK 调用和原始 SDK 返回对象到规范行的重建链路：归档不是原始 HTTP 响应；供应商发布时间未提供；该探测没有请求证券状态，因此研究可买资格仍是 `UNKNOWN`。它不能证明 09:31 历史可得性、真实可成交性、历史点时版本或接口稳定性。原始文件及明细位于本机 `data/private/provider-probes/akshare-minute-SH600000-20261105-v1/`，不提交 Git。

当前 uv 环境未安装 Tushare，且本次探测没有配置或读取 Tushare Token；因此没有验证 Tushare 账户权限。

## 纸面观察记录

记录格式为 `PAPER_OBSERVATION` schema v2。v2 绑定排名输入、逐条原始响应 SHA-256、解析器名称/版本、由归档响应重建的规范 payload 和 payload 摘要，并分别保留请求开始、响应接收、计划观察、市场事件、供应商发布时间等时点。验证器会实际调用同一版本解析器重建 payload；只有文件哈希正确不再足以证明规范值来自该文件。

schema v1 仍可读取，但验证结果标为 `VALID_LEGACY_UNVERIFIED_PAYLOAD_BINDING`，不声称原始响应与规范 payload 已绑定。`can_buy` 不接受为供应商原始状态字段；只有报价、上市/停牌/ST 状态、交易规则和账户现金等输入足够时，项目才推导研究资格。规则完整性、价格限制和账户现金若只传布尔值，会显示 `SUPPLIED_ASSUMPTION`；结构化来源字段齐全时也只显示 `STRUCTURALLY_BOUND_SOURCE_UNVERIFIED`，不宣称来源内容已经独立核实。任何缺失状态都保持 `UNKNOWN`，不能等同于非停牌。

本机合成演练命令：

```bash
uv run python scripts/rehearse_paper_observation.py \
  --output-dir data/private/experiments/paper-observation-rehearsal-<unique-id>
```

此命令不连接行情供应商、华泰或其他账户，不生成订单。将来做真实采集时，原始响应与账户材料都留在 `data/private/`；缺少供应商发布时间时仍标为 `UNVERIFIED`。

## 只生成候选排名的前瞻记录

`scripts/run_paper_ranking.py` 可以生成研究排名文件和清单，不计算回报、不生成目标委托，也不连接券商。默认 `SYNTHETIC` 仅用于流程验证；`FORWARD_PAPER` 要求信号日是本机当天、收盘后等待到 15:05（Asia/Shanghai）、行情文件有同日收盘后本机抓取清单且 SHA-256 匹配、独立交易日历和逐日股票池输入齐备。程序先对 signal_date 股票池逐证券审计整个 `lookback + 1` 日窗口；缺日、历史不足、未知状态或价格口径不一致的证券会从排名范围排除，并写入 `window-audit.csv`。停牌窗口单独计数；不能仅凭缺行情推断停牌。

```bash
# 合成流程示例；输出只能写到私有目录
uv run python scripts/run_paper_ranking.py \
  --data path/to/bars.csv \
  --signal-date YYYY-MM-DD \
  --classification SYNTHETIC \
  --output-dir data/private/experiments/paper-ranking-<unique-id>

# 前瞻模式还需提供本机抓取清单、交易日历和按日股票池
uv run python scripts/run_paper_ranking.py \
  --data path/to/current-bars.csv \
  --signal-date YYYY-MM-DD \
  --classification FORWARD_PAPER \
  --source-capture-manifest path/to/capture-manifest.json \
  --calendar path/to/trading-calendar.csv \
  --universe path/to/securities_by_date.csv \
  --output-dir data/private/experiments/paper-ranking-<unique-id>
```

本地抓取时间不是可信外部时间戳，也不证明数据供应商此前发布了相同版本。缺少前置条件时生成 `NEEDS_DATA` 清单而不生成排名。即使排名成功，正式基线仍为 `NOT_READY`，H2 的收益和可成交结论仍受数据、规则、费用、状态和历史发布时间阻断。

## 核对来源

- [AKShare 官方股票行情文档：`stock_zh_a_hist_min_em`](https://akshare.akfamily.xyz/data/stock/stock_quote.html#id66)
- [Tushare Pro 分钟数据权限与更新说明](https://tushare.pro/document/1?doc_id=234)
- [Tushare Pro A 股实时分钟接口](https://tushare.pro/document/2?doc_id=374)
- [Tushare Pro A 股实时分钟日累计接口](https://tushare.pro/document/2?doc_id=457)
