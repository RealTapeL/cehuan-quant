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

历史合成预检证明使用 `schema_version=1`；从本地实际输入重建的证明使用 `schema_version=2`，并包含整份 proof 的 `canonical_payload_sha256`。下面的旧示意省略了 canonical payload 的具体行情/事件/费率字段；实际文件还应登记 `checkpoint.input_hashes`、真实输入文件路径及其摘要：

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
    "cutoff_source_artifact": {"path": "data/private/evidence/cutoff.pdf", "sha256": "<evidence SHA-256>"},
    "applied_increment_ids": [],
    "applied_increment_sha256s": [],
    "input_hashes": {
      "config_sha256": "<checkpoint config hash>",
      "market_rules_sha256": null,
      "fee_schedule_sha256": null,
      "accounting_events_sha256": "<hash>",
      "identity_events_sha256": "<hash>",
      "corporate_actions_sha256": "<hash>",
      "calendar_sha256": "<hash>",
      "calendar_prefix_sha256": "<hash>",
      "last_window_data_sha256": "<hash>",
      "last_window_bars_sha256": "<hash>",
      "data_chain_sha256": "<hash>"
    }
  },
  "inputs": {
    "strategy_config_sha256": "<SHA-256>",
    "historical_bars_prefix_sha256": "<SHA-256>",
    "events": [{
      "event_id": "event-id",
      "content_sha256": "<canonical event SHA-256>",
      "first_impact_date": "2025-01-15",
      "known_at_utc": "2025-01-13T08:01:00Z",
      "source_sha256": "<source SHA-256>",
      "event_type": "CASH_DIVIDEND",
      "symbol": "SH600001",
      "gross_per_share_cny": "0.1",
      "canonical_content": {
        "event_id": "event-id",
        "first_impact_date": "2025-01-15",
        "known_at_utc": "2025-01-13T08:01:00Z",
        "source_sha256": "<source SHA-256>",
        "event_type": "CASH_DIVIDEND",
        "symbol": "SH600001",
        "gross_per_share_cny": "0.1"
      },
      "source_path": "data/private/events/event.pdf"
    }],
    "calendar": [{"date": "2025-01-14", "is_open": true}],
    "rules": [{
      "row_id": "rule-id", "valid_from": "2025-01-15", "valid_to": null,
      "content_sha256": "<canonical row SHA-256>",
      "known_at_utc": "2025-01-13T08:01:00Z", "source_sha256": "<source SHA-256>",
      "buy_step_shares": 100,
      "canonical_content": {
        "row_id": "rule-id", "valid_from": "2025-01-15", "valid_to": null,
        "known_at_utc": "2025-01-13T08:01:00Z", "source_sha256": "<source SHA-256>",
        "buy_step_shares": 100
      },
      "source_path": "data/private/rules/rule.pdf"
    }],
    "fees": []
  }
}
```

执行约束：

- 实际 checkpoint 必须先通过 `read_backtest_checkpoint()` 的封存校验。CLI 分别报告证明 schema、checkpoint 状态/日期/下一开市日/完整输入哈希绑定，以及候选增量规则检查；checkpoint proof 缺少任何必要输入哈希时标记 `UNVERIFIED`，不当作通过。
- 截止时点必须由调用者显式提供来源及 SHA-256；工具不从 `as_of_date` 推断盘前/盘后时刻，也不验证来源文件内容的法律或业务含义。
- 事件、规则和费率的 `content_sha256` 会对 `canonical_content` 规范 JSON 重新计算；证明行展示的元数据及业务字段必须与规范内容一致。`canonical_payload_digest_valid` 表示这项自洽检查，缺 canonical payload 为 `UNVERIFIED`，摘要不符或行字段不一致为 `FAILED`。它不证明 payload 是由项目适配器从原始文件生成；后者由单独的 `normalized_inputs_binding_valid` 表示，目前为 `UNVERIFIED`。
- CLI 会分别重算 baseline 和 candidate 声明的输入文件及来源原件 SHA-256。`input_file_hashes_valid=VERIFIED` 只表示登记路径上的字节与登记摘要一致；它不证明规范行由这些字节解析得到。缺路径/摘要为 `UNVERIFIED`，文件缺失或哈希不符为 `FAILED`。
- `normalized_inputs_binding_valid` 只有通过下文的重建命令，并将对应重建报告传给预检 CLI 后，才会由适配器结果决定。缺少重建报告仍为 `UNVERIFIED`；即使它变为 `VERIFIED`，当前 `candidate_append_compatible`、`can_continue` 也不会放行执行。
- baseline 中每条已使用事件也需要带时区 `known_at_utc`。若旧事件只有公告日期或缺少首次可得证据，预检会报告历史来源证明不足并要求重新计算；不能用新造的 cutoff 替旧输入补证。
- 事件只有在 ID 为新增、来源和内容哈希有效、`known_at_utc` 严格晚于截止时点且 `first_impact_date` 晚于 checkpoint 日时，才会列为候选新增。事件更正、删除、晚到影响历史或精确时点不明都阻断。
- 日历只接受新增的未来日期。已有日期不能修改或删除；预检会从候选日历重新计算 `as_of_date` 之后最早的开市日，必须与 checkpoint 的 `next_expected_session` 相同。不同则报告 `NEXT_SESSION_CHANGED` 并列出原日期及候选日期。早于原下一开市日新增的休市日不改变该判断。
- 新规则和费率要求未来生效、截止时点之后才可得，并提供行内容和来源哈希。当前项目规则/费率 CSV 没有 `known_at_utc`，因此直接把新行导入现有表还不能满足此预检。
- 策略配置及已使用行情前缀必须保持相同哈希。重复 `increment_id` 或相同追加内容摘要会被报告为已应用；baseline 与 candidate 必须引用同一份 checkpoint 状态及已应用增量登记。
- 报告是候选兼容性证据，不是输入合并器或收益验证；增量事件重复应用、跨窗口账本/成交/NAV 一致性，以及与连续运行等价仍未由本预检器实现。

报告分别给出 `proof_schema_valid`、`checkpoint_binding_valid`、`input_file_hashes_valid`、`input_files_binding_valid`、`canonical_payload_digest_valid`、`normalized_inputs_binding_valid`、`candidate_rules_compatible`、`candidate_append_compatible`、`can_continue` 和 `engine_resume_supported`，并列出追加项、变更项、阻断项、最早可能影响日期及是否需要重算。当前 `candidate_append_compatible`、`can_continue`、`engine_resume_supported` 均保持 `false`。若只有整体前缀哈希变化而没有逐行差异，日期精度会明确标成未知，不能猜测一个回溯起点。

## 从本地输入重建规范化证明

`scripts/rebuild_checkpoint_input_proof.py` 使用实际回测适配器生成 `schema_version=2` 的 baseline proof 和独立重建报告。该操作只读输入；它不改 checkpoint、不合并增量、不运行策略。输出目录必须是新的目录，避免覆盖旧审计产物。

```bash
uv run python scripts/rebuild_checkpoint_input_proof.py \
  --checkpoint data/private/checkpoints/backtest_checkpoint.json \
  --config configs/default.toml \
  --bars data/private/windows/window-last.csv \
  --calendar data/private/trading-calendar.csv \
  --identity-events configs/security_identity_events.csv \
  --corporate-actions configs/corporate_actions.csv \
  --market-rules data/private/rules/market-rules.csv \
  --fees data/private/rules/fees.csv \
  --history-manifest data/private/windows/history-manifest.json \
  --provenance-registry data/private/evidence/input-provenance.csv \
  --cutoff-evidence data/private/evidence/checkpoint-cutoff.json \
  --output-dir data/private/proofs/rebuild-2026-11-03
