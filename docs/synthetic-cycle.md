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

`examples/accounting_events_template.csv` 提供 `CASH_DIVIDEND`、`CODE_CHANGE_1_TO_1` 和 `SHARE_DISTRIBUTION` 的列结构，并含可选分红税务字段 `tax_treatment` 与 `payment_withholding_per_share_cny`。旧 CSV 没有这两列时仍按旧口径推断：给了 `tax_cny` 就是显式研究最终税额，没有则要求账户证据。`DEFERRED_BY_HOLDING_PERIOD` 必须留空最终 `tax_cny` 并提供发放日每股预扣金额。CLI 只允许这样接入研究账务引擎：

```bash
uv run cehuan backtest \
  --data data/private/diagnostic/bars.csv \
  --cash 100000 \
  --diagnostic \
  --accounting-mode ledger_v2 \
  --accounting-events data/private/diagnostic/accounting-events.csv
```

每个事件行必须绑定本地归档原件、HTTPS 来源、SHA-256 和人工复核字段。适配器校验事件键、事件字段、点时日期、来源字段及归档原件字节散列，并把事件 CSV 散列和逐事件来源审计写入实验产物。它不会打开公告 URL、判断公告解释是否正确，也不证明事件覆盖完整。不得把合成示例或部分事件输入用于正式基线；`baseline_ready` 仍为 `NOT_READY`。

分红账本区分登记日权益、除息日应收、发放日现金和未来卖出税务。发放日没有预扣不能推导出未来税额为零。若采用递延税务状态，NAV 标为 `VALUED_WITH_UNRESOLVED_DIVIDEND_TAX`，税务口径为 `GROSS_CASH_EXCLUDING_UNRESOLVED_FUTURE_DIVIDEND_TAX`，并记录未决批次数；这是未扣除潜在后续税款的研究估值，不是净税后收益。账户到账实际预扣可由账本 API 通过 `actual_withheld_tax_cny` 加账户证据 ID/哈希登记；后续卖出税款只由账户证据过账，程序不计算税率。账本状态快照包含逐批次未决税务；真实账户导入适配器尚未实现。

`ledger_v2` 研究诊断现可调度现金分红、1:1 代码变更和受限送转事件。送转使用登记日收盘快照锁定持仓批次和成本分摊；生效日形成权利，股份登记日转成独立批次，可流通日才可卖。登记日至生效日前发生的卖出不会丢失送转权利；非整数获配若没有账户层股数及依据会在登记日阻断。事件后续阶段落在回测窗口外时，期末账本保留未完成权利及下一阶段日期；账本状态续跑需提供相符的 opening snapshot，完整规则策略续跑则使用单独的 `backtest_checkpoint.json` 并同时提供完整交易日历。没有窗口前权益快照的权利会阻断，不按零权利处理。

完整规则策略续跑的合成输入中，“连续一次跑”和“分段保存/恢复”逐日 NAV、成交、公司行动流水、账本快照和窗口交界待执行目标一致。checkpoint 还保留全局调仓位置、动量预热行、信号模式、代码变更信号保护状态、上次价格与前收、总权益和输入散列。该原型只支持 `ledger_v2` 规则策略，不代表 Qlib 训练状态续训，也不表示真实市场效果。

运行同日现金分红 + 送转合成诊断（不读取真实行情或华泰账户）：

```bash
uv run python scripts/run_synthetic_action_diagnostic.py --output outputs/synthetic-action-diagnostic
```

脚本生成唯一输出目录，含行情、成交、账本流水、逐批次税务状态、NAV 曲线、快照、指标和 manifest。事件日期和比例是虚构字段，交易日使用生成的工作日 fixture；NAV 指标只能检查链路和账务算术，不能解释成策略表现或真实公告字段通过。

登记至新增股份登记前，NAV 使用同一证券当日未复权收盘价作为 `UNDERLYING_CLOSE_PROXY`；流水和实验记录持仓权利数量、估值日期、底层价格来源与输入 SHA-256，并明确该价格不是权利成交价。缺收盘价、股数未确认、停牌或停牌状态未知会阻断 NAV。程序按进行中的权利维护待处理集合，不逐日扫描已结束事件。CSV 必须绑定本地原件哈希和人工复核字段，但程序不验证公告解释是否正确。2026-11-03 的真实公告小样已完成原件哈希与文本字段自动核对；逐字段审核表 `docs/evidence/corporate-action-sample-field-review.csv` 的 23 项仍为 `PENDING`，新增的审核建议只是机器提示。明星电力分红/送转与中航成飞代码变更目前各有一份**合成**短区间流程脚本，不读取待审公告记录；真实公告短区间诊断要等人工签认及可得时点证据。华泰成交字段调查模板见[华泰字段调查说明](huatai-fill-field-investigation.md)；尚无脱敏样本，去重仍是审查原型。配股事件还未进入每日调度，契约见[配股调度设计](rights-offering-scheduler-design.md)。增量预检器见[checkpoint 增量输入规则](checkpoint-incremental-input-policy.md)：它只分类候选输入，当前引擎仍严格要求完整输入哈希一致。非 1:1 合并、退市和完整逐证券事件覆盖也未通过正式账务门槛。详见[公司行动账务契约](corporate-action-accounting-contract.md)、[真实公告小样字段核对](corporate-action-sample-audit.md)和[正式基线就绪审计](baseline-readiness.md)。
