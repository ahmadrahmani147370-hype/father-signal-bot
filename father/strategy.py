"""Signal engines.

1M ENGINE  - candlestick detection, volume spikes, short-term momentum (RSI / ROC), entry candidates.
5M ENGINE  - trend (EMA fast/slow + slope), market structure (higher highs/lows), momentum (RSI + MACD hist).
COMBINED   - a 1m pattern is only a signal when the latest *closed* 5m candle confirms it.

Score parts: pattern 30, volume 0-15, 5m trend 0-20, 5m structure 0-10, 5m momentum 0-10,
1m momentum turn 0-10, RSI room 0-5, 5m pattern in same direction +5 (capped at 100).
Signal Score is an ENGINEERING SCORE (0-100) that counts how many quality checks passed.
It is NOT a probability and NOT a win rate.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .patterns import PATTERNS, detect_patterns, patterns_long

SCORE_CLASSES = [(85, "Strong candidate"), (75, "Qualified candidate"), (60, "Watchlist"), (0, "Reject")]


def score_class(score: float) -> str:
    for th, name in SCORE_CLASSES:
        if score >= th:
            return name
    return "Reject"


# ------------------------------------------------------------------ indicators
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def add_indicators(df: pd.DataFrame, cfg) -> pd.DataFrame:
    df = df.copy()
    c = df["close"]
    df["ema_fast"] = ema(c, cfg.ema_fast)
    df["ema_slow"] = ema(c, cfg.ema_slow)
    df["rsi"] = rsi(c, cfg.rsi_period)
    df["atr"] = atr(df, cfg.atr_period)
    df["atr_pct"] = df["atr"] / c * 100
    # volume vs the average of the previous 20 candles (current candle excluded)
    df["vol_ratio"] = df["volume"] / df["volume"].shift(1).rolling(20, min_periods=10).mean()
    df["roc3"] = c.pct_change(3) * 100
    macd = ema(c, 12) - ema(c, 26)
    df["macd_hist"] = macd - ema(macd, 9)

    slope = df["ema_slow"] - df["ema_slow"].shift(5)
    up = (df["ema_fast"] > df["ema_slow"]) & (c > df["ema_slow"]) & (slope > 0)
    dn = (df["ema_fast"] < df["ema_slow"]) & (c < df["ema_slow"]) & (slope < 0)
    df["trend"] = np.where(up, 1, np.where(dn, -1, 0))

    hh = df["high"].rolling(10).max() > df["high"].shift(10).rolling(10).max()
    hl = df["low"].rolling(10).min() > df["low"].shift(10).rolling(10).min()
    lh = df["high"].rolling(10).max() < df["high"].shift(10).rolling(10).max()
    ll = df["low"].rolling(10).min() < df["low"].shift(10).rolling(10).min()
    df["structure"] = np.where(hh & hl, 1, np.where(lh & ll, -1, 0))

    df["mom"] = np.where((df["rsi"] > 50) & (df["macd_hist"] > 0), 1,
                         np.where((df["rsi"] < 50) & (df["macd_hist"] < 0), -1, 0))
    med = df["atr_pct"].rolling(200, min_periods=50).median()
    df["vol_regime"] = np.where(df["atr_pct"] > med, "high_vol", "low_vol")
    df["regime"] = np.where(df["trend"] == 1, "trend_up", np.where(df["trend"] == -1, "trend_down", "range"))
    return df


def trend_label(v: int) -> str:
    return {1: "Bullish", -1: "Bearish"}.get(int(v), "Neutral")


# --------------------------------------------------------------- score weights
def pattern_adjustments(perf: pd.DataFrame | None, min_oos: int = 200) -> dict[tuple[str, str], float]:
    """Turn stored out-of-sample backtest results into score adjustments (engineering only)."""
    adj: dict[tuple[str, str], float] = {}
    if perf is None or perf.empty:
        return adj
    for r in perf.itertuples(index=False):
        pf = r.oos_profit_factor if r.oos_profit_factor is not None else 0
        n = r.oos_trades or 0
        if n >= min_oos and pf >= 1.1:
            a = 10
        elif n >= 50 and pf >= 1.1:
            a = 3
        elif n >= 50 and pf < 1.0:
            a = -15
        else:
            a = 0
        adj[(r.timeframe, r.pattern)] = a
    return adj


# ------------------------------------------------------------------ the engine
def build_events(df: pd.DataFrame, cfg, htf: pd.DataFrame | None = None, tf: str = "1m",
                 adjustments: dict | None = None) -> pd.DataFrame:
    """Return one row per pattern detection with features + engineering score.

    df   : candles of the entry timeframe (closed candles only), indicators added here.
    htf  : optional higher-timeframe candles (e.g. 5m) used as confirmation. If None, the entry
           timeframe's own trend/structure/momentum are used (stand-alone engine test).
    """
    if len(df) < 60:
        return pd.DataFrame()
    d = add_indicators(df, cfg).reset_index(drop=True)
    ev = patterns_long(d, detect_patterns(d))
    if ev.empty:
        return pd.DataFrame()
    feats = d.loc[ev["idx"].to_numpy(), ["open_time", "close_time", "close", "rsi", "atr", "vol_ratio", "roc3",
                                         "trend", "structure", "mom", "regime", "vol_regime"]].reset_index(drop=True)
    ev = pd.concat([ev.reset_index(drop=True), feats], axis=1)
    ev["rsi_prev"] = d["rsi"].shift(1).loc[ev["idx"].to_numpy()].to_numpy()

    if htf is not None and len(htf) >= 60:
        hi = add_indicators(htf, cfg).reset_index(drop=True)
        hp = detect_patterns(hi)
        longs = [p for p, d in PATTERNS.items() if d == "LONG"]
        shorts = [p for p, d in PATTERNS.items() if d == "SHORT"]
        # name of the first bullish / bearish pattern on each closed 5m candle ("" if none)
        hi["c_pat_long"] = hp[longs].idxmax(axis=1).where(hp[longs].any(axis=1), "")
        hi["c_pat_short"] = hp[shorts].idxmax(axis=1).where(hp[shorts].any(axis=1), "")
        h = hi[["close_time", "trend", "structure", "mom", "rsi", "c_pat_long", "c_pat_short"]].dropna(subset=["close_time"])
        h = h.rename(columns={"close_time": "htf_close_time", "trend": "c_trend", "structure": "c_structure",
                              "mom": "c_mom", "rsi": "c_rsi"}).sort_values("htf_close_time")
        ev = ev.sort_values("close_time")
        # backward as-of: only 5m candles that had CLOSED at (or before) the 1m candle close
        ev = pd.merge_asof(ev, h, left_on="close_time", right_on="htf_close_time", direction="backward")
        ev = ev.sort_values(["idx", "pattern"]).reset_index(drop=True)
        ev[["c_trend", "c_structure", "c_mom"]] = ev[["c_trend", "c_structure", "c_mom"]].fillna(0)
        ev[["c_pat_long", "c_pat_short"]] = ev[["c_pat_long", "c_pat_short"]].fillna("")
        ev["confirm_tf"] = "5m"
    else:
        ev["c_trend"], ev["c_structure"], ev["c_mom"], ev["c_rsi"] = ev["trend"], ev["structure"], ev["mom"], ev["rsi"]
        ev["c_pat_long"], ev["c_pat_short"] = "", ""
        ev["confirm_tf"] = tf
    ev["entry_tf"] = tf
    return score_events(ev, cfg, tf, adjustments)


def score_events(ev: pd.DataFrame, cfg, tf: str, adjustments: dict | None = None) -> pd.DataFrame:
    sign = np.where(ev["direction"] == "LONG", 1, -1)
    t_al = ev["c_trend"].to_numpy() * sign
    s_al = ev["c_structure"].to_numpy() * sign
    m_al = ev["c_mom"].to_numpy() * sign
    vr = ev["vol_ratio"].fillna(0).to_numpy()
    r = ev["rsi"].fillna(50).to_numpy()
    rp = ev["rsi_prev"].fillna(50).to_numpy()

    score = np.full(len(ev), 30.0)                                              # pattern present
    score += np.where(vr >= cfg.volume_spike_ratio, 15, np.where(vr >= 1.0, 7, 0))  # volume
    score += np.where(t_al == 1, 20, np.where(t_al == 0, 8, 0))                 # trend alignment
    score += np.where(s_al == 1, 10, np.where(s_al == 0, 4, 0))                 # market structure
    score += np.where(m_al == 1, 10, np.where(m_al == 0, 4, 0))                 # confirm-tf momentum
    mom_ok = np.where(sign == 1, r > rp, r < rp)                                # entry-tf momentum turn
    score += np.where(mom_ok, 10, 0)
    room = np.where(sign == 1, r < 70, r > 30)                                  # RSI not exhausted
    score += np.where(room, 5, 0)
    # higher-timeframe candle pattern in the same direction (5m engine) -> small bonus
    htf_pat = np.where(sign == 1, ev["c_pat_long"].astype(str), ev["c_pat_short"].astype(str)) \
        if "c_pat_long" in ev else np.full(len(ev), "")
    score += np.where(htf_pat != "", 5, 0)
    if adjustments:
        score += ev["pattern"].map(lambda p: adjustments.get((tf, p), 0)).to_numpy(dtype=float)
    ev = ev.copy()
    ev["score"] = np.clip(score, 0, 100)
    ev["score_class"] = [score_class(s) for s in ev["score"]]
    ev["volume_confirmed"] = vr >= cfg.volume_spike_ratio
    ev["trend_confirmed"] = t_al == 1
    ev["trend_against"] = t_al == -1
    ev["momentum_confirmed"] = mom_ok
    ev["structure_confirmed"] = s_al == 1
    ev["htf_pattern"] = htf_pat
    # a qualified signal needs: score >= MIN_ALERT_SCORE, confirmation-timeframe trend aligned,
    # and (by default) a volume spike
    vol_req = ev["volume_confirmed"] if int(getattr(cfg, "require_volume", 1)) else True
    ev["qualified"] = (ev["score"] >= cfg.min_alert_score) & ev["trend_confirmed"] & vol_req
    return ev


def reasons_for(row) -> list[str]:
    d = "Bullish" if row["direction"] == "LONG" else "Bearish"
    out = [f"{d} pattern detected."]
    ctf = row.get("confirm_tf", "5m")
    if row["trend_confirmed"]:
        out.append(f"{ctf} trend aligned.")
    if row["volume_confirmed"]:
        out.append("Volume confirmation.")
    if row["momentum_confirmed"]:
        out.append("Momentum confirmation.")
    if row.get("structure_confirmed"):
        out.append(f"{ctf} market structure aligned.")
    if row.get("htf_pattern"):
        out.append(f"{ctf} {row['htf_pattern']} pattern aligned.")
    return out
