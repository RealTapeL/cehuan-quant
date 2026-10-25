# 策环（cehuan-quant）

策环是一个本地运行的 A 股量化研究与人工审核命令行工具。第一阶段支持 AKShare 历史日线适配、数据质量检查、技术基线回测、交易计划文件和华泰导出 CSV 复盘模板。

**系统只生成研究材料和待审核计划，不连接华泰账户、不读取账户凭据、不自动下单。** 回测、模拟、研究候选和真实账户结果分别记录。任何收益指标都不代表未来表现。

## 一页实施方案与技术风险

| 顺序 | 实施范围 | 选择 |
|---|---|---|
| 1 | 数据底座 | Python 3.11、`uv` 锁依赖；AKShare 通过适配器提供统一字段 |
| 2 | 可复现技术基线 | 20 日动量排名、配置化调仓/仓位/费用/交易单位；信号取收盘后数据，回测最早下一交易日开盘执行 |
| 3 | 审核计划和人工复盘 | 独立 CSV/Markdown 文件；成交模板按计划汇总未成交、成交数量、VWAP 差异与费用 |
| 4 | Qlib/RD-Agent 和可选插件 | Qlib 用隔离 extra 验证静态数据加载；RD-Agent 独立环境，待数据和样本外流程稳定后再接入；TradingAgents、TradingView 保持关闭 |

主要技术风险：AKShare/Eastmoney 是公开网页数据接口，字段或访问可能变化；日线数据按 AKShare 文档应收盘后取用；单只行情接口不提供历史 ST、上市状态、停牌日和交易所日历，股票池必须有时点正确的证券状态才能形成可执行计划；不同板块及 ST 的涨跌幅随证券类别和规则日期变化；只使用当前股票列表会有幸存者偏差；未建模的分红、送转等公司行动会影响按原始价格结算的回测。系统会保留未知状态并阻止其成为可直接审核下单的候选，不把缺失状态默认为正常股票。

## 环境实测记录

当前执行环境实测为 **Arch Linux x86_64、系统 Python 3.14.6、uv 0.11.32**，不是 macOS；目录最初为空、没有 Git 仓库。Qlib 官方 README 列出的支持范围为 Python 3.8–3.12，Qlib 在 Apple Silicon 上构建 LightGBM 可能需要 `brew install libomp`。本项目因此固定 Python 3.11；仓库在 Linux 实测，macOS 需在你的 MacBook 上执行下方安装命令复核。

## 安装

需要 `uv` 和 Git。项目 Python 版本由 `.python-version` 固定为 3.11，`uv.lock` 固定解析依赖版本。

```bash
cd ~/Projects/cehuan-quant
uv sync
uv run cehuan --help
```

安装可选 Qlib 读取器（不安装 RD-Agent、TradingAgents 或 TradingView 客户端）：

```bash
uv sync --extra qlib
uv run cehuan qlib-check --data examples/demo_daily.csv
uv run cehuan qlib-signals --data examples/demo_daily.csv
```

Apple Silicon 如果 Qlib 安装时 LightGBM 编译失败，先 `brew install libomp` 再运行 `uv sync --extra qlib`。当前没有在 macOS 主机实测该步骤。

## 从 MacBook SSH 使用 Arch 上的项目

从 MacBook 登录 Arch 后直接运行 CLI，研究数据与输出保存在 Arch 项目目录：

```bash
ssh 用户名@Arch服务器地址
cd ~/Projects/cehuan-quant
uv run cehuan --help
```

当前阶段通过 CLI 执行任务，SSH 会话退出后已生成的文件仍保存在 Arch 项目目录。

## 运行闭环示例

仓库带有**合成演示数据**，其中 SIM 开头的代码不是 A 股代码：

```bash
uv run cehuan backtest --data examples/demo_daily.csv --cash 100000
uv run cehuan plan --data examples/demo_daily.csv --cash 100000
uv run cehuan review --plan outputs/latest_plan.csv --fills examples/fills_template.csv
```

如果同时导入成交后持仓快照，可附加 `--positions <持仓CSV>`；报告会增加实际股数与计划目标差异，以及输入成本价/现价可计算的浮动盈亏。

输出写入 `outputs/`（Git 忽略），包括 `backtest_metrics.json`、`trades.csv`、`latest_plan.csv`、`latest_plan.md`、`review.csv` 和 `review.md`。演示用的 100,000 元仅为合成样例输入，不是对你账户资金的假设。

获取单只股票日线示例：

```bash
uv run cehuan fetch --symbol 600519 --start 20250101 --end 20251231
```

有限股票池在 `configs/stock_pool_template.csv` 中填入代码和名称后，可一次按顺序获取；默认最多 50 个代码，完整性或质量检查失败时不会写入新的池数据文件：

