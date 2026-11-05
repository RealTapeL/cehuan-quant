# 计划输出与准入边界

`cehuan plan` 默认使用 `RESEARCH_ONLY`。它只给出候选动作和依据，目标股数留空、计划数量为 0，不能导入成交复盘，也不能当作委托建议。

`MANUAL_REVIEW` 是另一种输出类别。报告自身的 SHA-256 只检查文件内容自洽，不能证明 readiness 来自实际审计。人工审核路径必须同时提供实际 `--readiness-inputs` 清单；程序通过正式审计所用的数据适配器重新计算所有历史输入哈希和 readiness，再与保存报告逐字段比较，并验证运行配置、代码提交和评价窗口。报告自己的 `input_hashes` 与 `evaluation_window` 不作为期望值来源。缺输入、文件变化或无法重建时 fail closed。当前执行引擎的正式能力仍未完成，readiness 重建应返回 `NOT_READY`，因此目前人工审核计划都会被阻断。

历史基线输入与当日计划输入分开绑定：前者包含 readiness 审计文件、配置、历史行情、规则、费率和公司行动；后者包含计划实际使用的行情、当日股票池、日历、持仓及其映射。两组散列分别保存在准入报告中。新买候选只从信号日 `STOCK_MEMBER` 生成；`UNKNOWN_TYPE` 和其他未知成员状态不会成为新买候选。股票池外的现有持仓仍保留在持仓循环中，按卖出状态、可卖数量和现价继续生成 HOLD/SELL 审核结果。

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
--readiness-inputs path/to/plan-readiness-inputs.json
--data path/to/latest-bars.csv
--calendar path/to/trading-calendar.csv
--universe path/to/securities_by_date.csv
--positions path/to/positions.csv
--positions-as-of 2026-11-05T15:10:00+08:00
```

输出放在唯一的 `outputs/plans/<plan_id>/` 目录中，包含计划、准入报告和绑定的输入 SHA-256；不覆盖“latest plan”，也不连接华泰。

`--readiness-inputs` JSON 使用 `cehuan.plan_readiness_inputs.v1`，包含独立评价窗口和 `READINESS_CONTEXT_KEYS` 的全部输入路径。相对路径按清单所在目录解析。路径清单不是签名或通过证明；每次运行都会重新读取文件并重算审计。即使历史 readiness 以后通过，计划仍需单独满足当日输入检查。正式基线通过前，不要尝试将研究输出作为人工交易建议。
