# 每日人工研究简报

`cehuan daily-paper-run` 是每日人工研究的编排入口：当日收盘后采集固定 10 只流程试运行池的行情、日历、成员与上市/ST/停牌状态，通过门控后调用现有排名器，再把此次排名交给 `daily-research-brief`。

`cehuan daily-research-brief` 仍是只读简报装配入口。它不抓取行情、不运行排名或回测，只复核已有交易日历与当天已封存的 `FORWARD_PAPER` 排名，再由现有结构化研究 Agent 提出一条待人工审核的研究假设。

## 入口和边界

当前项目已有 AKShare 单股/股票池日线获取、BaoStock 快照与点时股票池采集、`forward_ranking.create_paper_ranking_artifact()`、`paper-ranking-review`、真实行情摘要研究和合成 `research-cycle`。这些功能的输入/范围不同：固定日期的 `lookback-pair-capture` 只服务 2026-12-17 登记实验；静态真实行情摘要仍覆盖 2024-10-08 至 2025-01-10；`research-cycle` 的回测链是合成流程。

每日流程固定使用 `data/private/experiments/forward-small-20261106-akshare-v1/selected_pool.csv` 中原有的 10 只代码，必须与 `FIXED_SYMBOLS` 及顺序完全相同。该池只用于流程试运行，不代表全市场；不允许在该命令中替换或增补证券。每日采集器与 2026-12-17 的冻结 `lookback-pair-capture` 是不同入口，不读取或修改该实验登记、股票池或参数。

每日采集只允许在本机当日 15:05 Asia/Shanghai 后启动，并且只将本机当天作为信号日。先请求 BaoStock 日历；只有日历确认当天开市且窗口包含当日及此前 20 个开市日时，才串行请求固定池的逐日成员、单证券基础资料、未复权/后复权行情。每个端点仅尝试一次，SDK 自动重试关闭；每个 SDK DataFrame 响应和请求日志分别留档。成员响应缺少任一固定代码时保留 `UNKNOWN` 并阻断，不能解释为非成员。缺行、重复、来源冲突、未知上市/ST/停牌状态、价格窗口不完整或哈希不符都会使本次运行成为 `NEEDS_DATA`，不调用模型、不生成排名。

只有采集清单状态、文件哈希、固定池、配置、21 个开市日、逐日成员和状态均通过后才运行现有排名器。每日编排器不修改有效策略。排名成功后，将本次运行目录作为唯一排名根交给简报入口，避免误选其他历史运行。若当前日期不是交易日，或在收盘等待时点之前运行，程序不会把上一交易日数据回填成“当日”。

当天排名必须在信号日当天生成，且行情来源清单的本机抓取起止日期也必须是信号日当天。排名日期或抓取日期不匹配时按历史回填处理并阻断当日简报。供应商发布时间与本机抓取时间分别记录，前者没有证据时保持 `UNVERIFIED`。

## 运行

日常采集和排名使用 `daily-paper-run`，它会在本机当天 15:05 Asia/Shanghai 后自行采集日历。若只复核已存在的排名并运行简报，`daily-research-brief` 需要一份覆盖当前日期的规范交易日历 CSV，最好与同目录的 `capture-manifest.json` 一起提供。只读简报入口以本机时间和该日历确定最近已完成的开市日：当日 15:05 前不把当天算作已完成日；15:05 后才允许检查当天。其余时间取当前日期之前最近的开市日。

```bash
uv run cehuan daily-paper-run \
  --model-mode llm
```

`llm` 是显式选择，只在全部采集与排名门控通过后才可能发出一次真实请求。若只验证研究流程而不请求外部模型，使用 `--model-mode stub`；Stub 会明确标为非 LLM。每次运行写入唯一的 `data/private/experiments/daily-paper-runs/<run-id>/`，不覆盖失败运行。

若已有封存排名，只想生成/复核简报，可单独使用只读入口：

```bash
uv run cehuan daily-research-brief \
  --calendar data/private/experiments/<当前日历快照>/trading-calendar.csv \
  --ranking-root data/private/experiments \
  --model-mode llm
```

只有发现匹配信号日且可由实际输入重建的封存排名，才会调用模型。模型模式必须显式指定；`llm` 最多发起一次请求，不自动重试；`stub` 仅用于本地结构流程测试，报告会明确标为非 LLM。模型只收到程序汇总事实、有限排名和摘要证据目录，不接收原始行情行、账户、持仓、成交、券商数据或 API Key。

输出写入 Git 忽略的 `outputs/daily-research-briefs/<唯一运行目录>/`，包括：

- `daily-brief.md`：中文简报，分开列出程序事实、模型建议、未核实项和人工风险检查；
- `daily-research-summary.json`、`agent-input.json`：脱敏摘要与模型输入；
- `program-facts.json`、`research-hypothesis.json`、`proposal-review.json`：可审计事实、结构化假设及程序状态；
- `model-audit.json`、`brief-manifest.json`：模型模式、调用/usage、输入与产物散列、执行边界。

结果只用于研究候选。程序不计算收益、NAV 或回撤，不应用模型参数，不改有效策略，不产生股数、订单或券商连接。正式基线继续是 `NOT_READY`。

`daily-paper-run` 当前一轮约有 52 次串行 BaoStock 请求：日历 1 次、固定池单证券资料 10 次、逐日成员名单 21 次、原始和后复权日线各 10 次。SDK 重试关闭；每次请求保留返回的 DataFrame CSV、字段/行数、请求时间和安全化请求日志。它保存 21 日逐证券状态、规范行情、输入与输出 SHA-256、采集清单及封条、排名文件和每日简报路径。SDK 响应 CSV 是适配器返回对象的留档，不是网络原始报文。供应商发布时间保持 `UNVERIFIED`；适配器 `available_at=15:00` 是项目口径，不是供应商发布时间证据。空的证券身份事件文件也不证明没有公司行动或身份变更。

## 合成验收与当前数据边界

```bash
uv run pytest -q tests/test_daily_paper_run.py tests/test_daily_research_brief.py tests/test_paper_ranking_review.py
```

测试覆盖 Stub 结构假设、事实/报告装配、哈希和 freshness gate。合成测试结果不代表真实市场分析。若最近交易日无当天成功排名，报告应列出所需行情、点时成员、逐日上市/ST/停牌状态、完整 21 个开市日窗口和来源抓取清单；旧排名只显示为旧记录，不得冒充当前数据。
