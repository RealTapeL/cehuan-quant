# 每日人工研究简报

`cehuan daily-research-brief` 是一个只读的简报装配入口。它不抓取行情、不运行排名或回测，而是复核已有交易日历与当天已封存的 `FORWARD_PAPER` 排名，随后用现有结构化研究 Agent 提出一条待人工审核的研究假设。

## 入口和边界

当前项目已有 AKShare 单股/股票池日线获取、BaoStock 快照与点时股票池采集、`forward_ranking.create_paper_ranking_artifact()`、`paper-ranking-review`、真实行情摘要研究和合成 `research-cycle`。这些功能的输入/范围不同：固定日期的 `lookback-pair-capture` 只服务 2026-12-17 登记实验；静态真实行情摘要仍覆盖 2024-10-08 至 2025-01-10；`research-cycle` 的回测链是合成流程。

本入口只接受**最近已完成交易日当天采集、当天生成并通过来源哈希重建**的封存前瞻排名。项目目前没有一条通用命令能自动完成“最新交易日行情 + 点时成员 + 每日上市/ST/停牌状态 + 21 日窗口 + 前瞻排名”全链，因此本入口不会拼装或补造这些输入；缺项时输出 `NEEDS_DATA`，不请求模型。

当天排名必须在信号日当天生成，且行情来源清单的本机抓取起止日期也必须是信号日当天。排名日期或抓取日期不匹配时按历史回填处理并阻断当日简报。供应商发布时间与本机抓取时间分别记录，前者没有证据时保持 `UNVERIFIED`。

## 运行

准备一份覆盖当前日期的规范交易日历 CSV，最好与同目录的 `capture-manifest.json` 一起提供。命令以本机 `Asia/Shanghai` 时间和日历确定最近已完成的开市日：当日 15:05 前不把当天算作已完成日；15:05 后才允许检查当天。其余时间取当前日期之前最近的开市日。

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

## 合成验收与当前数据边界

```bash
uv run pytest -q tests/test_daily_research_brief.py tests/test_paper_ranking_review.py
```

测试覆盖 Stub 结构假设、事实/报告装配、哈希和 freshness gate。合成测试结果不代表真实市场分析。若最近交易日无当天成功排名，报告应列出所需行情、点时成员、逐日上市/ST/停牌状态、完整 21 个开市日窗口和来源抓取清单；旧排名只显示为旧记录，不得冒充当前数据。
