"""Binance USD-M Futures *public* REST client (market data only).

- No API key, no signed endpoints, no order placement - by design.
- Thread-safe request pacing (minimum interval between calls).
- Weight-aware: reads X-MBX-USED-WEIGHT-1M and pauses before the limit.
- Retries with exponential backoff on network errors, 5xx, 429 and 418.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

import pandas as pd
import requests

log = logging.getLogger("father.binance")

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


class BinanceError(Exception):
    pass


class GeoBlockedError(BinanceError):
    """HTTP 451/403 - the endpoint is not available from this network/region."""


def kline_weight(limit: int) -> int:
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


class BinanceFuturesPublic:
    def __init__(self, base_url: str = "https://fapi.binance.com", min_interval: float = 0.12,
                 max_weight_per_min: int = 1200, timeout: float = 10, max_retries: int = 5):
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.max_weight = max_weight_per_min
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "father-signal-bot/1.0 (analysis-only)"})
        self._lock = threading.Lock()
        self._last_call = 0.0
        self.used_weight = 0
        self._weight_ts = 0.0
        self.time_offset_ms = 0  # server_time - local_time

    # ------------------------------------------------------------------ core
    def _pace(self, weight: int) -> None:
        with self._lock:
            now = time.time()
            # weight window resets every minute on Binance's side
            if now - self._weight_ts > 60:
                self.used_weight = 0
            if self.used_weight + weight > self.max_weight:
                wait = 61 - (now % 60)
                log.warning("Request weight %s near limit %s - pausing %.0fs", self.used_weight, self.max_weight, wait)
                time.sleep(wait)
                self.used_weight = 0
            gap = self.min_interval - (time.time() - self._last_call)
            if gap > 0:
                time.sleep(gap)
            self._last_call = time.time()

    def _get(self, path: str, params: dict | None = None, weight: int = 1) -> Any:
        url = f"{self.base_url}{path}"
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._pace(weight)
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                log.warning("Network error on %s (attempt %d/%d): %s", path, attempt, self.max_retries, e)
                time.sleep(delay + random.random())
                delay = min(delay * 2, 60)
                continue
            w = r.headers.get("X-MBX-USED-WEIGHT-1M") or r.headers.get("x-mbx-used-weight-1m")
            if w:
                with self._lock:
                    self.used_weight = int(w)
                    self._weight_ts = time.time()
            if r.status_code == 200:
                return r.json()
            if r.status_code in (451, 403):
                raise GeoBlockedError(f"HTTP {r.status_code} from {url}: endpoint blocked from this location. "
                                      f"Set BINANCE_BASE_URL in .env or use another network.")
            if r.status_code in (418, 429):
                wait = float(r.headers.get("Retry-After", 60))
                log.warning("Rate limited (HTTP %s). Sleeping %.0fs", r.status_code, wait)
                time.sleep(wait)
                continue
            if 500 <= r.status_code < 600:
                last_exc = BinanceError(f"HTTP {r.status_code}: {r.text[:200]}")
                log.warning("Server error on %s (attempt %d): %s", path, attempt, r.status_code)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise BinanceError(f"HTTP {r.status_code} on {path}: {r.text[:300]}")
        raise BinanceError(f"Giving up on {path} after {self.max_retries} attempts: {last_exc}")

    # ------------------------------------------------------------- endpoints
    def ping(self) -> bool:
        self._get("/fapi/v1/ping")
        return True

    def server_time(self) -> int:
        return int(self._get("/fapi/v1/time")["serverTime"])

    def sync_time(self) -> int:
        local = int(time.time() * 1000)
        st = self.server_time()
        self.time_offset_ms = st - local
        return self.time_offset_ms

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    def exchange_info(self) -> dict:
        return self._get("/fapi/v1/exchangeInfo", weight=1)

    def ticker_24hr(self) -> list[dict]:
        return self._get("/fapi/v1/ticker/24hr", weight=40)

    def book_ticker(self) -> list[dict]:
        return self._get("/fapi/v1/ticker/bookTicker", weight=5)

    def klines_raw(self, symbol: str, interval: str, limit: int = 500,
                   start_time: int | None = None, end_time: int | None = None) -> list:
        limit = max(1, min(int(limit), 1500))
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        return self._get("/fapi/v1/klines", params=params, weight=kline_weight(limit))

    def klines(self, symbol: str, interval: str, limit: int = 500,
               start_time: int | None = None, end_time: int | None = None,
               closed_only: bool = True) -> pd.DataFrame:
        df = klines_to_df(self.klines_raw(symbol, interval, limit, start_time, end_time))
        if closed_only:
            df = drop_unclosed(df, self.now_ms())
        return df

    def klines_range(self, symbol: str, interval: str, start_ms: int, end_ms: int,
                     closed_only: bool = True) -> pd.DataFrame:
        """Paginated download of [start_ms, end_ms] using 1000-candle pages (weight 5 each)."""
        step = INTERVAL_MS[interval]
        frames = []
        cur = int(start_ms)
        while cur < end_ms:
            raw = self.klines_raw(symbol, interval, 1000, start_time=cur, end_time=end_ms)
            if not raw:
                break
            frames.append(klines_to_df(raw))
            last_open = int(raw[-1][0])
            nxt = last_open + step
            if nxt <= cur or len(raw) < 1000:
                break
            cur = nxt
        if not frames:
            return klines_to_df([])
        df = pd.concat(frames, ignore_index=True).drop_duplicates("open_time").sort_values("open_time")
        df = df.reset_index(drop=True)
        if closed_only:
            df = drop_unclosed(df, self.now_ms())
        return df


def klines_to_df(raw: list) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume",
                                     "close_time", "quote_volume", "trades"])
    df = pd.DataFrame(raw, columns=KLINE_COLS[: len(raw[0])])
    for c in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[c] = df[c].astype(float)
    for c in ("open_time", "close_time", "trades"):
        df[c] = df[c].astype("int64")
    return df[["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades"]]


def drop_unclosed(df: pd.DataFrame, now_ms: int) -> pd.DataFrame:
    """Remove the in-progress candle: a candle is closed only when close_time < now."""
    if df.empty:
        return df
    return df[df["close_time"] < now_ms].reset_index(drop=True)
