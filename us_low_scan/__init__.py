"""美股低位高流动性股票观察工具（US Low-Position High-Liquidity Scanner）.

零第三方依赖：核心仅使用 Python 标准库，可在系统自带的 Python 3.9+ 上直接运行。

模块划分
--------
config     读取 config/config.ini，做类型转换与环境变量展开
calendar_us 美股交易日历（NYSE 假日、提前收市、America/New_York 时区）
db         SQLite 存储层（symbols / daily_bars / daily_features / ...）
indicators 技术指标与形态特征（全部为自实现、经过单元测试的公式）
providers  行情数据源适配器（sample / csv / alpaca / alphavantage）与 SEC EDGAR
universe   阶段 A：股票池与硬性过滤
features   阶段 B/C/D：位置特征、企稳证据、风险标签
scoring    100 分制评分（不含上涨概率的含义）
report     Markdown / CSV / JSON 报告输出
outcomes   候选股入选后 5/10/20 个交易日表现跟踪
backtest   无未来数据泄漏的历史回放与基准对比
pipeline   每日流程编排
cli        命令行入口
"""

__version__ = "0.1.0"
