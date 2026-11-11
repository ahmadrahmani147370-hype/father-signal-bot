"""Outcome tracking, 5m pattern confirmation, error reporting, heartbeat.  python -m unittest discover -s tests -v"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from father.backtest import filters_table, run_backtest  # noqa: E402
from father.config import Config  # noqa: E402
from father.db import Database  # noqa: E402
from father.scanner import Scanner  # noqa: E402
from father.strategy import build_events, reasons_for  # noqa: E402
from father.telegram_alerts import format_signal  # noqa: E402
from father.telegram_bot import CommandBot  # noqa: E402
from father.webapp import parse_action  # noqa: E402

T0 = 1_794_000_000_000 - (1_794_000_000_000 % 60_000)


class FakeClient:
    used_weight = 0

    def now_ms(self):
        return T0 + 10 * 86_400_000

    def klines(self, *a, **k):
        raise RuntimeError("offline")

    def reset_session(self):
        pass


class FakeTG:
    chat_id = "42"
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, text, html_mode=False, chat_id=None, reply_markup=None):
        self.sent.append((text, reply_markup))
        return True

    def set_menu_button(self, *a, **k):
        return True


def candles(rows, start=T0):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["open_time"] = start + np.arange(len(df), dtype="int64") * 60_000
    df["close_time"] = df["open_time"] + 59_999
    df["volume"], df["quote_volume"], df["trades"] = 100.0, 1e4, 100
    return df


def signal(db, key, direction, sl, tp, t=T0):
    db.save_signal({"key": key, "symbol": "TESTUSDT", "direction": direction, "entry_tf": "1m", "confirm_tf": "5m",
                    "pattern": "Hammer", "candle_open_time": t, "price": 100.0, "score": 80.0,
                    "score_class": "Qualified candidate", "volume_confirmed": True, "trend": "Bullish",
                    "rsi": 50.0, "reasons": [], "sent": 1, "atr": 1.0, "sl": sl, "tp": tp})


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.cfg = Config()
        self.cfg.max_hold_bars = 5
        self.sc = Scanner(self.cfg, self.db, FakeClient(), FakeTG())

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _run(self, rows, direction="LONG", sl=99.0, tp=101.5):
        self.db.upsert_candles("TESTUSDT", "1m", candles([(100, 100, 100, 100)] + rows))
        signal(self.db, "k", direction, sl, tp)
        self.sc.track_outcomes()
        return self.db.query_df("SELECT outcome, ret_pct, entry_price FROM signals").iloc[0]

    def test_tp(self):
        r = self._run([(100, 100.5, 99.5, 100.2), (100.2, 101.6, 100.1, 101.4)])
        self.assertEqual(r.outcome, "TP")
        self.assertEqual(r.entry_price, 100)
        self.assertAlmostEqual(r.ret_pct, (101.5 * 0.9998 / (100 * 1.0002) - 1) * 100 - 0.1, places=3)

    def test_sl_first_when_both_in_same_candle(self):
        r = self._run([(100, 102, 98.5, 100)])
        self.assertEqual(r.outcome, "SL")
        self.assertLess(r.ret_pct, 0)

    def test_short_and_time_stop(self):
        r = self._run([(100, 100.2, 99.8, 100)] * 5, direction="SHORT", sl=101, tp=98)
        self.assertEqual(r.outcome, "TIME")

    def test_still_open(self):
        r = self._run([(100, 100.2, 99.8, 100)] * 2)
        self.assertTrue(pd.isna(r.outcome))
        st = self.db.outcome_stats()
        self.assertEqual((st["closed"], st["pending"]), (0, 1))

    def test_stats(self):
        self._run([(100, 101.6, 99.9, 101)])
        st = self.db.outcome_stats()
        self.assertEqual((st["closed"], st["tp"], st["wins"]), (1, 1, 1))


class HtfPatternTests(unittest.TestCase):
    def test_5m_pattern_bonus_and_reason(self):
        rng = np.random.default_rng(5)
        n = 3000
        c = 100 + rng.normal(0, 0.15, n).cumsum()
        o = np.r_[c[0], c[:-1]]
        d1 = candles(list(zip(o, np.maximum(o, c) + 0.1, np.minimum(o, c) - 0.1, c)))
        d1["volume"] = rng.random(n) * 200 + 50
        g = d1.groupby(d1.index // 5)
        d5 = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                           "close": g["close"].last(), "volume": g["volume"].sum(), "open_time": g["open_time"].first(),
                           "close_time": g["close_time"].last(), "quote_volume": 1.0, "trades": 1})
        ev = build_events(d1, Config(), htf=d5, tf="1m")
        self.assertIn("htf_pattern", ev)
        with_p = ev[ev["htf_pattern"] != ""]
        self.assertGreater(len(with_p), 0)
        row = with_p.iloc[0]
        self.assertTrue(any("pattern aligned" in x for x in reasons_for(row)))
        # a 5m pattern must come from a 5m candle that closed at/before the 1m candle (no look-ahead)
        self.assertTrue((ev["htf_close_time"].dropna() <= ev.loc[ev["htf_close_time"].notna(), "close_time"]).all())
        self.assertLessEqual(ev["score"].max(), 100)


class BotRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.tg = FakeTG()
        cfg = Config()
        cfg.webapp_url = "https://x.io/app/"
        self.sc = Scanner(cfg, self.db, FakeClient(), self.tg)
        self.bot = CommandBot(cfg, self.db, self.sc, self.tg)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _wad(self, data):
        self.bot.handle({"update_id": 1, "message": {"chat": {"id": 42}, "web_app_data": {"data": data}}})

    def test_failing_action_reports_error_and_still_sends_button(self):
        def boom(args):
            raise RuntimeError("disk full")
        self.bot.cmd_report = boom
        self._wad('{"a":"report"}')
        texts = [t for t, _ in self.tg.sent]
        self.assertTrue(any("failed" in t and "disk full" in t for t in texts))
        self.assertIsNotNone(self.tg.sent[-1][1])        # fresh app keyboard sent anyway
        err = self.db.recent_errors(5)
        self.assertTrue(err["stage"].str.startswith("telegram:").any())

    def test_failing_command_is_reported(self):
        def boom(args):
            raise ValueError("bad")
        self.bot.cmd_coins = boom
        self.bot.handle({"update_id": 1, "message": {"chat": {"id": 42}, "text": "/coins"}})
        self.assertIn("bad", self.tg.sent[-1][0])

    def test_refresh_replies(self):
        self._wad('{"a":"refresh"}')
        self.assertTrue(any("Refreshing" in t for t, _ in self.tg.sent))

    def test_new_actions_whitelisted(self):
        for a in ("performance", "signals", "errors"):
            self.assertEqual(parse_action('{"a":"%s"}' % a)[0], a)

    def test_performance_and_status_commands(self):
        self.bot.handle({"update_id": 1, "message": {"chat": {"id": 42}, "text": "/performance"}})
        self.assertIn("No tracked alert results", self.tg.sent[-1][0])
        self.bot.handle({"update_id": 2, "message": {"chat": {"id": 42}, "text": "/status"}})
        self.assertIn("heartbeat", self.tg.sent[-1][0])

    def test_scanner_loop_survives_exceptions(self):
        calls = []

        def bad_scan():
            calls.append(1)
            raise RuntimeError("network down")
        self.sc.scan_once = bad_scan
        self.sc.cfg.close_delay_sec = 0
        self.sc.client.now_ms = lambda: int(time.time() * 1000) // 60_000 * 60_000 + 59_990  # next close in ~10ms
        stop = threading.Event()
        t = threading.Thread(target=self.sc.run_forever, args=(stop,), daemon=True)
        t.start()
        time.sleep(0.5)
        self.assertTrue(t.is_alive())
        stop.set()
        t.join(10)
        self.assertGreaterEqual(len(calls), 1)
        self.assertIn("network down", self.sc.last_error)


class ReportTests(unittest.TestCase):
    def test_filters_table_and_format(self):
        rng = np.random.default_rng(3)
        n = 4000
        c = 100 + rng.normal(0, 0.12, n).cumsum()
        o = np.r_[c[0], c[:-1]]
        d1 = candles(list(zip(o, np.maximum(o, c) + 0.1, np.minimum(o, c) - 0.1, c)))
        d1["volume"] = rng.random(n) * 200 + 50
        g = d1.groupby(d1.index // 5)
        d5 = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                           "close": g["close"].last(), "volume": g["volume"].sum(), "open_time": g["open_time"].first(),
                           "close_time": g["close_time"].last(), "quote_volume": 1.0, "trades": 1})
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            res = run_backtest(Config(), db, None, ["SYN"], 3, data={"SYN": {"1m": d1, "5m": d5}})
            txt = filters_table(res["rows"])
            for k in ("volume:confirmed", "trend:unconfirmed", "regime:range", "volatility:high_vol"):
                self.assertIn(k, txt)
            Path(res["csv"]).unlink()
            db.close()

    def test_section9_exact_lines(self):
        msg = format_signal({"direction": "LONG", "symbol": "BTCUSDT", "entry_tf": "1m", "confirm_tf": "5m",
                             "pattern": "Bullish Engulfing", "volume_confirmed": True, "trend": "Bullish",
                             "rsi": 55.25, "score": 85, "reasons": ["Bullish pattern detected.", "5m trend aligned.",
                                                                    "Volume confirmation.", "Momentum confirmation."]})
        expected = ("FATHER SIGNAL BOT\n\nLONG SIGNAL\n\nCoin: BTCUSDT\nExchange: Binance Futures\nEntry Timeframe: 1M\n"
                    "Confirmation: 5M\nPattern: Bullish Engulfing\nVolume: Confirmed\nTrend: Bullish\nRSI: 55.2\n"
                    "Signal Score: 85/100\n\nReasons:\n\n- Bullish pattern detected.\n- 5m trend aligned.\n"
                    "- Volume confirmation.\n- Momentum confirmation.\n\nStatus: Analysis Only.")
        self.assertEqual(msg, expected)


if __name__ == "__main__":
    unittest.main()
