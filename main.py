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


HEARTBEAT_FILE = ROOT / "logs" / "heartbeat"


def heartbeat_loop(stop: threading.Event, scanner, bot, client, tg, interval: int = 60, log_every: int = 180) -> None:
    """Writes logs/heartbeat every minute (start.sh watchdog reads it), logs a status line every 3 min,
    restarts the scanner thread if it died, and detects sleeps/pauses of the phone or VM."""
    log = logging.getLogger("father.heartbeat")
    last_tick = time.time()
    last_log = 0.0
    while not stop.is_set():
        try:
            now = time.time()
            gap = now - last_tick
            if gap > interval * 2.5:
                log.warning("Process was paused for %.1f min (phone/VM sleep or no CPU). Resetting network sessions.",
                            gap / 60)
                client.reset_session()
                tg.reset_session()
            last_tick = now
            HEARTBEAT_FILE.write_text(str(int(now)))
            st = getattr(scanner, "thread", None)
            if st is not None and not st.is_alive() and not stop.is_set():
                log.error("Scanner thread died - restarting it")
                scanner.thread = threading.Thread(target=scanner.run_forever, args=(stop,), daemon=True, name="scanner")
                scanner.thread.start()
            if now - last_log >= log_every:
                last_log = now
                ls = f"{int(now - scanner.last_scan_ts)}s ago" if scanner.last_scan_ts else "never"
                log.info("heartbeat: scanner=%s last_scan=%s scan_loop=%ds ago tg_loop=%ds ago coins=%d weight=%s",
                         "ON" if scanner.enabled else "OFF", ls, int(now - scanner.loop_ts), int(now - bot.loop_ts),
                         len(scanner.symbols), client.used_weight)
        except Exception:
            log.exception("heartbeat error")
        stop.wait(interval)


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
    # `kill -USR1 <pid>` writes all thread stacks to logs/threads.txt (hang diagnosis without extra tools)
    try:
        import faulthandler
        _fh = open(ROOT / "logs" / "threads.txt", "a")
        faulthandler.register(signal.SIGUSR1, file=_fh, all_threads=True)
    except (AttributeError, ValueError, OSError):
        pass
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    bot = CommandBot(cfg, db, scanner, tg)
    tg.set_commands([("app", "Open the 📊 Father App"), ("status", "Scanner status"), ("scan_on", "Start scanning"),
                     ("scan_off", "Pause scanning"), ("scan_once", "Run one scan now"),
                     ("backtest", "Backtest: /backtest 5 3"), ("report", "Latest backtest report"),
                     ("signals", "Recent signals"), ("coins", "Coin universe"), ("settings", "Show settings"),
                     ("set", "Change: /set min_score 80"), ("errors", "Recent errors"), ("help", "Help")])
    scanner.thread = threading.Thread(target=scanner.run_forever, args=(stop,), daemon=True, name="scanner")
    scanner.thread.start()
    threading.Thread(target=heartbeat_loop, args=(stop, scanner, bot, client, tg), daemon=True, name="heartbeat").start()
    try:
        bot.send_app_button(f"FATHER SIGNAL BOT started (@{me.get('username')}).\nScanner: {'ON' if scanner.enabled else 'OFF'}"
                            f"\nStatus: Analysis Only.\nTap 📊 Father App below, or send /help.")
    except Exception:
        log.exception("startup message failed (will keep running)")
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
