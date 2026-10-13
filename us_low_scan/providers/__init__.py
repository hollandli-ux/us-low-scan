"""行情数据源适配器。

统一接口 ``MarketDataProvider``，通过配置里的 ``provider`` 选择实现：

* ``sample``       离线合成市场（零联网，用于跑通流程、演示与测试）
* ``csv``          读取本地 CSV 目录（任何供应商导出的数据都能用）
* ``alpaca``       Alpaca Market Data v2（需要 API key）
* ``alphavantage`` Alpha Vantage 日线（需要 API key）
* ``nasdaq``       Nasdaq 官方公开接口（免费、无需 API key，含市值/行业）

关键原则（来自产品构想第 5 节）：**先验证覆盖，再开始写程序**。
所有 provider 都必须如实报告 ``coverage_note``，把"数据覆盖到什么程度"
写进报告，绝不用"没数据"冒充"没问题"。
"""

from __future__ import annotations

from typing import Dict, Type

from .base import DataProviderError, MarketDataProvider, RateLimiter, http_json, http_text
from .sample import SampleProvider
from .csv_provider import CsvProvider
from .nasdaq import NasdaqProvider

_REGISTRY: Dict[str, Type[MarketDataProvider]] = {
    "sample": SampleProvider,
    "csv": CsvProvider,
    "nasdaq": NasdaqProvider,
}


def register(name: str, cls: Type[MarketDataProvider]) -> None:
    _REGISTRY[name] = cls


def get_provider(name: str, settings) -> MarketDataProvider:
    """按名字实例化 provider。alpaca / alphavantage 延迟导入，避免无谓的网络代码加载。"""
    key = (name or "").lower()
    if key == "alpaca":
        from .alpaca import AlpacaProvider

        return AlpacaProvider(settings)
    if key == "alphavantage":
        from .alphavantage import AlphaVantageProvider

        return AlphaVantageProvider(settings)
    if key not in _REGISTRY:
        raise DataProviderError(
            f"未知的数据源 {name!r}；可选：sample, csv, alpaca, alphavantage"
        )
    return _REGISTRY[key](settings)


__all__ = [
    "MarketDataProvider",
    "DataProviderError",
    "RateLimiter",
    "http_json",
    "http_text",
    "get_provider",
    "register",
    "SampleProvider",
    "CsvProvider",
    "NasdaqProvider",
]
