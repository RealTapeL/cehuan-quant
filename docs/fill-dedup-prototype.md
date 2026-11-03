# 成交跨文件去重原型

状态：仅为通用规范 CSV 的分类原型。当前工作区没有脱敏华泰成交样本，所以没有确认华泰导出中的成交编号、委托编号、修正/冲正字段，也没有修改 `configs/huatai_columns.toml` 的真实映射。示例中的 `assumed_*` 字段和全部记录都是虚构数据。

## 保守规则

命令要求输入先规范为 `trade_date,symbol,side,quantity,price`。可选 `trade_time` 用于缩小疑似重叠范围，`order_id` 仅作为留痕输出。默认不采用任何稳定成交标识：交易日期、证券、方向、数量、价格和时间相同只会标为 `POSSIBLE_DUPLICATE`，不会自动去重。

只有操作者显式指定一列为稳定 execution ID 时，原始编号相同且核心成交内容相同才标记 `EXACT_DUPLICATE`；相同编号但核心内容不同则标为 `CONFLICT`。如果券商编号只在单日、单账户或单文件唯一，传入的规范标识列应先组合日期/账户别名等字段；本原型本身不会假定编号唯一范围。若显式 correction-link 指向先前记录，标为 `CORRECTION_OR_REVERSAL`，仍需人工核对原件语义。不同 ID 的相同成交字段会保留为 `NEW`，以免合并两笔真实成交。报告输出每条当前记录、输入文件散列和 CSV 行号，不合并、不覆盖、不写成交登记簿；输出文件必须尚不存在。

这与 `review --import-registry` 的整个文件哈希登记互补：后者负责拒绝完全相同的文件并绑定复盘产物；本原型只生成重叠审查报告，不改变登记状态，失败重跑不会重复累计账户成交。

## 合成样例

下面列名特意使用 `assumed_*`。它们不代表华泰实际字段。

```bash
uv run cehuan fills-dedup \
  --prior examples/fill_dedup_prior_synthetic.csv \
  --current examples/fill_dedup_current_synthetic.csv \
  --execution-id-column assumed_execution_id \
  --correction-link-column assumed_correction_link \
  --status-column assumed_record_status \
  --output /tmp/cehuan-fill-dedup-synthetic.csv
```

预期分类计数：`EXACT_DUPLICATE=1`、`NEW=1`、`CORRECTION_OR_REVERSAL=1`。示例只验证原型控制流；它不能证明任何券商字段稳定或唯一。

## 后续华泰字段调查

拿到脱敏样本后，先逐字段确认日期/时间精度、成交编号唯一范围、同一订单多笔成交、跨文件重叠、更正和冲正表达方式，再新增专属映射。若成交编号不稳定或只在单个导出内唯一，必须把适用范围记录进映射，不用于跨文件自动判重。身份字段和账号应留在本机脱敏；原始成交文件继续留在私有目录。