```

输出文件：

- `input-proof-baseline.json`：由配置、规范行情、日历、规则、费用、身份事件、公司行动和 ledger 事件适配器生成的规范内容；包含 proof 自身的 canonical SHA-256。
- `input-proof-rebuild-report.json`：列出每个输入的路径/私有别名、字节 SHA-256、适配器与摘要版本、重建摘要、checkpoint 摘要、比较状态和未验证原因。CLI 另外记录 proof 文件及 checkpoint 文件散列。

适配器和哈希边界：

- 共享函数位于 `src/cehuan/backtest/input_hashes.py`，标记为 `checkpoint-v1`。它们保留原回测的 JSON/CSV/pandas 哈希序列化，不改变旧 checkpoint 的解释方式。未来若修改字段范围或规范化算法，必须升版，不能重算后静默复用 v1。
- 配置走 `load_config()` 与 checkpoint 既有配置摘要；行情走 `read_bars()`，按 `BAR_COLUMNS` 计算窗口摘要；日历、规则、费率及身份/公司行动表各自调用项目规范化器；账务事件走 `read_accounting_event_csv()` 和 `AccountingEventSchedule.build()`，再按 checkpoint v1 事件加税务字段摘要。
- 若 checkpoint 绑定的公司行动/税务摘要不是空事件摘要，必须传入当时使用的 `--accounting-events` CSV；不提供时，空事件重建会与 checkpoint 不符并阻断绑定。
- `--history-manifest` 必须列出形成 checkpoint `data_chain_sha256` 的**全部**窗口，按执行顺序排列。示例：

  ```json
  {
    "schema_version": 1,
    "windows": [
      {"bars_path": "window-01.csv", "data_hash_mode": "raw_file"},
      {"bars_path": "window-02.csv", "data_hash_mode": "normalized_frame", "benchmark_path": "benchmark-02.csv"}
    ]
  }
  ```

  路径相对 manifest 所在目录解析；`data_hash_mode` 必须与原运行是否绑定源文件字节一致。缺一个旧窗口、窗口重叠/乱序、末日与 checkpoint 不同或重算链不匹配时，历史链不会被宣称通过。只有最后窗口而没有完整清单时，`historical_data_chain` 为 `UNVERIFIED`。
- 市场规则、费率及事件的 `canonical_row_sha256` 由独立 provenance CSV 按稳定 `row_id` 对应。登记表字段：

  ```text
  input_type,row_id,canonical_row_sha256,source_id,source_file_path,source_file_sha256,known_at_utc,known_at_evidence,review_status,reviewed_by,reviewed_at
  ```

  `MARKET_RULE` 的 `row_id` 格式为 `rule:<symbol>:<valid_from>`；`FEE` 为 `fee:<market>:<valid_from>`；`ACCOUNTING_EVENT` 使用事件 `event_id`。来源原件路径可相对 registry 文件，SHA-256 会实读复算。`known_at_utc` 必须有时区；仅日期将保持 `UNVERIFIED`。这张表只能证明登记的映射、文件字节和复核字段彼此一致，不会判断公告解读是否正确。
- 当前证券身份事件和合并公司行动 CSV 没有与原件归档路径一一绑定的统一审核输入，因此其底层来源内容/首次可得时点不会被报告成已验证；若这些表非空，对应状态保持 `UNVERIFIED`。CSV 自身的字节哈希和规范化摘要仍会单独核对。
- checkpoint v1 只有日期，没有精确决策截止时间。只有独立 cutoff 记录与 checkpoint `state_sha256` 绑定，且来源文件、UTC 时间及审核字段通过校验，才将截止时点列为 `VERIFIED`。记录格式为 `schema_version=1`，字段至少包括 `checkpoint_state_sha256`、`decision_cutoff_utc`、`source_id`、`source_file_path`、`source_file_sha256`、`review_status`、`reviewed_by`、`reviewed_at`。没有证据时不推断盘前/盘后时间。

报告把四个层次和证据状态分开：

| 报告字段 | 通过所表示的范围 |
|---|---|
| `input_file_hashes_valid` | proof/registry 所登记的文件字节与实读 SHA-256 相符；缺登记材料为 `UNVERIFIED` |
| `canonical_payload_digest_valid` | proof 的整体 canonical 摘要及事件、规则、费用规范行摘要自洽 |
| `normalized_inputs_binding_valid` | 项目适配器从给定输入重建的规范内容与 proof 相同；不会因为原文件仅有格式变化就把规范化对比混为文件哈希对比 |
| `checkpoint_binding_valid` | 重建配置、日历、事件、规则、费用、行情窗口和完整行情链摘要与已封存 checkpoint 一致 |
| `source_content_review_status` | 来源/字段的人工审核登记状态；哈希通过本身不代表公告内容解释正确 |
| `known_at_evidence_status` | cutoff 与各输入首次可得时点证据是否足以进行点时判断 |

`VERIFIED`、`UNVERIFIED`、`FAILED` 分别代表所需计算完成、证据不足、发现明确冲突。文件哈希变化但适配器规范结果不变时，文件层可失败而规范化绑定仍通过；checkpoint 层和来源审核层也分别报告。输出证明自身带 `canonical_payload_sha256`，对 proof 做任何修改后必须重算此摘要；即便重算后自洽，重建器仍会对照输入适配器的实际结果。

将输出接入现有增量预检（仍然不会续跑）：

```bash
uv run python scripts/audit_checkpoint_incremental_inputs.py \
  --checkpoint data/private/checkpoints/backtest_checkpoint.json \
  --baseline-proof data/private/proofs/rebuild-2026-11-03/input-proof-baseline.json \
  --candidate-proof data/private/checkpoints/input-proof-candidate.json \
  --baseline-rebuild-report data/private/proofs/rebuild-2026-11-03/input-proof-rebuild-report.json \
  --output data/private/proofs/rebuild-2026-11-03/preflight-report.json
```

要让 baseline 与 candidate 两份 proof 的规范化绑定都可判定，还需分别提供 `--candidate-rebuild-report`。没有对应报告时保持 `UNVERIFIED`。即使所有输入绑定均为 `VERIFIED`，预检仍明确写出 `can_continue=false`、`candidate_append_compatible=false`；本轮没有实现增量合并或回测执行。

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

预检器的单元测试覆盖未来事件追加、历史影响事件、事件更正及关键元数据/业务字段篡改、规范摘要不符、盘前/盘后边界、日期级时点拒绝、候选下一开市日重算、历史日历修改、规则/费率关键字段变化、来源文件 SHA-256 和重复增量 ID。它们验证的是**证明及报告分类逻辑**，不是策略状态的实际增量续跑，也没有验证规范化行确实由源文件解析生成。

直到增量合并、晚到更正、重复应用、完整策略 checkpoint 续跑与连续/分段成交、账本和 NAV 一致性都进入引擎并通过测试前，继续使用完整哈希相等校验。正式基线状态也不因这个预检器改变。
