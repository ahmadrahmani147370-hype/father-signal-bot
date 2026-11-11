"""Telegram Mini App support - no server needed.

The bot puts a compact snapshot of its data into the Mini App URL (?d=<base64url JSON>).
The static page (GitHub Pages) decodes it. Actions come back with Telegram.WebApp.sendData
as a `web_app_data` message, which the bot validates against a whitelist.
"""
from __future__ import annotations

import base64
import json
import math
import time
from urllib.parse import urlencode

import pandas as pd

from .config import TUNABLES
from .patterns import PATTERNS

PATTERN_LIST = list(PATTERNS)          # index order is shared with webapp/app.js
VERDICT_CODES = {"positive after costs (OOS)": "P", "negative after costs": "N",
                 "insufficient sample": "I", "unstable (OOS positive, train/val not)": "U"}
MAX_URL_LEN = 2000                      # keep well under client limits
SETTINGS_IN_APP = ["min_score", "max_spread_bps", "min_volume", "volume_spike", "cooldown_min",
                   "universe_size", "require_volume", "max_alerts"]
ACTIONS = {"scan_on", "scan_off", "scan_once", "backtest", "set", "refresh", "status", "report"}


def _num(x, sig: int = 6):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return float(f"{f:.{sig}g}")


def b64url(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64url_decode(s: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode())


def build_payload(cfg, db, scanner, bt_running: bool = False, n_signals: int = 15) -> dict:
    s = scanner
    sig = db.recent_signals(n_signals)
    sigs = []
    for r in sig.itertuples():
        pat = PATTERN_LIST.index(r.pattern) if r.pattern in PATTERN_LIST else -1
        sigs.append([r.symbol, "L" if r.direction == "LONG" else "S", pat, int(round(r.score)),
                     _num(r.price), _num(getattr(r, "sl", None)), _num(getattr(r, "tp", None)),
                     int(r.created_at // 1000)])
    bt = {}
    rows = db.latest_backtest()
    if not rows.empty:
        t = rows[(rows["split"] == "test") & (rows["dimension"].isin(["overall", "strategy"]))]
        bt["id"] = str(rows["run_id"].iloc[0])
        bt["ts"] = int(rows["created_at"].iloc[0] // 1000)
        bt["pm"] = str(rows["params"].iloc[0])[:160]
        bt["o"] = [[r.timeframe, r.grp[:24], int(r.total), _num(r.win_rate, 4), _num(min(r.profit_factor, 999), 4),
                    _num(r.net_return, 5)] for r in t.itertuples() if r.grp != "not qualified"]
        bt["o"].sort(key=lambda x: (0 if x[1].startswith("qualified") else 1, x[0]))
    perf = db.latest_performance()
    if not perf.empty:
        bt["p"] = [[r.timeframe, PATTERN_LIST.index(r.pattern) if r.pattern in PATTERN_LIST else -1,
                    int(r.oos_trades), _num(min(r.oos_profit_factor or 0, 999), 4), _num(r.oos_avg_return, 4),
                    VERDICT_CODES.get(r.verdict, "?")] for r in perf.itertuples()]
        order = {"1m+5m": 0, "1m": 1, "5m": 2}
        bt["p"].sort(key=lambda x: (order.get(x[0], 3), -(x[3] or 0)))
    since = int((time.time() - 3600) * 1000)
    return {
        "v": 1, "t": int(time.time()),
        "s": {"on": int(bool(s.enabled)), "n": len(s.symbols),
              "ls": int(s.last_scan_ts) if s.last_scan_ts else 0,
              "la": int((s.last_scan_info or {}).get("alerts", 0)),
              "e": int(db.count_errors_since(since)), "bt": int(bool(bt_running))},
        "g": sigs, "b": bt,
        "c": {k: _num(getattr(cfg, TUNABLES[k][0])) for k in SETTINGS_IN_APP},
    }


def app_url(base: str, payload: dict | None, keyboard: bool = True, max_len: int = MAX_URL_LEN) -> str:
    """URL with the data snapshot; trims signals / pattern rows until it fits."""
    base = base.rstrip("/") + "/"
    if payload is None:
        return base + ("?k=1" if keyboard else "")
    p = json.loads(json.dumps(payload))
    while True:
        q = {"d": b64url(p)}
        if keyboard:
            q["k"] = "1"
        url = base + "?" + urlencode(q)
        if len(url) <= max_len:
            return url
        b = p.get("b", {})
        if len(b.get("p", [])) > 10:
            b["p"] = b["p"][:-1]
        elif len(b.get("o", [])) > 3:
            b["o"] = b["o"][:-1]
        elif b.get("pm"):
            b.pop("pm")
        elif len(p.get("g", [])) > 8:
            p["g"] = p["g"][:-1]
        elif b.get("p"):
            b["p"] = b["p"][:-1]
        elif b.get("o"):
            b["o"] = b["o"][:-1]
        elif p.get("g"):
            p["g"] = p["g"][:-1]
        else:
            return url


def parse_action(data: str) -> tuple[str, list[str]]:
    """Validate a web_app_data payload and map it to (command, args). Raises ValueError."""
    if not data or len(data) > 4096:
        raise ValueError("empty or too large")
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as e:
        raise ValueError("not JSON") from e
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    a = str(obj.get("a", "")).strip().lower()
    if a not in ACTIONS:
        raise ValueError(f"action not allowed: {a!r}")
    if a == "backtest":
        try:
            n = int(obj.get("symbols", 5))
            d = float(obj.get("days", 3))
        except (TypeError, ValueError) as e:
            raise ValueError("bad backtest numbers") from e
        if not (1 <= n <= 100) or not (0.5 <= d <= 30):
            raise ValueError("symbols must be 1-100 and days 0.5-30")
        return "backtest", [str(n), f"{d:g}"]
    if a == "set":
        k = str(obj.get("key", "")).strip().lower()
        if k not in TUNABLES:
            raise ValueError(f"setting not allowed: {k!r}")
        try:
            v = float(obj.get("value"))
        except (TypeError, ValueError) as e:
            raise ValueError("value must be a number") from e
        if math.isnan(v) or math.isinf(v):
            raise ValueError("value must be a number")
        return "set", [k, f"{v:g}"]
    return a, []
