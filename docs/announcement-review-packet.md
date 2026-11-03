# 公司行动公告人工审阅包

项目登记了三条事件样本：明星电力现金分红、同日转增，以及中航成飞代码变更。前两条引用同一份发行人公告，因此当前事件清单对应两份唯一公告原件。样本不是全市场事件覆盖证明。

在保存私有原件的机器上运行：

```bash
uv run python scripts/build_accounting_sample_review_pack.py \
  --output outputs/corporate-action-review-pack
```

脚本会核验登记 SHA-256、复制公告 PDF 和提取文本到本机输出目录，并生成逐字段 `review_fields.csv`、候选页码/原文摘录、`package_manifest.json` 和阅读说明。登记表中的 `suggested_review_outcome` 和 `review_guidance` 是机器提示，不是人工结论；`review_status` 仍为 `PENDING`，签认摘录、原因、审核人、日期和复核时原件 SHA-256 保持空白。输出目录已被 `.gitignore` 中的 `outputs/` 排除；不要把私有原件或账户资料加入 Git。

页码和摘录仅用于缩短人工定位时间。维护者需要打开复制的 PDF 对照逐字段的公告值、项目规范字段和账户事实边界。脚本不修改两份输入证据表的审核状态；所有字段仍须由实际复核人签认。将状态改成 `VERIFIED_FROM_ANNOUNCEMENT`、`NOT_IN_ANNOUNCEMENT`、`NEEDS_OTHER_SOURCE` 或 `UNRESOLVED` 时，构建器会检查审核人、日期、与样本一致的原件 SHA-256，以及对应摘录或原因。原件哈希匹配仅证明副本字节一致，不证明摘录、法律含义或会计解释正确。

需要账户或结算资料的项目（实际分红入账、后续扣税、个人送转整数股获配等）应标记为“公告不包含/需账户证据”，不能仅凭发行人公告标记已核实。公告只给出日期而未披露精确首次可得时刻时，应保留时刻未知。

自动生成审阅包前可先运行只读校验：

```bash
uv run python -m cehuan.data.accounting_sample_audit \
  --manifest docs/evidence/corporate-action-sample-audit.csv \
  --output outputs/corporate-action-sample-audit.json
```

自动检查覆盖原件散列与提取文本中的指定词句，不代表人工签认。