```bash
uv run cehuan fetch-pool --universe configs/stock_pool_template.csv \
  --start 20250101 --end 20251231 \
  --security-master data/private/security_master.csv
```

AKShare 文档说明该接口数据按日更新，当日收盘价应在收盘后取得；返回量单位是“手”，适配器转成“股”，成交额单位为元。接口使用原始价和后复权价两次读取：原始价用于计划参考价/成交价格约束，后复权收盘价用于连续性较好的动量特征。单只股票抓取结果缺少历史 ST 和上市状态，所以这条命令仅验证行情接口与规范化；状态未知的标的不能进入可执行计划。

## 配置和研究口径

股票池、动量窗口、调仓节奏、仓位上限、回测缺省涨跌幅与交易单位、费用、现金和基准口径均在 `configs/default.toml`。证券实际交易规则应通过带有效期和来源的证券主数据提供；规则缺失时计划只给出 `VERIFY`，不能作为下单依据。默认值是技术占位假设，配置不会从华泰账户读取资金。

日线基线的时间顺序：收盘后读取日期为 *t* 的日线并计算信号，最早于下一交易日 *t+1* 开盘审核执行。AKShare 日线没有历史停牌行时，不能仅靠行情表准确构建交易所日历；历史 ST、涨跌幅区间和板块交易单位需带生效日期的证券主数据。现阶段计划会在这些规则不全时输出 `VERIFY`/`HOLD`，回测缺失规则时则使用显式标注的研究配置假设。配置费用、板块规则与交易单位并非固定真值，使用前要核对当日交易所规则、华泰订单限制和账户实际费率，详见 [市场规则口径](docs/market-rules.md)。

Qlib 暂以官方 `StaticDataLoader` 读取项目导出的 Parquet 多级索引数据，避免把 Qlib 原始行情文件格式混入业务层；它是 Qlib 数据层接入示例，不声称已完成模型训练或成熟的组合回测。RD-Agent 的 fin_quant 模板需要其自己的 Qlib provider 数据路径、实验配置和模型服务；目前仅记录集成入口，不克隆/安装上游仓库、不把候选直接转成交易指令。TradingAgents 原版依赖 Yahoo Finance 等外部时点数据，未证明 A 股历史时点适配前禁用；tradingview-mcp 亦待验证 A 股覆盖与时间戳后再接入。

## 华泰 CSV 模板

- `examples/fills_template.csv`：成交日期、委托/成交编号、代码、方向、数量、成交价、费用、计划编号、成本价（卖出计算已实现盈亏用）。
- `examples/positions_template.csv`：快照日期、代码、名称、持仓数量、可卖数量、成本价、最新价；复盘时用 `--positions` 导入。
- `examples/security_master_template.csv`：代码、名称、板块、有效起止日期、ST/上市/停牌状态、是否有限价、涨跌幅、买卖数量规则及来源。
- `configs/stock_pool_template.csv`：你维护的有限证券池，包含代码和名称。

实际华泰导出文件格式未知，所以先用通用 CSV 列模板；`configs/huatai_columns.toml` 提供成交与持仓的列名别名及买卖值映射，按你的导出表头编辑后使用 `--mapping configs/huatai_columns.toml`。证券主数据需由你提供带有效日期的可靠来源，不要把当前状态向历史回填。不要把真实成交、持仓、账户资料、密钥或大体量行情放进 Git。

将真实状态文件保存在 Git 忽略的本地目录后，可这样应用：

```bash
uv run cehuan fetch --symbol 600519 --start 20250101 --end 20251231 \
  --security-master data/private/security_master.csv
uv run cehuan plan --data data/private/raw/600519_daily.csv --cash 100000 \
  --security-master data/private/security_master.csv --positions data/private/positions.csv
```

示例中的现金数值须替换为你主动设定的研究预算；程序不会读取账户余额。

## 阶段验收

1. `uv run cehuan fetch ...` 返回规范字段且重复/价格检查无错误；失败时保存错误信息，不静默生成计划。
2. `uv run cehuan backtest ...` 输出带数据区间、调仓成本、样本限制和基准口径的回测文件。
3. `uv run cehuan plan ...` 生成独立审核文件、明确数据截止时间和最早执行时点。
4. `uv run cehuan review ...` 对照计划和成交模板，报告偏差并保留缺失字段。
5. `uv run pytest` 检查时间边界、交易单位、涨跌停阻断和计划成交汇总。
6. `uv run cehuan qlib-check ...`（安装 qlib extra 后）使用 Qlib StaticDataLoader 读取同一标准化数据。

详见 [架构说明](docs/architecture.md)、[数据口径](docs/data-contract.md) 和 [实验记录规范](docs/experiments.md)。
另见 [完整合成示例结果](docs/example-run.md)、[RD-Agent fin_quant 调查](docs/rdagent-integration.md) 和 [可选 Agent/行情插件核验清单](docs/optional-integrations.md)。
