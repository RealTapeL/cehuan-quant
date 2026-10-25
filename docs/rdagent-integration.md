# RD-Agent fin_quant 接入调查

## 上游实际要求

上游 `fin_quant` 是 Qlib 因子与模型共同迭代的研究场景，建议 Python 3.10/3.11。当前文档模板默认 Qlib provider 位于 `~/.qlib/qlib_data/cn_data`，股票池 `csi300`、基准 `SH000300`；模板会用 Qlib 表达式/Alpha158 数据加载器生成特征，并可通过 `StaticDataLoader` 拼接预计算 Parquet 因子。它不是把任意 CSV 路径直接传给 `rdagent fin_quant` 就能运行。

RD-Agent 仓库的环境工具还会为某些 Qlib 场景建立单独 conda 环境并安装指定 Qlib Git commit。策环核心环境使用 PyPI `pyqlib==0.9.7`，而 `uv sync --extra qlib` 当前验证的是 Qlib `StaticDataLoader` 读 Parquet；**当前还没有**将策环 Parquet 转为 RD-Agent 模板所需的 provider 二进制数据、改写股票池或跑 fin_quant。二者数据接口和 Qlib 版本没有通过集成验证前，建议给 RD-Agent 使用独立 uv 环境与独立输出目录。

另一个时点风险：当前安装配置文档说明 `QLIB_QUANT_*` 时间段变量只供 fin_quant 前端 UI 显示，不会改变实际执行区间。真正的 factor/model 训练与测试时间参数要逐个核对运行模板和代码，不可只看 UI。

## 为策环准备的接入路径

1. 先取得点时证券主数据、历史交易日历与可复现日线，保留 `SH/SZ/BJ` 代码和数据版本哈希。
2. 决定是把数据转成 Qlib `.bin` provider，还是为 RD-Agent 模板提供自定义 `StaticDataLoader`；同一批特征和标签必须能在 Qlib 单独重读。
3. 在隔离环境固定 Python、RD-Agent、Qlib Git SHA、模板版本和依赖锁文件；将 `provider_uri`、股票池、基准、训练/验证/测试日期显式写入运行配置。
4. 从无 API 调用的解析/数据验证开始，再运行一个因子或模型候选；记录每次 LLM 请求/Token/模型单价、耗时、CPU/GPU、数据缓存、实验数、生成代码 Git SHA、候选产物和完整日志。
5. RD-Agent 输出仅作为候选；用策环同一股票池、特征可得时间、手续费、滑点、涨跌停和 T+1 假设重跑样本外比较。未通过门槛只记录，不生成 BUY/SELL 交易计划。

## 暂缓原因

fin_quant 依赖模型/API 服务并可能重复产生自动研究调用；当前没有设定研究预算，也没有可供其训练的点时 A 股历史股票池。现在安装/运行只会制造不可比的候选和不透明成本。接入前最少需确定：允许的模型/API 供应商与花费上限、计划股票池/历史区间、基准和样本外门槛。

上游资料：

- [RD-Agent fin_quant 场景说明](https://github.com/microsoft/RD-Agent/blob/main/docs/scens/quant_agent_fin.rst)
- [RD-Agent 安装与时间段配置说明](https://github.com/microsoft/RD-Agent/blob/main/docs/installation_and_configuration.rst)
- [RD-Agent Qlib 环境工具](https://github.com/microsoft/RD-Agent/blob/main/rdagent/utils/env.py)
