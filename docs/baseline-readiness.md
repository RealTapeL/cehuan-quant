# 正式基线就绪审计

`baseline-readiness` 只检查输入与审计条件，不运行策略，也不代表收益得到验证。报告绑定行情、点时质量报告、股票池 manifest/成员/对账、证券身份事件、公司行动、公司行动覆盖证明、市场规则、费率、基准和配置文件的 SHA-256，并记录评价窗口、Git 提交及工作区状态。

## 生成就绪报告

所有输入都必须是同一份评价数据版本。公司行动覆盖表逐证券覆盖整个评价区间，包含已核查“无公司行动”的来源证据；市场规则需逐日给出板块、有效期、限价状态和涨跌幅、买卖最小数量/增量及来源复核信息；费用表需覆盖每个评价交易日，并登记佣金、最低佣金、卖出印花税、过户费及来源。

先运行 `universe-unknown-types`，并逐代码复核未知类型。未解决行必须为零；被确认是股票的行需要更新/重建点时股票池后再运行本审计。否则它们不会进入研究股票池，门控会阻断正式基线。

```bash
uv run cehuan baseline-readiness \
  --data data/private/audits/<audit>/point_in_time_bars.csv \
  --universe data/private/universes/<snapshot-id> \
  --point-in-time-quality data/private/audits/<audit>/point_in_time_quality.json \
  --identity-events configs/security_identity_events.csv \
  --out-date-evidence docs/evidence/universe-outdate-boundary-audit.csv \
  --unknown-type-review data/private/audits/<audit>/unknown-types.csv \
  --unknown-type-bars data/private/snapshots/<snapshot-id>/bars.csv \
  --corporate-actions configs/corporate_actions.csv \
  --corporate-action-coverage data/private/audits/<audit>/corporate-action-coverage.csv \
  --market-rules data/private/audits/<audit>/market-rules.csv \
  --fees data/private/audits/<audit>/fee-schedule.csv \
  --benchmark-data data/private/snapshots/<snapshot-id>/benchmark.csv \
  --config configs/default.toml \
  --start 2023-01-01 --end 2025-12-31 \
  --output data/private/audits/<audit>/baseline_readiness.json
```

退出码 `0` 表示全部审计条件和运行引擎能力通过，`2` 表示已生成 `NOT_READY` 报告，`1` 表示输入或执行错误。报告不是手工可签发的凭证：正式回测会校验报告自身摘要、输入哈希、评价窗口、当前 Git 提交和干净工作区，并再次检查底层审计结果。

正式基线示意命令：

```bash
uv run cehuan backtest \
  --data data/private/audits/<audit>/point_in_time_bars.csv \
  --prevalidated-quality data/private/audits/<audit>/point_in_time_quality.json \
  --readiness-universe data/private/universes/<snapshot-id> \
  --readiness-quality data/private/audits/<audit>/point_in_time_quality.json \
  --identity-events configs/security_identity_events.csv \
  --out-date-evidence docs/evidence/universe-outdate-boundary-audit.csv \
  --unknown-type-review data/private/audits/<audit>/unknown-types.csv \
  --unknown-type-summary data/private/audits/<audit>/unknown-types.csv.summary.json \
  --unknown-type-bars data/private/snapshots/<snapshot-id>/bars.csv \
  --corporate-actions configs/corporate_actions.csv \
  --corporate-action-coverage data/private/audits/<audit>/corporate-action-coverage.csv \
  --market-rules data/private/audits/<audit>/market-rules.csv \
  --fees data/private/audits/<audit>/fee-schedule.csv \
  --benchmark-data data/private/snapshots/<snapshot-id>/benchmark.csv \
  --readiness data/private/audits/<audit>/baseline_readiness.json \
  --evaluation-start 2023-01-01 --evaluation-end 2025-12-31 \
  --cash 100000 --formal
```

## 目前为什么仍会阻断

当前回测引擎尚未把经审计的有效日期规则、历史费率及完整公司行动账本用于逐日组合模拟。因此 readiness 报告会包括 `EXECUTION_ENGINE_DOES_NOT_YET_CONSUME_AUDITED_RULES_FEES_AND_ACTION_LEDGER`，即使输入 CSV 列齐全也不会通过。不得手工删除阻断项。

正式回测唯一支持的行情输入是同一次 `universe-audit` 生成的 `--data <point_in_time_bars.csv>` 与 `--prevalidated-quality <point_in_time_quality.json>`。`--formal --universe` 会被拒绝，避免把原始快照与过滤后内存数据混用。`--readiness-universe` 仍指向原始股票池快照目录，用于重新核验成员、manifest 和证据；它不是行情过滤参数。若同时提供 `--readiness-quality`，其文件内容必须与 `--prevalidated-quality` 相同。

探索回测可使用 `--diagnostic --universe`，也可使用 `--diagnostic --prevalidated-quality`；没有显式诊断标记不能把结果称为正式基线。`baseline-diagnostics` 产生规则回退和假设敏感性场景，也必须显式传 `--diagnostic`。研究诊断结果仍不是模拟账户或华泰真实账户结果。

三年快照汇总中的内部覆盖率只对已分类沪深 `type=1` 股票日成立；未知类型记录不在分母内，北交所未覆盖。该数据集不支持 2026-10-29 当日计划。正式策略评价前还需在持有私有数据的机器上重新核验快照、完成未知类型审计并归档公告原件。
