# 架构与运行边界

```text
AKShare Eastmoney/Tencent ─┐
                            ├──> explicit source adapters ──> canonical daily bars ──┬──> baseline signals/backtest
BaoStock private snapshot ──┘                                                       ├──> Qlib StaticDataLoader / momentum feature (optional extra)
BaoStock index reference ──────────────────────────────────────────────────────────┤
                                                                                    ├──> review plan CSV/Markdown ──> manual Huatai import
                                                                                    └──> actual fills/positions CSV ──> review report
```

- 行情源：`src/cehuan/data/akshare_adapter.py` 分别适配 AKShare 东方财富与腾讯端点，对字段和成交量单位做映射；来源选择显式配置，策略只消费规范列。
- 真实快照：`src/cehuan/data/baostock_adapter.py` 是可选、单独安装的数据源，用于保存小股票池的原始响应与版本清单。沪深 300 作为独立基准文件，不进入交易股票池。它当前缺逐日涨跌停和交易单位，不能生成个人交易计划。
- 研究：技术基线按后复权收盘计算动量；Qlib 静态数据读取与动量特征命令可独立运行。它未训练模型，也不能把技术基线描述为已验证盈利策略。
- 回测：信号在收盘后形成，下一交易日开盘模拟；费用、滑点、交易单位、价格阈值来自 TOML。数据不足时记录阻断原因。
- 输出：每次回测按 experiment ID 写独立目录；交易计划是独立文件，有数据截止和生效时间；计划不是券商委托。
- 复盘：成交 CSV 对账只解释输入记录支持的事实。原因判断需要委托状态、盘口/行情和持仓快照等证据。
- CLI：在 Arch 上通过 SSH 登录后运行；行情和账户 CSV 留在本机项目数据目录。
- 实测：本机有限数据覆盖与当前动量基线结果见[数据源审计](data-provider-audit.md)和[真实数据基线报告](real-data-baseline.md)。

## 故意暂缓的组件

- Qlib：已用 `StaticDataLoader` 读取统一 Parquet 并计算基线动量特征；若后续使用 `.bin` provider、模型工作流或 recorder，再单独评估官方转换脚本和独立环境。
- RD-Agent fin_quant：独立 Python 环境，确认输入数据目录、模型/API 费用与实验资源后再启用。候选须与基线使用同一股票池、交易假设和样本外区间。
- TradingAgents：只可为个股报告提供可核验辅助信息。上游依赖数据会随运行时变化，不直接用于历史回测标签或计划交易动作。
- tradingview-mcp：接入前检查 A 股覆盖、时区、延迟和接口可用性；当前不作为必需行情源。
- 华泰：暂无程序化交易接口权限证明。成交与持仓目前仅用用户手动导出的 CSV，不保存账户登录信息。
