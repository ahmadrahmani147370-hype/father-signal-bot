"""Mini App encoding + action mapping tests.  Run: python -m unittest discover -s tests -v"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from father.config import TUNABLES, Config  # noqa: E402
from father.db import Database  # noqa: E402
from father.telegram_bot import CommandBot  # noqa: E402
from father.webapp import (ACTIONS, MAX_URL_LEN, PATTERN_LIST, app_url, b64url,  # noqa: E402
                           b64url_decode, build_payload, parse_action)

ROOT = Path(__file__).resolve().parent.parent


class FakeScanner:
    def __init__(self):
        self.enabled, self.symbols, self.last_scan_ts, self.last_scan_info = True, ["BTCUSDT"] * 54, 1794417000.0, {"alerts": 2}
        self.client = type("C", (), {"used_weight": 10})()
        import threading
        self.scan_lock = threading.Lock()
        self.universe_ts = 0


class FakeTG:
    chat_id = "42"
    enabled = True

    def __init__(self):
        self.sent = []
        self.menu = []

    def send(self, text, html_mode=False, chat_id=None, reply_markup=None):
        self.sent.append((text, reply_markup))
        return True

    def set_menu_button(self, url, text="Father App", chat_id=None):
        self.menu.append(url)
        return True


def big_payload(n=40):
    return {"v": 1, "t": 1794417000, "s": {"on": 1, "n": 100, "ls": 1794417000, "la": 0, "e": 0, "bt": 0},
            "g": [["1000PEPEUSDT", "L", 3, 88, 0.0123456, 0.0121, 0.0127, 1794417000 + i] for i in range(n)],
            "b": {"id": "x", "ts": 1, "pm": "p" * 150, "o": [["1m", "all", 100, 25.5, 0.8, -3.2]] * 10,
                  "p": [["5m", i % 10, 50, 0.9, -0.01, "I"] for i in range(30)]},
            "c": {"min_score": 75}}


class EncodingTests(unittest.TestCase):
    def test_roundtrip(self):
        p = {"a": 1, "txt": "BTCUSDT ✓", "l": [1.5, None]}
        s = b64url(p)
        self.assertNotIn("=", s)
        self.assertNotIn("+", s)
        self.assertNotIn("/", s)
        self.assertEqual(b64url_decode(s), p)

    def test_url_trimmed_under_limit(self):
        url = app_url("https://x.github.io/father-signal-bot/webapp", big_payload(), keyboard=True)
        self.assertLessEqual(len(url), MAX_URL_LEN)
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["k"], ["1"])
        d = b64url_decode(q["d"][0])
        self.assertGreaterEqual(len(d["g"]), 5)
        self.assertEqual(d["s"]["n"], 100)

    def test_menu_url_has_no_keyboard_flag(self):
        url = app_url("https://x.io/app/", {"v": 1}, keyboard=False)
        self.assertNotIn("k=1", url)
        self.assertEqual(app_url("https://x.io/app", None, keyboard=False), "https://x.io/app/")

    def test_payload_from_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            db.save_signal({"key": "k1", "symbol": "ETHUSDT", "direction": "SHORT", "entry_tf": "1m", "confirm_tf": "5m",
                            "pattern": "Tweezer Top", "candle_open_time": 1, "price": 3000.123456, "score": 82.0,
                            "score_class": "Qualified candidate", "volume_confirmed": True, "trend": "Bearish",
                            "rsi": 61.0, "reasons": [], "sent": 1, "atr": 5.0, "sl": 3005.12, "tp": 2992.62})
            p = build_payload(Config(), db, FakeScanner())
            self.assertEqual(p["s"]["n"], 54)
            g = p["g"][0]
            self.assertEqual(g[:4], ["ETHUSDT", "S", PATTERN_LIST.index("Tweezer Top"), 82])
            self.assertEqual(g[5], 3005.12)
            self.assertIn("min_score", p["c"])
            json.dumps(p)  # serialisable
            db.close()

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js_decoder_compatible(self):
        p = big_payload(3)
        s = b64url(p)
        js = ("const s=process.argv[1];let b=s.replace(/-/g,'+').replace(/_/g,'/');while(b.length%4)b+='=';"
              "const bin=atob(b);const u=new Uint8Array([...bin].map(c=>c.charCodeAt(0)));"
              "process.stdout.write(JSON.stringify(JSON.parse(new TextDecoder().decode(u))))")
        out = subprocess.run(["node", "-e", js, s], capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), p)

    def test_js_settings_match_python(self):
        js = (ROOT / "webapp" / "app.js").read_text()
        for k in TUNABLES:
            self.assertIn(f"{k}:", js, k)


class ActionTests(unittest.TestCase):
    def test_simple_actions(self):
        for a in ("scan_on", "scan_off", "scan_once", "refresh", "status", "report"):
            self.assertEqual(parse_action(json.dumps({"a": a})), (a, []))

    def test_backtest_and_set(self):
        self.assertEqual(parse_action('{"a":"backtest","symbols":10,"days":7}'), ("backtest", ["10", "7"]))
        self.assertEqual(parse_action('{"a":"set","key":"min_score","value":80}'), ("set", ["min_score", "80"]))

    def test_rejections(self):
        bad = ['', 'nope', '[]', '{"a":"rm_rf"}', '{"a":"backtest","symbols":500,"days":3}',
               '{"a":"backtest","symbols":"x"}', '{"a":"set","key":"telegram_token","value":1}',
               '{"a":"set","key":"min_score","value":"abc"}', '{"a":"set","key":"min_score","value":"nan"}',
               "x" * 5000]
        for b in bad:
            with self.assertRaises(ValueError, msg=b[:40]):
                parse_action(b)
        self.assertNotIn("trade", ACTIONS)

    def _bot(self, tmp):
        db = Database(Path(tmp) / "t.db")
        tg = FakeTG()
        cfg = Config()
        cfg.webapp_url = "https://x.io/app/"
        return CommandBot(cfg, db, FakeScanner(), tg), tg, db

    def test_web_app_data_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, tg, db = self._bot(tmp)
            upd = {"update_id": 1, "message": {"chat": {"id": 42}, "web_app_data":
                   {"data": '{"a":"set","key":"min_score","value":82}', "button_text": "📊 Father App"}}}
            bot.handle(upd)
            self.assertEqual(bot.cfg.min_alert_score, 82)
            self.assertEqual(db.get_settings()["min_score"], "82.0")
            last_text, markup = tg.sent[-1]
            url = markup["keyboard"][0][0]["web_app"]["url"]
            self.assertTrue(url.startswith("https://x.io/app/?d="))
            self.assertIn("k=1", url)
            self.assertTrue(tg.menu and "k=1" not in tg.menu[-1])
            bot.handle({"update_id": 2, "message": {"chat": {"id": 42}, "web_app_data": {"data": '{"a":"scan_off"}'}}})
            self.assertFalse(bot.scanner.enabled)
            db.close()

    def test_unauthorised_chat_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, tg, db = self._bot(tmp)
            bot.handle({"update_id": 1, "message": {"chat": {"id": 999}, "web_app_data": {"data": '{"a":"scan_off"}'}}})
            self.assertTrue(bot.scanner.enabled)
            self.assertEqual(tg.sent, [])
            db.close()

    def test_rejected_action_replies(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, tg, db = self._bot(tmp)
            bot.handle({"update_id": 1, "message": {"chat": {"id": 42}, "web_app_data": {"data": '{"a":"withdraw"}'}}})
            self.assertIn("rejected", tg.sent[-1][0])
            db.close()

    def test_app_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, tg, db = self._bot(tmp)
            bot.handle({"update_id": 1, "message": {"chat": {"id": 42}, "text": "/app"}})
            self.assertEqual(tg.sent[-1][1]["keyboard"][0][0]["text"], "📊 Father App")
            db.close()


if __name__ == "__main__":
    unittest.main()
