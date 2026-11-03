# 研究到复盘闭环示例

此示例用虚构的 `SZ000001` / `SZ000002` 价格运行技术基线，生成待审核计划，再用合成成交/状态/持仓输入验证复盘流程。它不包含真实行情、不读取账户、不连接华泰、不发送委托，也不验证策略效果。

```bash
uv run python examples/run_synthetic_cycle.py --output outputs/synthetic-cycle --scenario partial_fill
```

每次运行都会在输出根目录下创建唯一子目录并拒绝覆盖。可用场景：

- `partial_fill`：部分成交，未成交原因未知。
- `no_fill`：没有匹配成交行，订单状态为 `UNKNOWN`，不会推断撤单。
- `duplicate_import`：同一成交文件散列只登记一次，第二次导入会被拒绝。
- `partial_fill_cancelled`：部分成交后有单独的撤单状态证据和来源原件 SHA-256。
- `modified_plan`：保存原计划、修改后计划及字段差异。
- `missing_positions`：不生成持仓快照，明确报告仓位未知。

例如：

```bash
uv run python examples/run_synthetic_cycle.py --output outputs/synthetic-cycle --scenario no_fill
uv run python examples/run_synthetic_cycle.py --output outputs/synthetic-cycle --scenario partial_fill_cancelled
uv run python examples/run_synthetic_cycle.py --output outputs/synthetic-cycle --scenario modified_plan
uv run python examples/run_synthetic_cycle.py --output outputs/synthetic-cycle --scenario missing_positions
```

产物因场景而异，常见文件包括：

- `plan.csv`、`plan.md`：待审核计划，醒目标注 `SYNTHETIC ONLY`。
- `synthetic_bars.csv`、`baseline_metrics.json`、`baseline_equity_curve.csv`、`baseline_trades.csv`：合成行情、基线和回测产物。
- `fictitious_fills.csv`：虚构成交输入；无成交场景只有带表头的空 CSV。
- `fictitious_order_statuses.csv`、`synthetic_order_status_evidence.txt`：撤单场景的状态与来源证据。
- `plan_original.csv`、`plan_changes.csv`：计划修改场景的原始版本与逐字段差异。
- `fill_import_registry.json`、`duplicate_import_attempt.json`：重复导入场景的幂等登记证据。
- `fictitious_positions.csv`、`position_reconciliation.csv`：仅在提供快照的场景出现。
- `reconciliation.csv`、`review.md`、`next_hypothesis.json`：对账复盘和下一轮研究假设。
- `manifest.json`：唯一运行编号、场景、产物索引、合成类型和行情 SHA-256。

一次本机运行曾生成 6.87% 的合成基线收益、5 笔模拟交易，以及计划 1,300 股、虚构成交 600 股的部分成交案例。这些数字只检查程序串接与算术，不是 A 股回测或账户收益，也不支持调整策略参数。

## 人工 CSV 复盘

`review` 可接收明确的委托状态证据、用户修改计划的原版文件和可选成交导入登记簿。程序先在临时目录写完报告与 manifest，再原子发布完整输出目录，最后登记成交文件；登记簿同时绑定 `cycle_run_id`、成交散列、输出 manifest 路径及其散列。报告写入失败时不会创建登记，登记失败时会移除本次报告目录，因此可以用同一输出路径重试。若进程恰在目录发布和登记提交之间退出，重试会验证原目录及输入散列后补登记；若登记已提交，则核对原登记并按成功恢复。相同成交文件并发处理时，登记簿锁只允许一个运行成功，另一份临时报告会清理。每次运行的 manifest 记录计划、成交、映射、快照与状态证据文件散列。账户原始 CSV 不会复制进产物目录。

```bash
uv run cehuan review \
  --plan data/private/review/plan.csv \
  --original-plan data/private/review/plan-issued.csv \
  --fills data/private/review/fills.csv \
  --order-statuses data/private/review/order-statuses.csv \
  --positions data/private/review/positions.csv \
  --import-registry data/private/review/fill-import-registry.json \
  --output-dir data/private/review/reports/plan-cycle-2026-11-03
```

首次运行应为新的计划周期选一个不存在的输出目录；需要恢复时沿用该目录路径。

`--order-statuses` 的字段见 `examples/order_statuses_template.csv`，需要订单 ID、计划 ID、证券、方向、状态、带时区的报告时间、本地原件路径及原件 SHA-256。适配器会重新计算本地原件散列；这只验证字节一致，不验证原件内容的解释。没有委托状态证据时，`NO_FILL` 只说明成交 CSV 中没有匹配成交；它不等于撤单、拒绝或过期。没有持仓快照时，持仓和可卖数量未知，不视作零。

`--original-plan` 与 `--plan` 的行键及动作、数量、目标股数、参考价格差异会另存。系统无法独立认证修改人身份。导入登记簿只拒绝字节完全相同的成交导出；不同版本文件可能有重叠记录，仍需人工避免重复计数。

## 公司行动事件 CSV 研究输入

`examples/accounting_events_template.csv` 提供 `CASH_DIVIDEND` 和 `CODE_CHANGE_1_TO_1` 的列结构。CLI 只允许这样接入研究账务引擎：

```bash
uv run cehuan backtest \
  --data data/private/diagnostic/bars.csv \
  --cash 100000 \
  --diagnostic \
  --accounting-mode ledger_v2 \
  --accounting-events data/private/diagnostic/accounting-events.csv
```

每个事件行必须绑定本地归档原件、HTTPS 来源、SHA-256 和人工复核字段。适配器校验事件键、事件字段、点时日期、来源字段及归档原件字节散列，并把事件 CSV 散列和逐事件来源审计写入实验产物。它不会打开公告 URL、判断公告解释是否正确，也不证明事件覆盖完整。不得把合成示例或部分事件输入用于正式基线；`baseline_ready` 仍为 `NOT_READY`。

`ledger_v2` 当前调度现金分红与 1:1 代码变更。分红税费未知或事件日期不在输入交易日时会阻断；1:1 代码变更在模拟开盘只读取开盘价及当时可用的上市/停牌状态，完整 OHLC 仍由独立数据质量检查负责。送转、配股、非 1:1 合并、退市和完整逐证券事件覆盖审计尚未接入执行调度。详见[公司行动账务契约](corporate-action-accounting-contract.md)和[正式基线就绪审计](baseline-readiness.md)。
