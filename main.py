#!/usr/bin/env python3
"""FATHER SIGNAL BOT - entry point.

    python main.py            -> starts the bot (Telegram commands + background scanner)

Everything is controlled from Telegram (/help). Analysis only: no trading, no orders, no wallet.
Debug-only fallback:  python main.py --debug once | backtest N D | report | test-telegram
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from father.backtest import pattern_table, run_backtest, summary_text
from father.binance import BinanceFuturesPublic
from father.config import ROOT, TUNABLES, apply_setting, load_config
from father.db import Database
from father.scanner import Scanner
from father.telegram_alerts import Telegram, format_signal
from father.telegram_bot import HELP, CommandBot


def setup_logging(level: str) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fh = RotatingFileHandler(ROOT / "logs" / "father.log", maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers = [fh, sh]
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build():
    cfg = load_config()
    setup_logging(cfg.log_level)
    db = Database(cfg.db_file)
    saved = db.get_settings()
    for k, v in saved.items():  # tunables changed earlier with /set
        if k in TUNABLES:
            try:
                apply_setting(cfg, k, v)
            except ValueError:
                pass
    client = BinanceFuturesPublic(cfg.base_url, cfg.request_min_interval, cfg.max_weight_per_min,
                                  cfg.http_timeout, cfg.http_max_retries)
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    scanner = Scanner(cfg, db, client, tg)
    scanner.enabled = saved.get("scan_enabled", "1") == "1"
    return cfg, db, client, tg, scanner


def run_bot() -> None:
    cfg, db, client, tg, scanner = build()
    log = logging.getLogger("father")
    if not tg.enabled:
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env - the bot is operated from Telegram.")
        log.error("For a console-only test run:  python main.py --debug once")
        sys.exit(2)
    me = None
    for attempt in range(5):  # phone may have no network right after boot
        me = tg.get_me()
        if me:
            break
        log.warning("Telegram not reachable / token rejected (attempt %d/5). Retrying in 20s...", attempt + 1)
        time.sleep(20)
    if not me:
        log.error("Could not reach Telegram. Check TELEGRAM_BOT_TOKEN and your internet.")
        sys.exit(3)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    bot = CommandBot(cfg, db, scanner, tg)
    threading.Thread(target=scanner.run_forever, args=(stop,), daemon=True, name="scanner").start()
    tg.send(f"FATHER SIGNAL BOT started (@{me.get('username')}).\nScanner: {'ON' if scanner.enabled else 'OFF'}"
            f"\nStatus: Analysis Only.\nSend /help for commands.")
    log.info("Bot running as @%s. Press Ctrl+C to stop.", me.get("username"))
    try:
        bot.run_forever(stop)
    finally:
        stop.set()
        tg.send("FATHER SIGNAL BOT stopped.")
        db.close()


def debug(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="main.py --debug", description="Debug commands (normal use: Telegram)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("once", help="run one scan (alerts print to console if Telegram not set)")
    b = sub.add_parser("backtest")
    b.add_argument("symbols", type=int, nargs="?", default=5)
    b.add_argument("days", type=float, nargs="?", default=3)
    sub.add_parser("report")
    sub.add_parser("test-telegram")
    a = p.parse_args(argv)
    cfg, db, client, tg, scanner = build()
    if a.cmd == "once":
        print(scanner.scan_once())
    elif a.cmd == "backtest":
        syms = scanner.refresh_universe()[: a.symbols]
        res = run_backtest(cfg, db, client, syms, a.days, progress=print)
        print(summary_text(res))
        print()
        print(pattern_table(res["perf"]))
        print("\nCSV:", res["csv"])
    elif a.cmd == "report":
        print(pattern_table(db.latest_performance()))
    elif a.cmd == "test-telegram":
        demo = {"direction": "LONG", "symbol": "BTCUSDT", "entry_tf": "1m", "confirm_tf": "5m",
                "pattern": "Bullish Engulfing", "volume_confirmed": True, "trend": "Bullish", "rsi": 55.2,
                "score": 85, "reasons": ["Bullish pattern detected.", "5m trend aligned.", "Volume confirmation.",
                                         "Momentum confirmation."]}
        ok = tg.send("TEST MESSAGE (not a real signal)\n\n" + format_signal(demo))
        print("sent" if ok else "not sent (printed above) - check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    db.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--debug":
        debug(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print(HELP)
    else:
        run_bot()
