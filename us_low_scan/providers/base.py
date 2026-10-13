"""数据源接口与 HTTP 小工具（仅用标准库）。"""

from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import date
from typing import Dict, List, Optional, Sequence

from ..models import Bar, Symbol


class DataProviderError(RuntimeError):
    """数据源不可用、额度耗尽或返回格式异常。"""


class RateLimiter:
    """简单的匀速限流器：保证两次请求之间的间隔不小于 1/rps 秒。"""

    def __init__(self, requests_per_second: float, burst: int = 1):
        self.min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self.burst = burst
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        # 多线程抓取时也保证整体速率不超过设定值
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last = time.monotonic()


def http_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, object]] = None,
    timeout: int = 30,
    max_retries: int = 3,
    limiter: Optional[RateLimiter] = None,
) -> object:
    """GET 一个 JSON 接口，带指数退避重试。

    只使用标准库 ``urllib``，因此不引入任何第三方依赖。
    """
    if params:
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(params)}"

    request_headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "us-low-scan/0.1 (+https://example.invalid)",
    }
    if headers:
        request_headers.update(headers)

    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        if limiter:
            limiter.wait()
        try:
            req = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # pragma: no cover - 仅用于错误提示
                pass
            last_error = DataProviderError(f"HTTP {exc.code} {url}: {body}")
            # 429 / 5xx 值得重试；4xx 其余直接抛出
            if exc.code not in (429, 500, 502, 503, 504):
                raise last_error from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = DataProviderError(f"请求失败 {url}: {exc}")
        time.sleep(min(2 ** attempt * 0.5, 8.0))
    raise DataProviderError(f"重试 {max_retries} 次仍失败：{last_error}")


def http_text(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, object]] = None,
    timeout: int = 30,
    max_retries: int = 3,
    limiter: Optional[RateLimiter] = None,
) -> str:
    """GET 一个纯文本（CSV 等）接口，带重试。"""
    if params:
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(params)}"
    request_headers = {"User-Agent": "us-low-scan/0.1"}
    if headers:
        request_headers.update(headers)

    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        if limiter:
            limiter.wait()
        try:
            req = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_error = DataProviderError(f"HTTP {exc.code} {url}")
            if exc.code not in (429, 500, 502, 503, 504):
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = DataProviderError(f"请求失败 {url}: {exc}")
        time.sleep(min(2 ** attempt * 0.5, 8.0))
    raise DataProviderError(f"重试 {max_retries} 次仍失败：{last_error}")


class MarketDataProvider(ABC):
    """行情数据源统一接口。"""

    name: str = "base"
    #: 数据覆盖说明，会直接写进报告，让使用者知道边界在哪里
    coverage_note: str = ""

    def __init__(self, settings):
        self.settings = settings
        cfg = settings.data_source
        self.limiter = RateLimiter(cfg.requests_per_second)
        self.timeout = cfg.request_timeout
        self.max_retries = cfg.max_retries

    # -- 必须实现 ---------------------------------------------------------
    @abstractmethod
    def list_symbols(self) -> List[Symbol]:
        """返回股票池（含交易所、证券类型、市值等，用于阶段 A 硬性过滤）。"""

    @abstractmethod
    def get_daily_bars(
        self, symbol: str, start: Optional[date] = None, end: Optional[date] = None
    ) -> List[Bar]:
        """返回按日期升序的日线 K 线。"""

    # -- 可选实现 ---------------------------------------------------------
    def get_benchmark_bars(
        self, symbol: str, start: Optional[date] = None, end: Optional[date] = None
    ) -> List[Bar]:
        return self.get_daily_bars(symbol, start=start, end=end)

    def get_bars_multi(
        self, symbols: Sequence[str], start: Optional[date] = None, end: Optional[date] = None
    ) -> Dict[str, List[Bar]]:
        """批量拉取。默认逐只调用；支持批量接口的数据源应覆盖此方法。"""
        out: Dict[str, List[Bar]] = {}
        for symbol in symbols:
            out[symbol] = self.get_daily_bars(symbol, start=start, end=end)
        return out

    def get_fundamentals(self, symbol: str) -> Optional[Dict[str, object]]:
        """基本面数据；默认没有（报告会标记"基本面覆盖不足"）。"""
        return None

    def close(self) -> None:  # pragma: no cover - 便于资源清理
        pass

    def __enter__(self) -> "MarketDataProvider":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
