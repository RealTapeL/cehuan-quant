# 策略 checkpoint 的增量输入兼容规则

## 当前行为

策略 checkpoint 目前绑定完整的公司行动输入、证券身份事件、交易日历、规则、费率、策略配置及已使用行情。续跑时这些输入任一变化都会被拒绝；包括仅追加未来日历日期或只追加一条 checkpoint 之后才公告的事件。该严格策略是当前实现，不支持公告增量合并。

这条边界刻意保守：新公告可能是更正，也可能改变 checkpoint 前已经建立的持仓权利、税务批次或价格处理。仅凭事件生效日期在未来，不能证明它不会回溯影响历史。

## 只读预检器（研究用，不放行续跑）

`src/cehuan/backtest/incremental_inputs.py` 提供第一版兼容性预检。它比较两份显式输入证明，并报告候选追加、历史变化、阻断原因和可能受影响日期。它**不会**修改 checkpoint，也不会改变 `run_backtest()` 当前的完整输入哈希校验；报告中的 `can_continue` 和 `engine_resume_supported` 始终为 `false`。

命令：

```bash
uv run python scripts/audit_checkpoint_incremental_inputs.py \
  --checkpoint data/private/checkpoints/backtest_checkpoint.json \
  --baseline-proof data/private/checkpoints/input-proof-baseline.json \
  --candidate-proof data/private/checkpoints/input-proof-candidate.json \
  --output outputs/checkpoint-incremental-audit/report.json
```

输入证明 JSON 的 schema_version 为 1，结构示意：

```json
{
  "schema_version": 1,
  "increment_id": "opaque-stable-id-for-this-import",
  "checkpoint": {
    "state_sha256": "<checkpoint state SHA-256>",
    "as_of_date": "2025-01-13",
    "next_expected_session": "2025-01-14",
    "decision_cutoff_utc": "2025-01-13T08:00:00Z",
    "cutoff_source": "明确说明截止时间依据的来源",
    "cutoff_source_sha256": "<evidence SHA-256>",
    "applied_increment_ids": [],
    "applied_increment_sha256s": []
  },
  "inputs": {
    "strategy_config_sha256": "<SHA-256>",
    "historical_bars_prefix_sha256": "<SHA-256>",
    "events": [{
      "event_id": "event-id",
      "content_sha256": "<canonical event SHA-256>",
      "first_impact_date": "2025-01-15",
      "known_at_utc": "2025-01-13T08:01:00Z",
      "source_sha256": "<source SHA-256>"
    }],
    "calendar": [{"date": "2025-01-14", "is_open": true}],
    "rules": [{
      "row_id": "rule-id", "valid_from": "2025-01-15", "valid_to": null,
      "content_sha256": "<canonical row SHA-256>",
      "known_at_utc": "2025-01-13T08:01:00Z", "source_sha256": "<source SHA-256>"
    }],
    "fees": []
  }
}
```

执行约束：

- 实际 checkpoint 必须先通过 `read_backtest_checkpoint()` 的封存校验，且 baseline proof 的 checkpoint 哈希、截止日和下一开市日必须与它一致。
- 截止时点必须由调用者显式提供来源及 SHA-256；工具不从 `as_of_date` 推断盘前/盘后时刻，也不验证来源文件内容的法律或业务含义。
- baseline 中每条已使用事件也需要带时区 `known_at_utc`。若旧事件只有公告日期或缺少首次可得证据，预检会报告历史来源证明不足并要求重新计算；不能用新造的 cutoff 替旧输入补证。
- 事件只有在 ID 为新增、来源和内容哈希有效、`known_at_utc` 严格晚于截止时点且 `first_impact_date` 晚于 checkpoint 日时，才会列为候选新增。事件更正、删除、晚到影响历史或精确时点不明都阻断。
- 日历只接受新增的未来日期。已有日期不能修改或删除；原 checkpoint 记录的下一开市日必须仍然存在且保持开市。
- 新规则和费率要求未来生效、截止时点之后才可得，并提供行内容和来源哈希。当前项目规则/费率 CSV 没有 `known_at_utc`，因此直接把新行导入现有表还不能满足此预检。
- 策略配置及已使用行情前缀必须保持相同哈希。重复 `increment_id` 或相同追加内容摘要会被报告为已应用；baseline 与 candidate 必须引用同一份 checkpoint 状态及已应用增量登记。
- 报告是候选兼容性证据，不是输入合并器或收益验证；增量事件重复应用、跨窗口账本/成交/NAV 一致性，以及与连续运行等价仍未由本预检器实现。

报告至少包含 `candidate_append_compatible`、`can_continue`、`engine_resume_supported`、追加项、变更项、阻断项、最早可能影响日期和是否需要重算。若只有整体前缀哈希变化而没有逐行差异，日期精度会明确标成未知，不能猜测一个回溯起点。

## 未来增量输入的候选规则

如果以后实现增量续跑，输入必须分成不可变历史前缀与可追加未来后缀，并对每类输入分别核验：

当前 checkpoint 只保存 `as_of_date`，没有精确的决策截止时间。因此未来增量协议还必须新增带时区的 `decision_cutoff_utc`；只知道公告日期或 checkpoint 日期，不能证明公告在当时尚不可得。同一日期内的公告时间顺序不明时，候选增量一律阻断。

| 输入 | 允许追加的候选条件 | 必须回溯重算的条件 |
|---|---|---|
| 公司行动/证券身份事件 | `known_at` 明确晚于 checkpoint 决策截止时点；事件首次影响日期晚于 checkpoint；事件 ID 未使用；来源版本与更正链可追溯 | 已有事件被更正/删除；新事件的权利基准日、生效日或税务关联早于/等于 checkpoint；可得时点不明或仅精确到日期、无法证明当时未知 | 
| 交易日历 | 已处理日期和原有下一开市日完全一致；新增日期严格位于 checkpoint 后，且重算后续边界 | 修改历史开市状态、删除既有日期、改变下一开市日或历史交易日序号 | 
| 行情 | 过去窗口规范化数据与历史链哈希一致；新行情日期不早于续跑边界 | 修正/补录已使用行情，或预热特征依赖的历史数据改变 | 
| 规则、费率、配置 | 新增规则只能覆盖未来有效区间，并且显式保留原历史前缀；策略参数仍须完全一致 | 历史有效规则、费用、特征或策略参数变化；影响日期无法精确定位 | 

发生历史变化时，系统应报告最早可能受影响日期，丢弃该日期之后的 checkpoint 链并重新计算；不能在已有账本上做静默修补。时间只有“公告日期”而没有可靠时刻时，不应推断盘前可用。

## 当前验收状态

预检器的单元测试覆盖未来事件追加、历史影响事件、事件更正、盘前/盘后边界、日期级时点拒绝、未来日历追加、历史日历修改、未来规则附证、历史费用变化和重复增量 ID。它们验证的是**报告分类逻辑**，不是策略状态的实际增量续跑。

直到增量合并、晚到更正、重复应用、完整策略 checkpoint 续跑与连续/分段成交、账本和 NAV 一致性都进入引擎并通过测试前，继续使用完整哈希相等校验。正式基线状态也不因这个预检器改变。
