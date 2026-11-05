# 计划输出与准入边界

`cehuan plan` 默认使用 `RESEARCH_ONLY`。它只给出候选动作和依据，目标股数留空、计划数量为 0，不能导入成交复盘，也不能当作委托建议。

`MANUAL_REVIEW` 是另一种输出类别。它要求正式基线 readiness 报告本身通过散列和完整性检查，并确认执行引擎已经消费审核规则；同时要求当日点时股票池、交易日历、最新日线的可用和抓取时间、行情来源，以及带时区的当日持仓快照。最新日线必须对应截至当前最近的开市日，计划只能在上海时间 15:05 后生成。股票池成员缺行情、市场规则无效、持仓缺可卖数量或缺最新价格时，命令只写准入失败报告，不写计划数量。

准入报告描述输入是否满足生成供人工审核文件的条件，不代表券商接受委托或订单可成交。当前正式 baseline 为 `NOT_READY`，因此人工审核模式会被阻断；系统继续只输出研究候选。任何交易仍由用户在华泰软件审核并手动操作。

## 研究候选示例

```bash
uv run cehuan plan \
  --data path/to/canonical-bars.csv \
  --cash 100000 \
  --purpose RESEARCH_ONLY
```

该模式不读取或推断真实账户资金；`--cash` 只是候选权重计算的研究输入，且不产生可执行数量。

## 人工审核模式所需输入

在正式 readiness 达到 `READY` 后，人工审核命令还需提供这些文件和快照时点：

```text
--purpose MANUAL_REVIEW
--formal-readiness path/to/baseline_readiness.json
--data path/to/latest-bars.csv
--calendar path/to/trading-calendar.csv
--universe path/to/securities_by_date.csv
--positions path/to/positions.csv
--positions-as-of 2026-11-05T15:10:00+08:00
```

输出放在唯一的 `outputs/plans/<plan_id>/` 目录中，包含计划、准入报告和绑定的输入 SHA-256；不覆盖“latest plan”，也不连接华泰。
