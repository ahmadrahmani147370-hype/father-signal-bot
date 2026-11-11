"""Live market scanner: coin universe + closed-candle signal detection."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from .binance import INTERVAL_MS, BinanceFuturesPublic, GeoBlockedError
from .db import Database, now_ms
from .strategy import build_events, pattern_adjustments, reasons_for, trend_label
from .telegram_alerts import Telegram, format_signal

log = logging.getLogger("father.scanner")
ANALYSIS_BARS = 300


def check_candles(df: pd.DataFrame, interval: str, min_rows: int, now: int) -> str | None:
    """Return None if the candle history is valid, else a reason string."""
    if df is None or len(df) < min_rows:
        return f"only {0 if df is None else len(df)} {interval} candles (<{min_rows})"
    step = INTERVAL_MS[interval]
    tail = df.tail(min_rows)
    if (tail["open_time"].diff().dropna() != step).any():
        return f"gap in {interval} candle history"
    expected_last = (now // step) * step - step
    if int(tail["open_time"].iloc[-1]) < expected_last - step:
        return f"stale {interval} data (last candle not recent)"
    if (tail["close_time"] >= now).any():
        return f"unclosed {interval} candle present"
    if (tail["volume"] <= 0).sum() > min_rows * 0.2:
        return f"too many zero-volume {interval} candles"
    return None


class Scanner:
    def __init__(self, cfg, db: Database, client: BinanceFuturesPublic, tg: Telegram):
        self.cfg, self.db, self.client, self.tg = cfg, db, client, tg
        self.symbols: list[str] = []
        self.universe_ts = 0.0
        self.enabled = True
        self.last_scan_ts: float | None = None
        self.last_scan_info: dict = {}
        self.last_error: str | None = None
        self.scan_lock = threading.Lock()
        self._adj: dict = {}
        self._adj_ts = 0.0

    # --------------------------------------------------------------- universe
    def refresh_universe(self, force: bool = False) -> list[str]:
        if not force and self.symbols and time.time() - self.universe_ts < self.cfg.universe_refresh_min * 60:
            return self.symbols
        info = self.client.exchange_info()
        valid = {
            s["symbol"] for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
        }
        tick = {t["symbol"]: t for t in self.client.ticker_24hr() if t["symbol"] in valid}
        book = {b["symbol"]: b for b in self.client.book_ticker() if b["symbol"] in valid}
        rows = []
        for sym, t in tick.items():
            b = book.get(sym)
            if not b:
                continue
            bid, ask = float(b["bidPrice"]), float(b["askPrice"])
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            spread_bps = (ask - bid) / ((ask + bid) / 2) * 1e4
            qv = float(t["quoteVolume"])
            if qv < self.cfg.min_quote_volume or spread_bps > self.cfg.max_spread_bps:
                continue
            if int(t.get("count", 0)) < 10_000:  # thin markets
                continue
            rows.append({"symbol": sym, "quote_volume": qv, "spread_bps": round(spread_bps, 3),
                         "last_price": float(t["lastPrice"]), "status": "TRADING"})
        rows.sort(key=lambda r: r["quote_volume"], reverse=True)
        rows = rows[: self.cfg.universe_size]
        self.db.save_universe(rows)
        self.symbols = [r["symbol"] for r in rows]
        self.universe_ts = time.time()
        pruned = self.db.prune_candles(self.cfg.retention_days)
        log.info("Universe refreshed: %d coins (of %d valid perps); pruned %d old candles",
                 len(self.symbols), len(valid), pruned)
        return self.symbols

    # ---------------------------------------------------------------- candles
    def _sync(self, symbol: str, interval: str, now: int) -> None:
        step = INTERVAL_MS[interval]
        last = self.db.last_open_time(symbol, interval)
        if last is None or (now - last) / step > ANALYSIS_BARS:
            limit = ANALYSIS_BARS + 2
        else:
            limit = int((now - last) / step) + 2
            if limit <= 1:
                return
        df = self.client.klines(symbol, interval, limit=min(limit, 1000))
        self.db.upsert_candles(symbol, interval, df)

    def _fetch(self, symbol: str, need_5m: bool, now: int) -> tuple[str, str | None]:
        try:
            self._sync(symbol, "1m", now)
            step5 = INTERVAL_MS["5m"]
            last5 = self.db.last_open_time(symbol, "5m")
            # fetch 5m only when a newer 5m candle has closed since our last copy
            if need_5m or last5 is None or last5 < (now // step5) * step5 - step5:
                self._sync(symbol, "5m", now)
            return symbol, None
        except GeoBlockedError:
            raise
        except Exception as e:  # keep scanning the other coins
            return symbol, f"{type(e).__name__}: {e}"

    def _adjustments(self) -> dict:
        if not self.cfg.use_bt_weights:
            return {}
        if time.time() - self._adj_ts > 600:
            raw = pattern_adjustments(self.db.latest_performance(), self.cfg.min_oos_trades)
            # live signals are 1m entries with 5m confirmation -> use the combined-strategy results
            self._adj = {("1m", p): a for (tf, p), a in raw.items() if tf == "1m+5m"}
            self._adj_ts = time.time()
        return self._adj

    # ------------------------------------------------------------------- scan
    def scan_once(self) -> dict:
        with self.scan_lock:
            return self._scan_once()

    def _scan_once(self) -> dict:
        t0 = time.time()
        self.client.sync_time()
        symbols = self.refresh_universe()
        now = self.client.now_ms()
        # a new 5m candle closed in the last ~minute -> refresh 5m data
        need_5m = False
        errors = 0
        with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as pool:
            futs = [pool.submit(self._fetch, s, need_5m, now) for s in symbols]
            for f in as_completed(futs):
                sym, err = f.result()
                if err:
                    errors += 1
                    self.db.log_error(sym, "fetch", err)
        adj = self._adjustments()
        alerts, detections, skipped = [], 0, 0
        for sym in symbols:
            try:
                d1 = self.db.load_candles(sym, "1m", limit=ANALYSIS_BARS)
                d5 = self.db.load_candles(sym, "5m", limit=ANALYSIS_BARS)
                # evaluate everything as of the scan start (a slow scan may cross a candle close)
                d1 = d1[d1["close_time"] < now].reset_index(drop=True)
                d5 = d5[d5["close_time"] < now].reset_index(drop=True)
                bad = check_candles(d1, "1m", self.cfg.min_history, now) or \
                    check_candles(d5, "5m", min(self.cfg.min_history, 120), now)
                if bad:
                    skipped += 1
                    self.db.log_error(sym, "validate", bad)
                    continue
                res = self.evaluate_symbol(sym, d1, d5, adj)
                detections += res["detections"]
                alerts += res["alerts"]
            except Exception as e:
                errors += 1
                self.db.log_error(sym, "evaluate", f"{type(e).__name__}: {e}")
                log.exception("evaluate %s failed", sym)
        alerts = self.dispatch(alerts)
        self.last_scan_ts = time.time()
        self.last_scan_info = {"coins": len(symbols), "detections": detections, "alerts": len(alerts),
                               "skipped": skipped, "errors": errors, "seconds": round(time.time() - t0, 1),
                               "weight": self.client.used_weight}
        log.info("Scan done: %s", self.last_scan_info)
        return self.last_scan_info

    def evaluate_symbol(self, sym: str, d1: pd.DataFrame, d5: pd.DataFrame, adj: dict) -> dict:
        ev = build_events(d1, self.cfg, htf=d5, tf="1m", adjustments=adj)
        if ev.empty:
            return {"detections": 0, "alerts": []}
        last_idx = len(d1) - 1
        ev = ev[ev["idx"] == last_idx]  # only the most recent CLOSED candle
        if ev.empty:
            return {"detections": 0, "alerts": []}
        self.db.save_patterns([
            (now_ms(), sym, "1m", int(r.open_time), r.pattern, r.direction, float(r.close),
             None if pd.isna(r.vol_ratio) else float(r.vol_ratio), None if pd.isna(r.rsi) else float(r.rsi))
            for r in ev.itertuples(index=False)])
        cands = []
        best = ev.sort_values("score", ascending=False).drop_duplicates("direction")
        for _, row in best.iterrows():
            if not row["qualified"]:
                continue
            key = f"{sym}|1m|{row['direction']}|{int(row['open_time'])}"
            if self.db.signal_exists(key):
                continue
            if self.db.in_cooldown(sym, row["direction"], self.cfg.alert_cooldown_min):
                self.db.log_failed_signal(sym, row["pattern"], row["direction"], float(row["score"]),
                                          "suppressed: cooldown (duplicate)")
                continue
            sig = {
                "key": key, "symbol": sym, "direction": row["direction"], "entry_tf": "1m",
                "confirm_tf": row["confirm_tf"], "pattern": row["pattern"],
                "candle_open_time": int(row["open_time"]), "price": float(row["close"]),
                "score": float(row["score"]), "score_class": row["score_class"],
                "volume_confirmed": bool(row["volume_confirmed"]), "trend": trend_label(row["c_trend"]),
                "rsi": float(row["rsi"]), "reasons": reasons_for(row),
            }
            sig["message"] = format_signal(sig)
            cands.append(sig)
        return {"detections": len(ev), "alerts": cands}

    def dispatch(self, cands: list[dict]) -> list[dict]:
        """Send the best candidates (capped per scan); the rest are logged as suppressed."""
        cands = sorted(cands, key=lambda s: s["score"], reverse=True)
        sent = []
        for i, sig in enumerate(cands):
            if i >= self.cfg.max_alerts_per_scan:
                self.db.log_failed_signal(sig["symbol"], sig["pattern"], sig["direction"], sig["score"],
                                          "suppressed: max alerts per scan", sig)
                continue
            if not self.db.save_signal(sig):
                continue
            ok = self.tg.send(sig["message"])
            self.db.mark_sent(sig["key"], ok or not self.tg.enabled)
            if self.tg.enabled and not ok:
                self.db.log_failed_signal(sig["symbol"], sig["pattern"], sig["direction"], sig["score"],
                                          "telegram delivery failed", sig)
            sent.append(sig)
        return sent

    # ------------------------------------------------------------------- loop
    def run_forever(self, stop: threading.Event) -> None:
        log.info("Scanner loop started (polls once per closed 1m candle)")
        backoff = 5
        while not stop.is_set():
            # sleep until the next 1m candle close + a small delay
            now = self.client.now_ms()
            wait = (60_000 - now % 60_000) / 1000 + self.cfg.close_delay_sec
            if stop.wait(wait):
                break
            if not self.enabled:
                continue
            try:
                self.scan_once()
                self.last_error = None
                backoff = 5
            except GeoBlockedError as e:
                self.last_error = str(e)
                self.db.log_error(None, "network", str(e))
                log.error("%s", e)
                stop.wait(300)
            except Exception as e:  # network down etc. -> recover automatically
                self.last_error = f"{type(e).__name__}: {e}"
                self.db.log_error(None, "scan", self.last_error)
                log.exception("scan failed; retrying in %ss", backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, 300)
