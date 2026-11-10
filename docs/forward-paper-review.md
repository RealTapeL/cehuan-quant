# 每日纸面排名与模型复盘

下一次小范围前瞻运行的日期选择、数据要求和步骤见[每日小范围纸面研究操作手册](daily-small-scope-paper-run.md)。

`cehuan paper-ranking-review` 读取已经封存的 `FORWARD_PAPER` 排名目录和对应抓取清单。它校验排名、H1 排名、差异表、窗口审计、行情、配置、日历、研究池和抓取清单的 SHA-256；**不抓行情，也不重新运行排名**。它从程序输入重建事实，再以独立 JSON 契约要求 LLM 解释事实和限制。

每次运行写入新的 `outputs/paper-ranking-reviews/paper-review-*` 目录，保存程序事实、最小模型输入、提示词、模型审计和中文报告。模型请求仅发送必要排名/窗口摘要，不发送 API Key、账户数据、持仓或成交。模型必须通过证券、日期、程序计数、证据 ID、限制承认和非交易建议校验；校验不通过时保留程序事实，并记录安全错误类别，不保存被拒绝的原始响应、不自动重试、不回退 stub。

示例：

```bash
uv run cehuan paper-ranking-review \
  --ranking-dir data/private/experiments/forward-ranking-YYYYMMDD-<run-id> \
  --capture-manifest data/private/experiments/forward-small-YYYYMMDD-<run-id>/capture-manifest.json \
  --model-mode llm \
  --output outputs/paper-ranking-reviews
```

需要在当前 shell 中配置模型名、兼容服务地址和 API Key 环境变量。若缺少任一项，命令仍可生成包含程序事实的报告，但状态为 `BLOCKED_CONFIGURATION_MISSING_NO_REQUEST`、调用次数为 0，退出码为 2；它不会假装模型已复盘。

这类排名是小范围纸面观察。排名和动量不是策略收益或可成交性证明；数据供应商发布时间、历史规则、费用与公司行动覆盖等限制按各次清单保留。正式基线继续为 `NOT_READY`，本命令不生成订单。
