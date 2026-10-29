# 外部历史股票池 CSV 导入

当点时股票列表接口无法稳定获取时，可从确实提供按日历史成员的供应商导出 CSV，再用项目适配层规范化。这个入口只导入输入文件中已有的日期成员；不查询当前股票列表，不从今天的证券状态推算过去，也不补齐文件缺失日期。

## 必需字段

| 字段 | 用途 |
|---|---|
| `trade_date` | 该成员记录适用的交易日，格式 `YYYY-MM-DD` |
| `symbol` | 六位代码、`SH600519`、`sh.600519` 或 `600519.SH` |
| `source` | 供应商及接口/数据集名称 |
| `source_record_date` | 供应商记录自身的有效日期；必须等于 `trade_date` |
| `source_retrieved_at_utc` | 取得这份来源记录的带时区 UTC 时间 |

可选字段：`security_type`（1 表示股票，其他已知类型排除）、`daily_trade_status`（1 交易、0 停牌）、`is_suspended`、`is_st`、`st_status_source`、`name`。状态留空时保持未知。此字段约束能拦截把单一当前列表复制到历史日期的常见错误；导入器无法独立证明供应商文件真实，因此仍须审阅来源授权、数据集说明和抽样证据。

## 示例

```csv
trade_date,symbol,source,source_record_date,source_retrieved_at_utc,security_type,daily_trade_status,is_suspended,is_st,st_status_source,name
2025-01-02,SH600519,provider.daily_universe,2025-01-02,2025-01-03T08:00:00Z,1,1,false,,,
```

这行仅展示格式，不是本项目采集或核实过的供应商数据。

## 导入命令

```bash
uv run cehuan universe-import \
  --input data/private/incoming/historical_members.csv \
  --snapshot-id provider-universe-20200101-20251231-v1
```

输出写入 `data/private/universes/<snapshot-id>/`，包括规范化的 `securities_by_date.csv`、逐日覆盖、原始来源文件副本和 SHA-256 清单。已存在的快照 ID 或同名 partial 目录不会被覆盖。命令拒绝无效日期、`source_record_date` 与 `trade_date` 不一致、未来获取时间、代码无效或每日成员重复。

`manifest.json` 中 `bars_attached=false`。导入股票池不能替代行情快照；后续对齐时，缺失行情、ST、停牌和证券类型均保留为缺失或未知，不静默删除。
