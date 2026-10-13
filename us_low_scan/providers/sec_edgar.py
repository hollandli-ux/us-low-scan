"""SEC EDGAR 事件与风险数据（可选，默认关闭）。

能力
----
1. ``company_tickers.json`` → ticker 与 CIK 的映射（EDGAR 官方提供的对照表）。
2. ``submissions/CIK##########.json`` → 近期申报清单（8-K / 10-Q / 10-K /
   S-1 / S-3 / 424B 等），用于生成风险标签：
     * 稀释风险：S-1 / S-3 / 424B / S-1/A 等发行类文件
     * 退市风险：Form 25 / Form 15
     * 定期报告缺失：长期没有 10-Q / 10-K
3. 财报日期：EDGAR **不提供未来的财报日历**。本工具的做法是从历史上
   8-K（item 2.02 "Results of Operations"）的实际日期推算下次财报的大致
   日期，并明确标注为"推算值"。推不出来就写"财报日期未核实"。

合规提醒：SEC 要求 API 使用者声明 User-Agent（含联系方式），并控制在
每秒 10 次以内。本工具用 ``data_source.sec_user_agent`` 配置。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .base import DataProviderError, RateLimiter, http_json, http_text

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# 申报类型 -> 风险含义
DILUTION_FORMS = {"S-1", "S-1/A", "S-1MEF", "S-3", "S-3/A", "S-3ASR", "424A", "424B1",
                  "424B2", "424B3", "424B4", "424B5", "F-1", "F-3", "F-3ASR"}
DELISTING_FORMS = {"25", "25-NSE", "15-12B", "15-12G", "15-15D"}
PERIODIC_FORMS = {"10-Q", "10-K", "20-F", "40-F"}


class SecEdgarClient:
    """EDGAR 客户端，带本地文件缓存（避免重复请求、便于离线复算）。"""

    def __init__(self, user_agent: str, cache_dir: Optional[Path] = None,
                 requests_per_second: float = 5.0, timeout: int = 30, max_retries: int = 3):
        if not user_agent or "@" not in user_agent:
            raise DataProviderError(
                "SEC 要求 User-Agent 包含联系方式，请设置 data_source.sec_user_agent"
                "（例如 \"your-name research tool (contact: you@example.com)\"）"
            )
        self.user_agent = user_agent
        self.headers = {
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Host": "data.sec.gov",
        }
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        # SEC 官方建议不超过 10 rps
        self.limiter = RateLimiter(min(requests_per_second, 9.0))
        self.timeout = timeout
        self.max_retries = max_retries
        self._ticker_map: Optional[Dict[str, int]] = None

    # ------------------------------------------------------------------
    def _cache_path(self, name: str) -> Optional[Path]:
        return self.cache_dir / name if self.cache_dir else None

    def ticker_to_cik(self, ticker: str) -> Optional[int]:
        if self._ticker_map is None:
            cached = self._cache_path("company_tickers.json")
            payload = None
            if cached and cached.exists() and self._is_fresh(cached, days=7):
                try:
                    payload = json.loads(cached.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    payload = None
            if payload is None:
                payload = http_json(
                    TICKER_MAP_URL,
                    headers={"User-Agent": self.user_agent},
                    timeout=self.timeout,
                    max_retries=self.max_retries,
                    limiter=self.limiter,
                )
                if cached:
                    cached.write_text(json.dumps(payload), encoding="utf-8")
            mapping: Dict[str, int] = {}
            for item in payload.values() if isinstance(payload, dict) else []:
                symbol = (item.get("ticker") or "").upper()
                if symbol:
                    mapping[symbol] = int(item["cik_str"])
            self._ticker_map = mapping
        return self._ticker_map.get(ticker.upper())

    def submissions(self, ticker: str) -> Optional[dict]:
        cik = self.ticker_to_cik(ticker)
        if cik is None:
            return None
        cached = self._cache_path(f"CIK{cik:010d}.json")
        if cached and cached.exists() and self._is_fresh(cached, days=1):
            try:
                return json.loads(cached.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        payload = http_json(
            SUBMISSIONS_URL.format(cik=cik),
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout,
            max_retries=self.max_retries,
            limiter=self.limiter,
        )
        if cached:
            cached.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    @staticmethod
    def _is_fresh(path: Path, days: int) -> bool:
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        return age < timedelta(days=days)

    # ------------------------------------------------------------------
    def recent_filings(self, ticker: str, since: Optional[date] = None) -> List[Dict[str, object]]:
        payload = self.submissions(ticker)
        if not payload:
            return []
        recent = (payload.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accessions = recent.get("accessionNumber") or []
        descriptions = recent.get("primaryDocDescription") or []
        items = recent.get("items") or []

        out: List[Dict[str, object]] = []
        for i, form in enumerate(forms):
            if i >= len(dates):
                break
            try:
                filed = date.fromisoformat(dates[i])
            except (TypeError, ValueError):
                continue
            if since and filed < since:
                continue
            accession = accessions[i] if i < len(accessions) else ""
            url = ""
            if accession:
                cik = payload.get("cik") or ""
                try:
                    url = (
                        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                        f"{accession.replace('-', '')}/"
                    )
                except (TypeError, ValueError):
                    url = ""
            out.append(
                {
                    "symbol": ticker.upper(),
                    "event_date": filed.isoformat(),
                    "event_type": f"SEC:{form}",
                    "title": form + (
                        f" — {descriptions[i]}" if i < len(descriptions) and descriptions[i] else ""
                    ),
                    "source": "SEC EDGAR",
                    "url": url,
                    "detail": (items[i] if i < len(items) else "") or "",
                }
            )
        out.sort(key=lambda r: str(r["event_date"]), reverse=True)
        return out

    def earnings_dates(self, ticker: str, limit: int = 8) -> List[date]:
        """从 8-K item 2.02（业绩发布）反推历史财报日期。"""
        out: List[date] = []
        for filing in self.recent_filings(ticker):
            if str(filing["event_type"]) != "SEC:8-K":
                continue
            items = str(filing.get("detail") or "")
            if "2.02" not in items:
                continue
            out.append(date.fromisoformat(str(filing["event_date"])))
            if len(out) >= limit:
                break
        out.sort(reverse=True)
        return out

    def estimate_next_earnings(self, ticker: str, today: Optional[date] = None) -> Optional[date]:
        """按季度节奏推算下次财报日期（推算值，不是官方日历）。"""
        today = today or date.today()
        dates = self.earnings_dates(ticker, limit=6)
        if len(dates) < 3:
            return None
        gaps = [
            (dates[i] - dates[i + 1]).days
            for i in range(min(3, len(dates) - 1))
        ]
        cadence = int(sum(gaps) / len(gaps)) if gaps else 91
        if not (60 <= cadence <= 130):
            return None
        candidate = dates[0] + timedelta(days=cadence)
        # 推算日已过太久则顺延一个季度
        guard = 0
        while candidate < today - timedelta(days=15) and guard < 4:
            candidate += timedelta(days=cadence)
            guard += 1
        return candidate


class NullEventsClient:
    """不做任何事件检查时的占位实现：一切标注为"未核实"。"""

    def recent_filings(self, ticker: str, since: Optional[date] = None):
        return []

    def earnings_dates(self, ticker: str, limit: int = 8):
        return []

    def estimate_next_earnings(self, ticker: str, today: Optional[date] = None):
        return None
