"""Telegram command interface (long polling with getUpdates - free, no webhook).

Only messages from TELEGRAM_CHAT_ID are accepted. Everyone else is ignored.
"""
from __future__ import annotations

import logging
import threading
import time

import pandas as pd

from .backtest import filters_table, pattern_table, run_backtest, summary_text
from .config import TUNABLES, apply_setting
from .db import Database
from .scanner import Scanner
from .telegram_alerts import Telegram, pre
from .webapp import app_url, build_payload, parse_action

log = logging.getLogger("father.bot")

HELP = """FATHER SIGNAL BOT - commands
(analysis only - no trading, no orders, no wallet)

/status - scanner state, coins, last scan, errors
/scan_on - start automatic scanning (every closed 1m candle)
/scan_off - pause automatic scanning
/scan_once - run one scan now
/backtest [symbols] [days] - e.g. /backtest 5 3  (top 5 coins, 3 days)
/report - latest backtest pattern results
/signals [n] - last n signals (default 10)
/performance - tracked results of past alerts (TP/SL/time)
/coins - current coin universe
/settings - show tunable settings
/set key value - e.g. /set min_score 80
/errors - recent scanner errors
/app - open the 📊 Father App (Telegram Mini App)
/help - this message

Signal Score = engineering score (how many checks passed), NOT a probability or win rate."""


def _t(ms) -> str:
    """Pakistan time (UTC+5) for display."""
    if ms is None or pd.isna(ms):
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ms) / 1000 + 5 * 3600)) + " PKT"


