"""Backtesting engine.

Rules that keep results honest:
- Signals use only closed candles; ENTRY is at the NEXT candle's open (no look-ahead).
- Fees + slippage are charged on entry and exit (taker both sides by default).
- TP / SL from ATR at the signal candle; if TP and SL are touched in the same candle we assume SL first.
- Time stop after MAX_HOLD_BARS; trades whose window runs past the data end are dropped.
- No overlapping trades: inside each tested strategy/group, one open trade per symbol at a time.
- Chronological split: TRAIN -> VALIDATION -> TEST (unseen). Verdicts need TRAIN and VALIDATION to agree,
  and TEST must have >= MIN_OOS_TRADES trades or it is flagged "insufficient sample".
Returns are % per trade at 1x (no leverage), summed (not compounded).
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .binance import INTERVAL_MS, BinanceFuturesPublic
from .config import ROOT
from .db import Database
from .patterns import PATTERNS
from .strategy import build_events

log = logging.getLogger("father.backtest")


# ------------------------------------------------------------------ data
def load_history(db: Database, client: BinanceFuturesPublic | None, symbol: str, interval: str,
                 start_ms: int, end_ms: int) -> pd.DataFrame:
    """Use cached SQLite candles; download only what is missing (incremental)."""
    step = INTERVAL_MS[interval]
    if client is not None:
        first = db.first_open_time(symbol, interval)
        last = db.last_open_time(symbol, interval)
        if first is None or first > start_ms + step:
            df = client.klines_range(symbol, interval, start_ms, end_ms)
            db.upsert_candles(symbol, interval, df)
        elif last is not None and last < end_ms - 2 * step:
            df = client.klines_range(symbol, interval, last + step, end_ms)
            db.upsert_candles(symbol, interval, df)
    return db.load_candles(symbol, interval, start_ms, end_ms)


# -------------------------------------------------------------- simulation
def simulate(candles: pd.DataFrame, ev: pd.DataFrame, cfg) -> pd.DataFrame:
    """Vectorised trade outcomes for every event (before the no-overlap filter)."""
    if ev is None or ev.empty:
        return pd.DataFrame()
    O = candles["open"].to_numpy(float)
    H = candles["high"].to_numpy(float)
    L = candles["low"].to_numpy(float)
    C = candles["close"].to_numpy(float)
    T = candles["open_time"].to_numpy("int64")
    n, hold = len(O), int(cfg.max_hold_bars)
    ev = ev[ev["atr"].notna() & (ev["atr"] > 0)].copy()
    ev = ev[ev["idx"] + hold < n]          # full window must exist in the data
    if ev.empty:
        return pd.DataFrame()
    e = ev["idx"].to_numpy(int) + 1         # entry bar = next candle
    long = (ev["direction"] == "LONG").to_numpy()
    a = ev["atr"].to_numpy(float)
    raw = O[e]
    slip, fee = cfg.slippage_pct / 100, cfg.fee_pct / 100
    tp = np.where(long, raw + cfg.tp_atr * a, raw - cfg.tp_atr * a)
    sl = np.where(long, raw - cfg.sl_atr * a, raw + cfg.sl_atr * a)

    win = e[:, None] + np.arange(hold)[None, :]
    Hw, Lw, Ow = H[win], L[win], O[win]
    sl_hit = np.where(long[:, None], Lw <= sl[:, None], Hw >= sl[:, None])
    tp_hit = np.where(long[:, None], Hw >= tp[:, None], Lw <= tp[:, None])
    big = hold + 1
    f_sl = np.where(sl_hit.any(1), sl_hit.argmax(1), big)
    f_tp = np.where(tp_hit.any(1), tp_hit.argmax(1), big)
    rows = np.arange(len(e))

    is_sl = (f_sl <= f_tp) & (f_sl < big)   # same-candle tie -> stop first (conservative)
    is_tp = ~is_sl & (f_tp < big)
    exit_off = np.where(is_sl, f_sl, np.where(is_tp, f_tp, hold - 1))
    o_at = Ow[rows, np.minimum(exit_off, hold - 1)]
    # gap through the stop on a later candle's open -> fill at that worse open
    sl_px = np.where(long, np.where(exit_off > 0, np.minimum(sl, o_at), sl),
                     np.where(exit_off > 0, np.maximum(sl, o_at), sl))
    px = np.where(is_sl, sl_px, np.where(is_tp, tp, C[e + hold - 1]))
    entry = np.where(long, raw * (1 + slip), raw * (1 - slip))
    exitp = np.where(long, px * (1 - slip), px * (1 + slip))
    ret = np.where(long, exitp / entry - 1, entry / exitp - 1) * 100 - 2 * fee * 100

    out = ev.reset_index(drop=True).copy()
    out["entry_idx"] = e
    out["exit_idx"] = e + exit_off
    out["entry_time"] = T[e]
    out["ret"] = ret
    out["exit_reason"] = np.where(is_sl, "SL", np.where(is_tp, "TP", "TIME"))
    return out


def no_overlap(tr: pd.DataFrame) -> pd.DataFrame:
    """Keep trades in time order; skip a trade while the previous one on the same symbol is still open."""
    if tr.empty:
        return tr
    keep = []
    for _, g in tr.sort_values(["symbol", "entry_idx", "score"], ascending=[True, True, False]).groupby("symbol"):
        last_exit = -1
        ei, xi = g["entry_idx"].to_numpy(), g["exit_idx"].to_numpy()
        for k, i in enumerate(g.index):
            if ei[k] > last_exit:
                keep.append(i)
                last_exit = xi[k]
    return tr.loc[keep].sort_values("entry_time")


# ------------------------------------------------------------------- stats
def stats(r: pd.DataFrame, min_oos: int | None = None) -> dict:
    r = r.sort_values("entry_time") if not r.empty else r
    x = r["ret"].to_numpy(float) if not r.empty else np.array([])
    n = len(x)
    wins, losses = x[x > 0], x[x <= 0]
    gp, gl = wins.sum(), -losses.sum()
    eq = np.cumsum(x) if n else np.array([0.0])
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:] if n else np.array([0.0])
    lg = r[r["direction"] == "LONG"]["ret"] if n else pd.Series(dtype=float)
    sh = r[r["direction"] == "SHORT"]["ret"] if n else pd.Series(dtype=float)
    wr = len(wins) / n * 100 if n else 0.0
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = losses.mean() if len(losses) else 0.0
    d = {
        "total": n, "wins": int(len(wins)), "losses": int(len(losses)),
        "win_rate": round(wr, 2),
        "profit_factor": round(gp / gl, 3) if gl > 0 else (999.0 if gp > 0 else 0.0),
        "net_return": round(float(x.sum()), 3),
        "max_drawdown": round(float((peak - eq).max()) if n else 0.0, 3),
        "avg_return": round(float(x.mean()) if n else 0.0, 4),
        "expectancy": round(wr / 100 * avg_win + (1 - wr / 100) * avg_loss, 4),
        "avg_win": round(float(avg_win), 4), "avg_loss": round(float(avg_loss), 4),
        "long_trades": int(len(lg)), "long_net": round(float(lg.sum()), 3),
        "short_trades": int(len(sh)), "short_net": round(float(sh.sum()), 3),
    }
    if min_oos is not None:
        d["sample_status"] = "testable" if n >= min_oos else f"insufficient sample (<{min_oos} OOS trades)"
    return d


# ------------------------------------------------------------------- runner
def _dimensions(tf_name: str, tr: pd.DataFrame) -> list[tuple[str, str, pd.DataFrame]]:
    out: list[tuple[str, str, pd.DataFrame]] = [("overall", "all", tr)]
    if tf_name == "1m+5m":
        q = tr[tr["qualified"]]
        out.append(("strategy", "qualified (score+5m trend+volume)", q))
        out.append(("strategy", "not qualified", tr[~tr["qualified"]]))
        base = q
    else:
        base = tr
    for p in PATTERNS:
        out.append(("pattern", p, base[base["pattern"] == p]))
    for dval, name in (("LONG", "long (bullish)"), ("SHORT", "short (bearish)")):
        out.append(("direction", name, base[base["direction"] == dval]))
    out.append(("volume", "confirmed", tr[tr["volume_confirmed"]]))
    out.append(("volume", "unconfirmed", tr[~tr["volume_confirmed"]]))
    out.append(("trend", "confirmed", tr[tr["trend_confirmed"]]))
    out.append(("trend", "unconfirmed", tr[~tr["trend_confirmed"]]))
    for g in ("trend_up", "trend_down", "range"):
        out.append(("regime", g, tr[tr["regime"] == g]))
    for g in ("high_vol", "low_vol"):
        out.append(("volatility", g, tr[tr["vol_regime"] == g]))
    for g in ("Strong candidate", "Qualified candidate", "Watchlist", "Reject"):
        out.append(("score_class", g, tr[tr["score_class"] == g]))
    return out


def verdict(train_pf: float, val_pf: float, oos: dict, min_oos: int) -> str:
    if oos["total"] < min_oos:
        return "insufficient sample"
    if train_pf > 1 and val_pf > 1 and oos["profit_factor"] > 1:
        return "positive after costs (OOS)"
    if oos["profit_factor"] > 1:
        return "unstable (OOS positive, train/val not)"
    return "negative after costs"


def run_backtest(cfg, db: Database, client: BinanceFuturesPublic | None, symbols: list[str], days: float,
                 progress: Callable[[str], None] | None = None,
                 data: dict[str, dict[str, pd.DataFrame]] | None = None) -> dict:
    """Run the full study. `data` may supply candles directly ({sym: {'1m': df, '5m': df}}) for offline tests."""
    say = progress or (lambda m: log.info(m))
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    end_ms = (client.now_ms() if client else int(time.time() * 1000))
    start_ms = end_ms - int(days * 86_400_000)
    trades: dict[str, list[pd.DataFrame]] = {"1m": [], "5m": [], "1m+5m": []}
    t_lo, t_hi = None, None
    for k, sym in enumerate(symbols, 1):
        try:
            if data is not None:
                d1, d5 = data[sym]["1m"], data[sym]["5m"]
            else:
                say(f"[{k}/{len(symbols)}] loading {sym} ...")
                d1 = load_history(db, client, sym, "1m", start_ms, end_ms)
                d5 = load_history(db, client, sym, "5m", start_ms, end_ms)
            if len(d1) < 300 or len(d5) < 100:
                say(f"{sym}: not enough history, skipped")
                continue
            d1, d5 = d1.reset_index(drop=True), d5.reset_index(drop=True)
            t_lo = d1["open_time"].iloc[0] if t_lo is None else min(t_lo, d1["open_time"].iloc[0])
            t_hi = d1["open_time"].iloc[-1] if t_hi is None else max(t_hi, d1["open_time"].iloc[-1])
            for name, candles, htf, tf in (("1m", d1, None, "1m"), ("5m", d5, None, "5m"), ("1m+5m", d1, d5, "1m")):
                ev = build_events(candles, cfg, htf=htf, tf=tf)
                tr = simulate(candles, ev, cfg)
                if not tr.empty:
                    tr["symbol"] = sym
                    trades[name].append(tr)
        except Exception as e:
            db.log_error(sym, "backtest", f"{type(e).__name__}: {e}")
            say(f"{sym}: error {e}")
    if t_lo is None:
        raise RuntimeError("No usable candle data for backtest")

    span = t_hi - t_lo
    b1 = t_lo + span * cfg.train_frac
    b2 = t_lo + span * (cfg.train_frac + cfg.val_frac)
    params = (f"days={days} symbols={len(symbols)} fee={cfg.fee_pct}%/side slip={cfg.slippage_pct}%/side "
              f"tp={cfg.tp_atr}ATR sl={cfg.sl_atr}ATR hold={cfg.max_hold_bars} min_score={cfg.min_alert_score}")
    created = int(time.time() * 1000)
    rows, perf = [], []
    for name, parts in trades.items():
        if not parts:
            continue
        allt = pd.concat(parts, ignore_index=True)
        allt["split"] = np.where(allt["entry_time"] < b1, "train", np.where(allt["entry_time"] < b2, "validation", "test"))
        for dim, grp, sub in _dimensions(name, allt):
            sub = no_overlap(sub)
            res = {}
            for split in ("train", "validation", "test", "all"):
                s = sub if split == "all" else sub[sub["split"] == split]
                st = stats(s, cfg.min_oos_trades if split == "test" else None)
                res[split] = st
                rows.append({"run_id": run_id, "created_at": created, "params": params, "timeframe": name,
                             "dimension": dim, "grp": grp, "split": split, **st})
            if dim == "pattern":
                o = res["test"]
                perf.append({"updated_at": created, "run_id": run_id, "timeframe": name, "pattern": grp,
                             "oos_trades": o["total"], "oos_win_rate": o["win_rate"],
                             "oos_profit_factor": o["profit_factor"], "oos_avg_return": o["avg_return"],
                             "oos_net_return": o["net_return"], "train_pf": res["train"]["profit_factor"],
                             "val_pf": res["validation"]["profit_factor"],
                             "verdict": verdict(res["train"]["profit_factor"], res["validation"]["profit_factor"],
                                                o, cfg.min_oos_trades)})
    db.save_backtest_rows(rows)
    db.save_performance(perf)
    out_dir = ROOT / "data" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"backtest_{run_id}.csv"
    pd.DataFrame(rows).drop(columns=["params"]).to_csv(csv_path, index=False)
    return {"run_id": run_id, "params": params, "rows": pd.DataFrame(rows), "perf": pd.DataFrame(perf),
            "csv": str(csv_path), "range": (int(t_lo), int(t_hi)), "splits": (int(b1), int(b2)),
            "symbols": symbols}


# ------------------------------------------------------------------ report
def _fmt_pf(v: float) -> str:
    return "inf" if v >= 999 else f"{v:.2f}"


def summary_text(res: dict) -> str:
    rows: pd.DataFrame = res["rows"]
    t = lambda ms: time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))
    lines = ["FATHER SIGNAL BOT - BACKTEST", f"Run: {res['run_id']}", res["params"],
             f"Data (UTC): {t(res['range'][0])} -> {t(res['range'][1])}",
             f"Train < {t(res['splits'][0])} <= Validation < {t(res['splits'][1])} <= Test (unseen)",
             "", "TEST (out-of-sample) results:", f"{'Strategy':<26}{'N':>5}{'WR%':>6}{'PF':>6}{'Net%':>8}{'DD%':>7}"]
    sel = rows[(rows["split"] == "test") & (rows["dimension"].isin(["overall", "strategy", "direction"]))]
    for r in sel.itertuples():
        label = f"{r.timeframe} {r.grp}"[:25]
        lines.append(f"{label:<26}{r.total:>5}{r.win_rate:>6.1f}{_fmt_pf(r.profit_factor):>6}{r.net_return:>8.2f}{r.max_drawdown:>7.2f}")
    return "\n".join(lines)


def pattern_table(perf: pd.DataFrame) -> str:
    if perf is None or perf.empty:
        return "No pattern results."
    lines = ["PATTERN RESULTS (TEST split, after fees+slippage)",
             f"{'TF':<6}{'Pattern':<18}{'N':>5}{'WR%':>6}{'PF':>6}{'Avg%':>7}  Verdict"]
    for r in perf.itertuples():
        lines.append(f"{r.timeframe:<6}{r.pattern[:17]:<18}{r.oos_trades:>5}{r.oos_win_rate:>6.1f}"
                     f"{_fmt_pf(r.oos_profit_factor):>6}{r.oos_avg_return:>7.3f}  {r.verdict}")
    lines += ["", "N<200 = insufficient sample: do not trust the numbers yet.",
              "Past results do not predict future results. Analysis only."]
    return "\n".join(lines)


def filters_table(rows: pd.DataFrame) -> str:
    """Test-split results by condition: volume, trend, regime, volatility, score class."""
    if rows is None or rows.empty:
        return "No backtest yet."
    t = rows[(rows["split"] == "test") & rows["dimension"].isin(["volume", "trend", "regime", "volatility", "score_class"])]
    lines = ["CONDITIONS (TEST split, after fees+slippage)",
             f"{'TF':<6}{'Condition':<24}{'N':>5}{'WR%':>6}{'PF':>6}{'Net%':>8}"]
    for r in t.itertuples():
        lines.append(f"{r.timeframe:<6}{(r.dimension + ':' + r.grp)[:23]:<24}{r.total:>5}{r.win_rate:>6.1f}"
                     f"{_fmt_pf(r.profit_factor):>6}{r.net_return:>8.2f}")
    lines.append("Trend = confirmation-timeframe trend agrees with the signal. N<200 = insufficient sample.")
    return "\n".join(lines)
