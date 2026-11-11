"""Vectorised candlestick pattern detectors (pandas/numpy).

Each detector uses only the current and *previous* candles - never future data.
All patterns are evaluated on fully closed candles only (the scanner drops the live candle).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

PATTERNS: dict[str, str] = {
    "Bullish Engulfing": "LONG",
    "Bearish Engulfing": "SHORT",
    "Hammer": "LONG",
    "Shooting Star": "SHORT",
    "Morning Star": "LONG",
    "Evening Star": "SHORT",
    "Bullish Marubozu": "LONG",
    "Bearish Marubozu": "SHORT",
    "Tweezer Bottom": "LONG",
    "Tweezer Top": "SHORT",
}

AVG_WINDOW = 14
TREND_LOOKBACK = 5


def _parts(df: pd.DataFrame) -> dict[str, pd.Series]:
    o, h, l, c = (df[k].astype(float) for k in ("open", "high", "low", "close"))
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    # average of the *previous* candles' bodies / ranges (excludes the current one)
    avg_body = body.shift(1).rolling(AVG_WINDOW, min_periods=5).mean()
    avg_rng = (h - l).shift(1).rolling(AVG_WINDOW, min_periods=5).mean()
    bull = c > o
    bear = c < o
    # simple prior-trend context measured up to the previous candle
    prior_down = c.shift(1) < c.shift(1 + TREND_LOOKBACK)
    prior_up = c.shift(1) > c.shift(1 + TREND_LOOKBACK)
    return dict(o=o, h=h, l=l, c=c, body=body, rng=rng, upper=upper, lower=lower,
                avg_body=avg_body, avg_rng=avg_rng, bull=bull, bear=bear,
                prior_down=prior_down, prior_up=prior_up)


def detect_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a boolean DataFrame (same index as df) with one column per pattern."""
    p = _parts(df)
    o, h, l, c = p["o"], p["h"], p["l"], p["c"]
    body, rng, upper, lower = p["body"], p["rng"], p["upper"], p["lower"]
    avg_body, avg_rng, bull, bear = p["avg_body"], p["avg_rng"], p["bull"], p["bear"]
    o1, c1, h1, l1 = o.shift(1), c.shift(1), h.shift(1), l.shift(1)
    o2, c2 = o.shift(2), c.shift(2)
    body1, body2 = body.shift(1), body.shift(2)
    bull1, bear1 = bull.shift(1, fill_value=False), bear.shift(1, fill_value=False)
    bull2, bear2 = bull.shift(2, fill_value=False), bear.shift(2, fill_value=False)
    meaningful = body >= 0.5 * avg_body

    out = pd.DataFrame(index=df.index)

    # 1/2 Engulfing: current real body fully engulfs the previous opposite body
    out["Bullish Engulfing"] = bear1 & bull & (o <= c1) & (c >= o1) & (body > body1) & meaningful & p["prior_down"]
    out["Bearish Engulfing"] = bull1 & bear & (o >= c1) & (c <= o1) & (body > body1) & meaningful & p["prior_up"]

    # 3/4 Hammer / Shooting star: long shadow >= 2x body, tiny opposite shadow, after a move
    small_body = (body / rng) <= 0.35
    out["Hammer"] = (lower >= 2 * body) & (upper <= 0.1 * rng) & small_body & (body > 0) & p["prior_down"]
    out["Shooting Star"] = (upper >= 2 * body) & (lower <= 0.1 * rng) & small_body & (body > 0) & p["prior_up"]

    # 5/6 Morning / Evening star: big candle, small "star", strong reversal past the midpoint
    star_small = body1 <= 0.5 * avg_body
    big2 = body2 >= avg_body
    out["Morning Star"] = bear2 & big2 & star_small & bull & (c >= (o2 + c2) / 2) & (np.maximum(o1, c1) <= c2 + 0.25 * body2)
    out["Evening Star"] = bull2 & big2 & star_small & bear & (c <= (o2 + c2) / 2) & (np.minimum(o1, c1) >= c2 - 0.25 * body2)

    # 7/8 Marubozu: body >= 90% of range and larger than average
    maru = ((body / rng) >= 0.9) & (body >= 1.2 * avg_body)
    out["Bullish Marubozu"] = maru & bull
    out["Bearish Marubozu"] = maru & bear

    # 9/10 Tweezers: matching lows/highs with a colour flip, after a move
    tol = 0.05 * avg_rng
    out["Tweezer Bottom"] = bear1 & bull & ((l - l1).abs() <= tol) & p["prior_down"] & meaningful
    out["Tweezer Top"] = bull1 & bear & ((h - h1).abs() <= tol) & p["prior_up"] & meaningful

    return out.fillna(False).astype(bool)


def patterns_long(df: pd.DataFrame, pats: pd.DataFrame | None = None) -> pd.DataFrame:
    """Long format: one row per (candle index, pattern) detection with its direction."""
    if pats is None:
        pats = detect_patterns(df)
    stacked = pats.stack()
    stacked = stacked[stacked]
    if stacked.empty:
        return pd.DataFrame(columns=["idx", "pattern", "direction"])
    ev = stacked.reset_index()
    ev.columns = ["idx", "pattern", "_v"]
    ev["direction"] = ev["pattern"].map(PATTERNS)
    return ev[["idx", "pattern", "direction"]]