class CommandBot:
    def __init__(self, cfg, db: Database, scanner: Scanner, tg: Telegram):
        self.cfg, self.db, self.scanner, self.tg = cfg, db, scanner, tg
        self.offset: int | None = None
        self.bt_thread: threading.Thread | None = None
        self.started = time.time()
        self.loop_ts = time.time()

    # ---------------------------------------------------------------- polling
    def run_forever(self, stop: threading.Event) -> None:
        self.tg.delete_webhook()
        log.info("Telegram command loop started")
        while not stop.is_set():
            self.loop_ts = time.time()
            try:
                updates = self.tg.get_updates(self.offset, timeout=25)
            except Exception:  # never let the command loop die
                log.exception("getUpdates crashed; retrying in 5s")
                stop.wait(5)
                continue
            for upd in updates:
                try:
                    self.offset = int(upd["update_id"]) + 1
                    self.handle(upd)
                except Exception as e:
                    self.report_error("command", e)
        log.info("Telegram command loop stopped")

    def report_error(self, stage: str, e: Exception) -> None:
        """Log + store + tell the user in chat. Never raises."""
        msg = f"{type(e).__name__}: {e}"
        log.exception("%s failed: %s", stage, msg)
        try:
            self.db.log_error(None, "telegram:" + stage, msg)
        except Exception:
            pass
        try:
            self.reply(f"⚠️ {stage} failed: {msg[:300]}")
        except Exception:
            pass

    def reply(self, text: str, html_mode: bool = False) -> None:
        self.tg.send(text, html_mode=html_mode)

    # ------------------------------------------------------------- mini app
    APP_BUTTON = "📊 Father App"

    def app_keyboard(self) -> dict:
        url = app_url(self.cfg.webapp_url, self.payload(), keyboard=True)
        return {"keyboard": [[{"text": self.APP_BUTTON, "web_app": {"url": url}}]],
                "resize_keyboard": True, "is_persistent": True}

    def payload(self) -> dict:
        return build_payload(self.cfg, self.db, self.scanner, bool(self.bt_thread and self.bt_thread.is_alive()))

    def send_app_button(self, text: str = "Tap 📊 Father App below to open the dashboard.") -> bool:
        ok = self.tg.send(text, reply_markup=self.app_keyboard())
        self.update_menu_button()
        return ok

    def update_menu_button(self) -> bool:
        """Menu button = read-only snapshot (menu launches cannot send data back)."""
        try:
            return self.tg.set_menu_button(app_url(self.cfg.webapp_url, self.payload(), keyboard=False))
        except Exception as e:  # never break the bot because of the menu
            log.warning("setChatMenuButton failed: %s", e)
            return False

    def handle_web_app_data(self, data: str) -> None:
        try:
            cmd, args = parse_action(data)
        except ValueError as e:
            log.warning("Rejected Mini App data: %s", e)
            self.reply(f"Mini App action rejected: {e}")
            return
        log.info("Mini App action received: %s %s", cmd, args)
        ok = True
        try:
            if cmd == "refresh":
                self.reply("↻ Refreshing app data...")
            else:
                getattr(self, "cmd_" + cmd)(args)
        except Exception as e:
            ok = False
            self.report_error(f"Mini App action '{cmd}'", e)
        finally:
            try:
                self.send_app_button(("✅ Done. " if ok else "") + "Fresh data loaded. Tap 📊 Father App to open it.")
            except Exception as e:
                self.report_error("app button", e)
        log.info("Mini App action finished: %s ok=%s", cmd, ok)

    def handle(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        wad = msg.get("web_app_data")
        if not text.startswith("/") and not wad:
            return
        if chat != self.tg.chat_id:
            log.warning("Ignored message from unauthorised chat %s", chat)
            return
        if wad:
            log.info("Mini App data received (%d bytes)", len(str(wad.get("data", ""))))
            self.handle_web_app_data(str(wad.get("data", "")))
            return
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        fn = getattr(self, "cmd_" + cmd[1:], None)
        log.info("Command received: %s %s", cmd, " ".join(args)[:100])
        if fn is None:
            self.reply("Unknown command. Send /help")
            return
        try:
            fn(args)
        except Exception as e:
            self.report_error(cmd, e)

    # --------------------------------------------------------------- commands
    def cmd_start(self, args):
        self.tg.send(HELP, reply_markup=self.app_keyboard())
        self.update_menu_button()

    cmd_help = cmd_start

    def cmd_app(self, args):
        self.send_app_button("📊 Father App - dashboard, signals, backtest and settings.\n"
                             "Use the keyboard button below (the menu button opens a read-only view).")

    def cmd_status(self, args):
        s = self.scanner
        info = s.last_scan_info or {}
        errs = self.db.count_errors_since(int((time.time() - 3600) * 1000))
        lines = [
            "FATHER SIGNAL BOT - STATUS",
            f"Scanner: {'ON' if s.enabled else 'OFF'}",
            f"Coins in universe: {len(s.symbols)}",
            f"Universe refreshed: {_t(s.universe_ts * 1000) if s.universe_ts else '-'}",
            f"Last scan: {_t(s.last_scan_ts * 1000) if s.last_scan_ts else '-'}",
            f"Last scan: {info.get('detections', 0)} patterns, {info.get('alerts', 0)} alerts, "
            f"{info.get('skipped', 0)} skipped, {info.get('seconds', 0)}s",
            f"API weight used (1m): {s.client.used_weight}",
            f"Errors last hour: {errs}",
            f"Last error: {s.last_error or '-'}",
            f"Backtest running: {'yes' if self.bt_thread and self.bt_thread.is_alive() else 'no'}",
            f"Scanner loop heartbeat: {int(time.time() - s.loop_ts)}s ago",
            f"Tracked alerts: {self.db.outcome_stats().get('closed', 0)} closed, "
            f"{self.db.outcome_stats().get('pending', 0)} open (/performance)",
            f"Uptime: {int((time.time() - self.started) / 60)} min",
            "Mode: Analysis Only",
        ]
        self.reply("\n".join(lines))

    def cmd_scan_on(self, args):
        self.scanner.enabled = True
        self.db.set_setting("scan_enabled", "1")
        self.reply("Scanner ON. It checks every closed 1m candle. Android may pause Termux - keep wake-lock on.")

    def cmd_scan_off(self, args):
        self.scanner.enabled = False
        self.db.set_setting("scan_enabled", "0")
        self.reply("Scanner OFF.")

    def cmd_scan_once(self, args):
        if self.scanner.scan_lock.locked():
            self.reply("A scan is already running, please wait.")
            return
        self.reply("Running one scan...")

        def job():
            try:
                info = self.scanner.scan_once()
                self.tg.send(f"Scan done: {info['coins']} coins, {info['detections']} patterns on last closed candle, "
                             f"{info['alerts']} alerts, {info['skipped']} skipped, {info['errors']} errors, "
                             f"{info['seconds']}s", reply_markup=self.app_keyboard())
            except Exception as e:
                self.report_error("scan_once", e)
        threading.Thread(target=job, daemon=True).start()

    def cmd_backtest(self, args):
        if self.bt_thread and self.bt_thread.is_alive():
            self.reply("A backtest is already running.")
            return
        try:
            n = int(args[0]) if args else 5
            days = float(args[1]) if len(args) > 1 else 3
        except ValueError:
            self.reply("Usage: /backtest [symbols] [days]  e.g. /backtest 5 3")
            return
        n, days = max(1, min(n, 100)), max(0.5, min(days, 30))
        self.reply(f"Backtest started: top {n} coins, {days:g} days of 1m + 5m. This can take a while on a phone.")

        def job():
            try:
                syms = self.scanner.refresh_universe()[:n]
                if not syms:
                    self.reply("No coins in universe.")
                    return
                last = [0.0]

                def prog(m):
                    if time.time() - last[0] > 60:
                        last[0] = time.time()
                        self.reply(m)
                res = run_backtest(self.cfg, self.db, self.scanner.client, syms, days, progress=prog)
                self.reply(pre(summary_text(res)), html_mode=True)
                self.reply(pre(pattern_table(res["perf"])), html_mode=True)
                self.reply(pre(filters_table(res["rows"])), html_mode=True)
                self.tg.send_document(res["csv"], caption=f"Backtest {res['run_id']} - full results CSV")
                self.send_app_button("Backtest saved. Open 📊 Father App → Backtest tab.")
            except Exception as e:
                self.report_error("backtest", e)
        self.bt_thread = threading.Thread(target=job, daemon=True)
        self.bt_thread.start()

    def cmd_report(self, args):
        perf = self.db.latest_performance()
        if perf.empty:
            self.reply("No backtest yet. Run /backtest 5 3")
            return
        self.reply(pre(f"Run {perf['run_id'].iloc[0]} ({_t(perf['updated_at'].iloc[0])})\n" + pattern_table(perf)),
                   html_mode=True)
        self.reply(pre(filters_table(self.db.latest_backtest())), html_mode=True)

    def cmd_performance(self, args):
        st = self.db.outcome_stats()
        if not st.get("closed"):
            self.reply(f"No tracked alert results yet ({st.get('pending', 0)} still open). "
                       f"Each alert is followed until its TP / SL reference level or {self.cfg.max_hold_bars} candles.")
            return
        lines = ["TRACKED ALERT RESULTS (analysis only)",
                 "Entry next candle open, ATR TP/SL reference levels, fees + slippage included.",
                 f"Closed: {st['closed']}  (TP {st['tp']} / SL {st['sl']} / time {st['time']})  open: {st['pending']}",
                 f"Wins/Losses: {st['wins']}/{st['losses']}  PF: {st['profit_factor'] if st['profit_factor'] is not None else '-'}",
                 f"Net: {st['net']:+.2f}%  Avg/alert: {st['avg']:+.3f}%  (1x, summed)", "", "By side:"]
        lines += [f"  {k}: {v[0]} alerts, net {v[1]:+.2f}%" for k, v in st["by_side"].items()]
        lines += ["By pattern:"] + [f"  {k}: {v[0]}, net {v[1]:+.2f}%" for k, v in
                                     sorted(st["by_pattern"].items(), key=lambda x: -x[1][0])]
        lines += ["", "Small samples mean nothing. Past results do not predict future results."]
        self.reply("\n".join(lines))

    def cmd_signals(self, args):
        n = int(args[0]) if args and args[0].isdigit() else 10
        df = self.db.recent_signals(min(n, 50))
        if df.empty:
            self.reply("No signals yet.")
            return
        lines = [f"Last {len(df)} signals (engineering scores):"]
        for r in df.itertuples():
            lines.append(f"{_t(r.created_at)} {r.symbol} {r.direction} {r.pattern} "
                         f"score {r.score:.0f} RSI {r.rsi:.1f} @ {r.price:g}{'' if r.sent else ' (not sent)'}")
        self.reply("\n".join(lines))

    def cmd_coins(self, args):
        df = self.db.latest_universe()
        if df.empty:
            self.reply("Universe not loaded yet. Try /scan_once")
            return
        lines = [f"{'#':>3} {'Symbol':<14}{'24h Vol $M':>11}{'Spr bps':>8}"]
        for r in df.head(100).itertuples():
            lines.append(f"{r.rank:>3} {r.symbol:<14}{r.quote_volume / 1e6:>11.0f}{r.spread_bps:>8.2f}")
        self.reply(pre(f"Coin universe ({len(df)}) - {_t(df['refreshed_at'].iloc[0])}\n" + "\n".join(lines)),
                   html_mode=True)

    def cmd_settings(self, args):
        lines = ["Settings (change with /set key value):"]
        for k, (attr, _t2, lo, hi, desc) in TUNABLES.items():
            lines.append(f"{k} = {getattr(self.cfg, attr):g}   ({desc}; {lo:g}-{hi:g})")
        self.reply("\n".join(lines))

    def cmd_set(self, args):
        if len(args) != 2:
            self.reply("Usage: /set key value   e.g. /set min_score 80  (see /settings)")
            return
        try:
            v = apply_setting(self.cfg, args[0].lower(), args[1])
        except ValueError as e:
            self.reply(str(e))
            return
        self.db.set_setting(args[0].lower(), v)
        if args[0].lower() in ("max_spread_bps", "min_volume", "universe_size"):
            self.scanner.universe_ts = 0  # rebuild universe on next scan
        self.reply(f"OK: {args[0].lower()} = {v} (saved)")

    def cmd_errors(self, args):
        df = self.db.recent_errors(15)
        if df.empty:
            self.reply("No errors recorded.")
            return
        lines = ["Recent errors:"] + [f"{_t(r.created_at)} {r.symbol or '-'} [{r.stage}] {str(r.error)[:150]}"
                                     for r in df.itertuples()]
        self.reply("\n".join(lines))
