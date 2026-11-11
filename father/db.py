"""SQLite storage. All history is kept for future analysis (candles are pruned by retention only)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL, interval TEXT NOT NULL, open_time INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    close_time INTEGER, quote_volume REAL, trades INTEGER,
    PRIMARY KEY (symbol, interval, open_time)
);
CREATE TABLE IF NOT EXISTS patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, detected_at INTEGER, symbol TEXT, interval TEXT,
    candle_open_time INTEGER, pattern TEXT, direction TEXT, close REAL, volume_ratio REAL, rsi REAL,
    UNIQUE (symbol, interval, candle_open_time, pattern)
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER, signal_key TEXT UNIQUE,
    symbol TEXT, direction TEXT, entry_tf TEXT, confirm_tf TEXT, pattern TEXT,
    candle_open_time INTEGER, price REAL, score REAL, score_class TEXT,
    volume_confirmed INTEGER, trend TEXT, rsi REAL, reasons TEXT, sent INTEGER DEFAULT 0, message TEXT
);
CREATE TABLE IF NOT EXISTS backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, created_at INTEGER, params TEXT,
    timeframe TEXT, dimension TEXT, grp TEXT, split TEXT,
    total INTEGER, wins INTEGER, losses INTEGER, win_rate REAL, profit_factor REAL,
    net_return REAL, max_drawdown REAL, avg_return REAL, expectancy REAL,
    avg_win REAL, avg_loss REAL, long_trades INTEGER, long_net REAL, short_trades INTEGER, short_net REAL,
    sample_status TEXT
);
CREATE TABLE IF NOT EXISTS coin_universe (
    refreshed_at INTEGER, rank INTEGER, symbol TEXT, quote_volume REAL, spread_bps REAL,
    last_price REAL, status TEXT, PRIMARY KEY (refreshed_at, symbol)
);
CREATE TABLE IF NOT EXISTS performance_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, updated_at INTEGER, run_id TEXT, timeframe TEXT,
    pattern TEXT, oos_trades INTEGER, oos_win_rate REAL, oos_profit_factor REAL,
    oos_avg_return REAL, oos_net_return REAL, train_pf REAL, val_pf REAL, verdict TEXT
);
CREATE TABLE IF NOT EXISTS failed_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER, symbol TEXT, pattern TEXT,
    direction TEXT, score REAL, reason TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS scanner_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER, symbol TEXT, stage TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_signals_sym ON signals(symbol, direction, created_at);
CREATE INDEX IF NOT EXISTS ix_bt_run ON backtest_results(run_id);
CREATE INDEX IF NOT EXISTS ix_err_time ON scanner_errors(created_at);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    """Single shared connection guarded by a lock (safe for the scanner + Telegram threads)."""

    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(signals)")}
        for c in ("atr", "sl", "tp"):  # added in v1.1 (Mini App reference levels)
            if c not in cols:
                self.conn.execute(f"ALTER TABLE signals ADD COLUMN {c} REAL")
        self.conn.commit()

    # ---------------------------------------------------------------- helpers
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def query_df(self, sql: str, params: Iterable[Any] = ()) -> pd.DataFrame:
        with self._lock:
            return pd.read_sql_query(sql, self.conn, params=tuple(params))

    # ---------------------------------------------------------------- candles
    def upsert_candles(self, symbol: str, interval: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        rows = [
            (symbol, interval, int(r.open_time), float(r.open), float(r.high), float(r.low), float(r.close),
             float(r.volume), int(r.close_time), float(r.quote_volume), int(r.trades))
            for r in df.itertuples(index=False)
        ]
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.conn.commit()
        return len(rows)

    def last_open_time(self, symbol: str, interval: str) -> int | None:
        r = self.query("SELECT MAX(open_time) FROM candles WHERE symbol=? AND interval=?", (symbol, interval))
        return r[0][0] if r and r[0][0] is not None else None

    def first_open_time(self, symbol: str, interval: str) -> int | None:
        r = self.query("SELECT MIN(open_time) FROM candles WHERE symbol=? AND interval=?", (symbol, interval))
        return r[0][0] if r and r[0][0] is not None else None

    def load_candles(self, symbol: str, interval: str, start_ms: int | None = None,
                     end_ms: int | None = None, limit: int | None = None) -> pd.DataFrame:
        sql = ("SELECT open_time, open, high, low, close, volume, close_time, quote_volume, trades "
               "FROM candles WHERE symbol=? AND interval=?")
        params: list[Any] = [symbol, interval]
        if start_ms is not None:
            sql += " AND open_time>=?"
            params.append(int(start_ms))
        if end_ms is not None:
            sql += " AND open_time<=?"
            params.append(int(end_ms))
        if limit:
            sql = f"SELECT * FROM ({sql} ORDER BY open_time DESC LIMIT {int(limit)}) ORDER BY open_time"
        else:
            sql += " ORDER BY open_time"
        return self.query_df(sql, params)

    def prune_candles(self, retention_days: int) -> int:
        cutoff = now_ms() - retention_days * 86_400_000
        return self.execute("DELETE FROM candles WHERE open_time < ?", (cutoff,)).rowcount

    # --------------------------------------------------------------- universe
    def save_universe(self, rows: list[dict]) -> int:
        ts = now_ms()
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO coin_universe VALUES (?,?,?,?,?,?,?)",
                [(ts, i + 1, r["symbol"], r["quote_volume"], r["spread_bps"], r["last_price"], r.get("status", "TRADING"))
                 for i, r in enumerate(rows)])
            self.conn.commit()
        return ts

    def latest_universe(self) -> pd.DataFrame:
        return self.query_df(
            "SELECT * FROM coin_universe WHERE refreshed_at=(SELECT MAX(refreshed_at) FROM coin_universe) ORDER BY rank")

    # ---------------------------------------------------------------- patterns
    def save_patterns(self, rows: list[tuple]) -> None:
        if not rows:
            return
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO patterns (detected_at, symbol, interval, candle_open_time, pattern, direction,"
                " close, volume_ratio, rsi) VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self.conn.commit()

    # ----------------------------------------------------------------- signals
    def signal_exists(self, key: str) -> bool:
        return bool(self.query("SELECT 1 FROM signals WHERE signal_key=?", (key,)))

    def in_cooldown(self, symbol: str, direction: str, minutes: float) -> bool:
        cutoff = now_ms() - int(minutes * 60_000)
        return bool(self.query(
            "SELECT 1 FROM signals WHERE symbol=? AND direction=? AND sent=1 AND created_at>=? LIMIT 1",
            (symbol, direction, cutoff)))

    def save_signal(self, s: dict) -> bool:
        try:
            self.execute(
                "INSERT INTO signals (created_at, signal_key, symbol, direction, entry_tf, confirm_tf, pattern,"
                " candle_open_time, price, score, score_class, volume_confirmed, trend, rsi, reasons, sent, message,"
                " atr, sl, tp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now_ms(), s["key"], s["symbol"], s["direction"], s["entry_tf"], s["confirm_tf"], s["pattern"],
                 s["candle_open_time"], s["price"], s["score"], s["score_class"], int(s["volume_confirmed"]),
                 s["trend"], s["rsi"], json.dumps(s["reasons"]), int(s.get("sent", 0)), s.get("message", ""),
                 s.get("atr"), s.get("sl"), s.get("tp")))
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_sent(self, key: str, sent: bool) -> None:
        self.execute("UPDATE signals SET sent=? WHERE signal_key=?", (int(sent), key))

    def recent_signals(self, n: int = 10) -> pd.DataFrame:
        return self.query_df("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (int(n),))

    def log_failed_signal(self, symbol: str, pattern: str, direction: str, score: float, reason: str,
                          payload: dict | None = None) -> None:
        self.execute("INSERT INTO failed_signals (created_at, symbol, pattern, direction, score, reason, payload)"
                     " VALUES (?,?,?,?,?,?,?)",
                     (now_ms(), symbol, pattern, direction, score, reason, json.dumps(payload or {}, default=str)))

    def log_error(self, symbol: str | None, stage: str, error: str) -> None:
        self.execute("INSERT INTO scanner_errors (created_at, symbol, stage, error) VALUES (?,?,?,?)",
                     (now_ms(), symbol, stage, str(error)[:1000]))

    def recent_errors(self, n: int = 10) -> pd.DataFrame:
        return self.query_df("SELECT * FROM scanner_errors ORDER BY id DESC LIMIT ?", (int(n),))

    def count_errors_since(self, since_ms: int) -> int:
        return self.query("SELECT COUNT(*) FROM scanner_errors WHERE created_at>=?", (since_ms,))[0][0]

    # --------------------------------------------------------------- backtests
    def save_backtest_rows(self, rows: list[dict]) -> None:
        if not rows:
            return
        cols = ["run_id", "created_at", "params", "timeframe", "dimension", "grp", "split", "total", "wins",
                "losses", "win_rate", "profit_factor", "net_return", "max_drawdown", "avg_return", "expectancy",
                "avg_win", "avg_loss", "long_trades", "long_net", "short_trades", "short_net", "sample_status"]
        with self._lock:
            self.conn.executemany(
                f"INSERT INTO backtest_results ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [tuple(r.get(c) for c in cols) for r in rows])
            self.conn.commit()

    def save_performance(self, rows: list[dict]) -> None:
        if not rows:
            return
        cols = ["updated_at", "run_id", "timeframe", "pattern", "oos_trades", "oos_win_rate", "oos_profit_factor",
                "oos_avg_return", "oos_net_return", "train_pf", "val_pf", "verdict"]
        with self._lock:
            self.conn.executemany(
                f"INSERT INTO performance_stats ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [tuple(r.get(c) for c in cols) for r in rows])
            self.conn.commit()

    def latest_performance(self) -> pd.DataFrame:
        return self.query_df(
            "SELECT * FROM performance_stats WHERE run_id=(SELECT run_id FROM performance_stats ORDER BY id DESC LIMIT 1)"
            " ORDER BY timeframe, oos_profit_factor DESC")

    def latest_backtest(self) -> pd.DataFrame:
        return self.query_df(
            "SELECT * FROM backtest_results WHERE run_id=(SELECT run_id FROM backtest_results ORDER BY id DESC LIMIT 1)")

    # ---------------------------------------------------------------- settings
    def get_settings(self) -> dict[str, str]:
        return {k: v for k, v in self.query("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(value)))

    def close(self) -> None:
        with self._lock:
            self.conn.close()
