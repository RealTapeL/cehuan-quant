# 每日小范围纸面研究操作手册

本流程只在一个固定的 10 只沪深证券研究池上生成纸面排名和 H1 对照，不代表全市场选股。它不计算正式收益、不改当前有效策略、不生成股数或订单，也不连接华泰。正式基线保持 `NOT_READY`。

## 固定研究池

复用本机已有的 `data/private/experiments/forward-small-20261106-akshare-v1/selected_pool.csv`。这是 2026-11-06 按“从当次名单中，沪市和深市分别按六位代码升序各取前 5 只”选出的固定研究池；之后的运行不得每天重新选择或根据价格、动量和排名替换代码。若该文件不存在，应从已封存的 2026-11-06 私有研究池备份恢复并核对其哈希，不要静默重建。

研究日前仍须重新核对这 10 只是否属于当日 `STOCK_MEMBER`；该固定代码清单本身不证明当日上市、停牌或 ST 状态。缺状态时保持 `UNKNOWN` 并阻断排名。

## 何时运行

使用 Asia/Shanghai 本机时间。先从 BaoStock 交易日历确认当天 `is_open=1`。仅在研究日当日 15:05 之后尝试形成前瞻排名；在此之前停止，不把前一交易日或历史回填数据标为当天候选。日历尚未覆盖研究日时，先补日历；研究日不是开市日时不运行。

行情窗口应从真实交易日历中取研究日及此前至少 20 个开市日，共至少 21 个开市日。不要用固定的 21 个自然日替代开市日窗口。

## 数据捕获要求与现有入口

捕获文件写入新的 `data/private/experiments/forward-small-<日期>-<唯一ID>/` 目录，不覆盖历史运行。行情输入需为统一来源的原始收盘价和策略后复权收盘价；另保存 BaoStock 对 21 日窗口的 `tradestatus`、`isST` 记录、当日点时成员、交易日历和逐项请求时间。记录本机请求时间、行对应的交易日期及供应商发布时间证据；没有供应商发布时间证据时写 `UNVERIFIED`。不能用本机抓取时间替代供应商发布时间。

仓库已有 `cehuan fetch-pool`、`cehuan snapshot`、`cehuan universe-snapshot` 和 `scripts/run_paper_ranking.py` 等构件，但**目前没有一个受测的单命令采集器，将固定 10 只池的行情、逐证券状态、点时成员、交易日历及 paper-review 所需来源文件组装成完整抓取包**。不要仅凭 `fetch-pool` 输出手工伪造逐请求时间，也不要直接把全市场 `securities_by_date.csv` 传给 10 只股票的排名器。需要使用有真实逐项请求日志的捕获流程生成 `bars.csv`、`trading_calendar.csv`、仅含这 10 只证券当日成员记录的 `point_in_time_universe.csv`、状态 CSV、每证券后复权来源 CSV 和 `capture-manifest.json`。如果其中任一项缺失，本轮停在 `NEEDS_DATA`，不得手工填充为已验证。

抓取包的 `capture-manifest.json` 至少需满足下列现有校验契约：

- `data_file_sha256` 与本次 `bars.csv` 完全一致；
- `status_file_sha256` 唯一对应状态 CSV；`status_requests` 覆盖固定池每只证券的研究日 `tradestatus`、`isST` 与请求/返回时刻；
- `price_files_sha256` 和 `price_requests` 逐证券绑定完整窗口后复权价格文件、来源、`hfq` 口径及请求/返回时刻；当前模型复盘适配器要求该后复权文件来自 `akshare.stock_zh_a_hist_tx`，含 `date`、`close` 列，并与排名用的 `adjusted_close` 逐日一致；
- `capture_started_at_utc`、`capture_finished_at_utc` 和 `captured_at_utc` 为本机真实抓取起止/完成时刻；`provider_published_at_utc` 无证据时为空，`provider_publication_evidence` 和 `trusted_external_timestamp` 保持 `UNVERIFIED`；
- `scope` 明确为固定 10 只证券的小范围研究池。

这项采集组装目前仍是下一次日常运行的前置缺口；本手册不会把多个底层命令伪装成已经通过测试的一键采集器。发生 BaoStock 登录失败、AKShare 缺行、复权价格不完整、状态未知或清单哈希不匹配时，保留各自错误并停止。

## 排名与 H1 对照

只有抓取包完整且运行时确实为当日 15:05 之后，才运行排名器。`--signal-date` 必须使用本机当天日期；每次使用新的唯一输出目录。

```bash
SIGNAL_DATE="$(TZ=Asia/Shanghai date +%F)"
RUN_ID="$(TZ=Asia/Shanghai date +%Y%m%dT%H%M%S)"
CAPTURE_DIR="data/private/experiments/forward-small-${RUN_ID}-v1"
RANKING_DIR="data/private/experiments/forward-ranking-${RUN_ID}-v1"

uv run python scripts/run_paper_ranking.py \
  --data "$CAPTURE_DIR/bars.csv" \
  --signal-date "$SIGNAL_DATE" \
  --classification FORWARD_PAPER \
  --source-capture-manifest "$CAPTURE_DIR/capture-manifest.json" \
  --calendar "$CAPTURE_DIR/trading_calendar.csv" \
  --universe "$CAPTURE_DIR/point_in_time_universe.csv" \
  --output-dir "$RANKING_DIR"
```

检查输出 `paper-ranking-manifest.json`。只有状态为 `CAPTURED` 且固定 10 只研究证券都通过窗口门槛时，才形成有效排名；`NEEDS_DATA` 时读取 `blocking_reasons` 和 `per_security_window_status`，不要调用模型。排名器会同时保存原排名、H1 排名、差异表和逐证券窗口审计。

## 显式模型复盘

仅在排名成功并核对产物哈希后，才由操作者显式选择真实 LLM 模式。命令每次只请求一次，不自动重试、不回退 stub；模型校验失败时保留程序事实并记录安全错误类别。密钥通过当前 shell 环境变量提供，不放进命令行、文件或产物。

```bash
uv run cehuan paper-ranking-review \
  --ranking-dir "$RANKING_DIR" \
  --capture-manifest "$CAPTURE_DIR/capture-manifest.json" \
  --model-mode llm \
  --output outputs/paper-ranking-reviews
```

运行记录区分真实模型请求、返回用量和未知费用。复盘输入不包含账户、持仓或成交数据。即使模型复盘通过，也只表示它通过了对本次程序事实与研究边界的结构校验，不代表排名质量、策略收益或可成交性已经验证。

## 当前就绪状态

2026-11-10 08:51（Asia/Shanghai）检查时仍在开盘前，不能形成 2026-11-10 的收盘前瞻排名。本机已有的最近一次真实候选数据仍是 2026-11-06；因此本手册新增后尚未用新的真实交易日数据端到端验证。下次操作应在一个真实开市日 15:05 之后，先完成上述固定池数据捕获包，再运行排名器；若采集组装前置缺口尚未解决，结果应为 `NEEDS_DATA`，不要调用模型。
