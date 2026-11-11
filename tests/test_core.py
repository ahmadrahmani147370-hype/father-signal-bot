"""Run:  python -m unittest discover -s tests -v"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from father.backtest import no_overlap, run_backtest, simulate, stats  # noqa: E402
from father.binance import drop_unclosed  # noqa: E402
from father.config import Config  # noqa: E402
from father.db import Database  # noqa: E402
from father.patterns import detect_patterns  # noqa: E402
from father.strategy import build_events, score_class  # noqa: E402
from father.telegram_alerts import format_signal  # noqa: E402


def trend(n, start, step, body=0.3):
    """n small candles drifting by `step` per bar (downtrend if step<0)."""
    rows, p = [], start
    for _ in range(n):
        o = p
        c = p + step
        hi, lo = max(o, c) + body * 0.2, min(o, c) - body * 0.2
        rows.append((o, hi, lo, c))
        p = c
    return rows


def frame(rows):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["volume"] = 100.0
    df["open_time"] = np.arange(len(df), dtype="int64") * 60_000
    df["close_time"] = df["open_time"] + 59_999
    df["quote_volume"] = df["volume"] * df["close"]
    df["trades"] = 100
    return df


def last_hits(rows):
    return set(c for c, v in detect_patterns(frame(rows)).iloc[-1].items() if v)


class PatternTests(unittest.TestCase):
    def setUp(self):
        self.down = trend(20, 120, -0.5)   # ends near 110
        self.up = trend(20, 100, 0.5)      # ends near 110

    def test_bullish_engulfing(self):
        p = self.down[-1][3]
        rows = self.down + [(p, p + 0.1, p - 0.8, p - 0.7), (p - 0.9, p + 0.6, p - 1.0, p + 0.5)]
        self.assertIn("Bullish Engulfing", last_hits(rows))

    def test_bearish_engulfing(self):
        p = self.up[-1][3]
        rows = self.up + [(p, p + 0.8, p - 0.1, p + 0.7), (p + 0.9, p + 1.0, p - 0.6, p - 0.5)]
        self.assertIn("Bearish Engulfing", last_hits(rows))

    def test_hammer(self):
        p = self.down[-1][3]
        rows = self.down + [(p, p + 0.32, p - 1.5, p + 0.3)]
        self.assertIn("Hammer", last_hits(rows))

    def test_shooting_star(self):
        p = self.up[-1][3]
        rows = self.up + [(p, p + 1.5, p - 0.32, p - 0.3)]
        self.assertIn("Shooting Star", last_hits(rows))

    def test_morning_star(self):
        p = self.down[-1][3]
        rows = self.down + [(p, p + 0.1, p - 1.3, p - 1.2), (p - 1.3, p - 1.2, p - 1.5, p - 1.35),
                            (p - 1.3, p - 0.1, p - 1.35, p - 0.2)]
        self.assertIn("Morning Star", last_hits(rows))

    def test_evening_star(self):
        p = self.up[-1][3]
        rows = self.up + [(p, p + 1.3, p - 0.1, p + 1.2), (p + 1.3, p + 1.5, p + 1.2, p + 1.35),
                          (p + 1.3, p + 1.35, p + 0.1, p + 0.2)]
        self.assertIn("Evening Star", last_hits(rows))

    def test_marubozu(self):
        p = self.up[-1][3]
        self.assertIn("Bullish Marubozu", last_hits(self.up + [(p, p + 1.02, p - 0.01, p + 1.0)]))
        self.assertIn("Bearish Marubozu", last_hits(self.up + [(p, p + 0.01, p - 1.02, p - 1.0)]))

    def test_tweezer_bottom(self):
        p = self.down[-1][3]
        rows = self.down + [(p, p + 0.05, p - 0.75, p - 0.6), (p - 0.6, p + 0.05, p - 0.75, p - 0.05)]
        self.assertIn("Tweezer Bottom", last_hits(rows))

    def test_tweezer_top(self):
        p = self.up[-1][3]
        rows = self.up + [(p, p + 0.75, p - 0.05, p + 0.6), (p + 0.6, p + 0.75, p - 0.05, p + 0.05)]
        self.assertIn("Tweezer Top", last_hits(rows))

    def test_no_false_positive_on_plain_trend(self):
        hits = detect_patterns(frame(trend(60, 100, 0.5)))
        for col in ("Bullish Engulfing", "Bearish Engulfing", "Hammer", "Morning Star", "Evening Star",
                    "Tweezer Top", "Tweezer Bottom"):
            self.assertFalse(hits[col].any(), col)

    def test_no_lookahead(self):
        """Detections on a prefix must equal detections on the full series for the same rows."""
        rng = np.random.default_rng(1)
        c = 100 + rng.normal(0, 0.3, 500).cumsum()
        o = np.r_[c[0], c[:-1]]
        h = np.maximum(o, c) + rng.random(500) * 0.3
        lo = np.minimum(o, c) - rng.random(500) * 0.3
        df = frame(list(zip(o, h, lo, c)))
        full = detect_patterns(df)
        part = detect_patterns(df.iloc[:300])
        pd.testing.assert_frame_equal(full.iloc[:300], part)


class EngineTests(unittest.TestCase):
    def test_score_classes(self):
        self.assertEqual(score_class(90), "Strong candidate")
        self.assertEqual(score_class(80), "Qualified candidate")
        self.assertEqual(score_class(65), "Watchlist")
        self.assertEqual(score_class(40), "Reject")

    def test_drop_unclosed(self):
        df = frame(trend(5, 100, 0.1))
        now = int(df["close_time"].iloc[-1])  # last candle not closed yet at this instant
        self.assertEqual(len(drop_unclosed(df, now)), 4)

    def test_format_signal(self):
        msg = format_signal({"direction": "LONG", "symbol": "BTCUSDT", "entry_tf": "1m", "confirm_tf": "5m",
                             "pattern": "Bullish Engulfing", "volume_confirmed": True, "trend": "Bullish",
                             "rsi": 54.321, "score": 85, "reasons": ["Bullish pattern detected."]})
        self.assertIn("LONG SIGNAL", msg)
        self.assertIn("RSI: 54.3", msg)
        self.assertIn("Signal Score: 85/100", msg)
        self.assertTrue(msg.endswith("Status: Analysis Only."))

    def test_simulation_and_overlap(self):
        rng = np.random.default_rng(7)
        n = 3000
        c = 100 + rng.normal(0, 0.15, n).cumsum()
        o = np.r_[c[0], c[:-1]]
        h = np.maximum(o, c) + rng.random(n) * 0.2
        lo = np.minimum(o, c) - rng.random(n) * 0.2
        df = frame(list(zip(o, h, lo, c)))
        df["volume"] = rng.random(n) * 200 + 50
        cfg = Config()
        ev = build_events(df, cfg, tf="1m")
        self.assertFalse(ev.empty)
        tr = simulate(df, ev, cfg)
        self.assertTrue((tr["entry_idx"] == tr["idx"] + 1).all())       # next-candle entry
        self.assertTrue((tr["exit_idx"] >= tr["entry_idx"]).all())
        tr["symbol"] = "X"
        k = no_overlap(tr).sort_values("entry_idx")
        self.assertTrue((k["entry_idx"].to_numpy()[1:] > k["exit_idx"].to_numpy()[:-1]).all())
        s = stats(k, 200)
        self.assertEqual(s["total"], s["wins"] + s["losses"])
        self.assertIn("sample_status", s)

    def test_offline_backtest_pipeline(self):
        rng = np.random.default_rng(3)
        n = 4000
        c = 100 + rng.normal(0, 0.12, n).cumsum()
        o = np.r_[c[0], c[:-1]]
        h = np.maximum(o, c) + rng.random(n) * 0.15
        lo = np.minimum(o, c) - rng.random(n) * 0.15
        d1 = frame(list(zip(o, h, lo, c)))
        d1["volume"] = rng.random(n) * 200 + 50
        g = d1.groupby(d1.index // 5)
        d5 = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                           "close": g["close"].last(), "volume": g["volume"].sum(),
                           "open_time": g["open_time"].first(), "close_time": g["close_time"].last(),
                           "quote_volume": g["quote_volume"].sum(), "trades": g["trades"].sum()})
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            res = run_backtest(Config(), db, None, ["SYN"], 3, data={"SYN": {"1m": d1, "5m": d5}})
            self.assertGreater(len(res["rows"]), 50)
            self.assertFalse(db.latest_performance().empty)
            db.close()
            Path(res["csv"]).unlink()


if __name__ == "__main__":
    unittest.main()
