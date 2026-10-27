# 完整闭环示例（纯合成行情）

以下命令只使用 `examples/demo_daily.csv` 中的 720 行合成数据、4 个虚构证券代码和 100,000 元演示预算。SIM 代码不是 A 股，所有结果只验证程序路径。

```bash
uv run cehuan quality --data examples/demo_daily.csv
uv run cehuan backtest --data examples/demo_daily.csv --cash 100000
uv run cehuan plan --data examples/demo_daily.csv --cash 100000
uv run cehuan review --plan outputs/latest_plan.csv --fills examples/fills_template.csv \
  --positions examples/positions_template.csv
uv run --extra qlib cehuan qlib-signals --data examples/demo_daily.csv
```

一次实测输出摘要：

- 数据质量：720 行、4 个代码、重复 0 行、必需行情空值 0；行情日期 2025-01-02 至 2025-09-10。
- 合成全区间基线：累计收益约 3.40%、年化波动约 3.77%、最大回撤约 -2.27%、日胜率约 46.67%、24 笔模拟交易。输出还包含按时间顺序拆分的区间指标；规则固定且没有训练模型，不能称为已验证的样本外盈利结果。
- 默认滚动诊断生成 7 个窗口（每窗 60 个数据交易日、每 20 日移动一次）。第一个窗口基线约 +0.22%、输入股票池基准约 +5.45%；最后一个窗口分别约 +1.08% 与 -0.28%。这是同一连续模拟组合的窗口观察，不是独立重置的回测，也不是 walk-forward 样本外验证。
- 等权输入股票池比较基准约 11.04%，基线相对约 -7.64%。它不是指数，也不修正幸存者偏差。
- 计划：输出 SIM003 买入 400 股、SIM004 买入 1,200 股，供文件结构验证。这里的名字、价格、数据都是虚构的。
- 复盘：能够标出 SIM001 的 100 股计划外示例成交，并将 SIM003/4 显示为未成交；费用汇总来自 CSV 输入。
- Qlib：`StaticDataLoader` 读回 720 行，计算 20 日动量后有 640 行非空特征；输出只用于研究，不是委托指令。

## 行情源现场验收（单只股票）

2026-10-27 使用项目 CLI 从腾讯接口拉取贵州茅台 2026-10-19 至 2026-10-26 的日线：6 行、1 个证券、重复 0 行、必需行情字段空值 0，最新数据日 2026-10-26。质量检查提示 ST、上市/停牌状态和交易规则缺失；因此这份日线可用于验证数据转换，不能单独生成可执行计划。

```bash
uv run cehuan fetch --symbol 600519 --start 20261019 --end 20261026 \
  --source tencent --max-age-days 7 \
  --output data/private/raw/SH600519_tencent_20261026.csv
```

默认东方财富接口同一时段发生过代理断连。腾讯当前是显式可选源；数据不会因主源失败而在后台静默换源。

实验指标 JSON、交易、权益曲线和计划复盘文件写到 Git 忽略的 `outputs/`。同一命令再次执行会生成新的 experiment ID 和数据/配置哈希；浮动时间、随机 ID 不作为结果值的一部分。

当前基线仍缺滚动样本外门槛、指数行情、真实成本校准和公司行动处理；不能据此声称真实策略表现。
