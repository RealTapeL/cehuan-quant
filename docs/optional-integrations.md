# 可选 Agent 与行情插件核验清单

插件在明确通过覆盖和时点检查前都保持关闭，不能写入基线目标权重或替代统一回测。

## TradingAgents

当前 TauricResearch 上游配置默认以 Yahoo Finance 提供核心行情/技术指标/新闻，且官方代码里有 `.SS`、`.SZ` A 股后缀的基准映射；工具层也有按 `trade_date` 限定历史读取的路径。这说明代码支持部分 A 股代码表示，不证明每个沪深京标的在 Yahoo 历史行情、基本面、新闻上都完整，也不证明其公司行动/停牌/ST/涨跌停规则能代替策环数据。

接入验证：用 600519.SH、000001.SZ、688xxx.SH 和一个已退市标的比对源数据是否存在；记录请求日期/返回数据截止、复权口径、缺失字段和运行时新闻快照；同一历史 `trade_date` 重复跑两次检查输入可否复现。报告只能给候选股票的定性风险观点，不生成统一组合回测指标或计划交易动作。

## TradingView MCP

优先评估 TradingView 官方 MCP 文档所述只读行情能力，而不是默认安装社区同名服务。官方工具要求 `EXCHANGE:TICKER` 形式；OHLCV 时间字段以 Unix UTC 秒返回，需要规范转换到 Asia/Shanghai。官方筛选器列出的市场包含美国、印度、日本等，但未列出中国大陆市场；单股图表可能与筛选器覆盖不同，不能推断 A 股可用。

接入验证：先在不写入策略的只读探针中尝试少量沪深标的和明确日期区间；比较交易日、时区、复权、缺失日和最近 bar 延迟；验证调用权限/套餐与接口稳定性。需要能重复获取同一 `as_of` 数据，否则只可作为当前行情辅助，不参与历史回测。

## 上游链接

- [TauricResearch TradingAgents 配置源码](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/default_config.py)
- [TauricResearch TradingAgents 日期工具源码](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/agents/tools.py)
- [TradingView 官方 MCP 文档](https://www.tradingview.com/mcp/docs)
