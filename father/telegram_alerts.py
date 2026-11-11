"""Telegram Bot API (free, long polling - no webhook, no server needed).

If TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are missing, messages are printed to the console.
"""
from __future__ import annotations

import html
import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("father.telegram")
MAX_LEN = 4000


def format_signal(sig: dict) -> str:
    """Exact layout from the project spec (section 9)."""
    lines = [
        "FATHER SIGNAL BOT",
        "",
        f"{sig['direction']} SIGNAL",
        "",
        f"Coin: {sig['symbol']}",
        "Exchange: Binance Futures",
        f"Entry Timeframe: {sig['entry_tf'].upper()}",
        f"Confirmation: {sig['confirm_tf'].upper()}",
        f"Pattern: {sig['pattern']}",
        f"Volume: {'Confirmed' if sig['volume_confirmed'] else 'Not confirmed'}",
        f"Trend: {sig['trend']}",
        f"RSI: {sig['rsi']:.1f}",
        f"Signal Score: {sig['score']:.0f}/100",
        "",
        "Reasons:",
        "",
    ]
    lines += [f"- {r}" for r in sig["reasons"]]
    lines += ["", "Status: Analysis Only."]
    return "\n".join(lines)


def pre(text: str) -> str:
    """Wrap text in an HTML <pre> block (monospace tables)."""
    return f"<pre>{html.escape(text)}</pre>"


def _split_lines(text: str, limit: int) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def _chunks(text: str, html_mode: bool) -> list[str]:
    """Split long messages on line breaks; keep <pre> blocks valid in every chunk."""
    if len(text) <= MAX_LEN:
        return [text]
    if html_mode and text.startswith("<pre>") and text.endswith("</pre>"):
        return [f"<pre>{c}</pre>" for c in _split_lines(text[5:-6], MAX_LEN - 20)]
    return _split_lines(text, MAX_LEN)


class Telegram:
    def __init__(self, token: str, chat_id: str, timeout: float = 15, retries: int = 4):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def can_poll(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _call(self, method: str, data: dict | None = None, files: dict | None = None,
              timeout: float | None = None) -> dict | None:
        delay = 2.0
        for attempt in range(1, self.retries + 1):
            try:
                r = self.session.post(self._url(method), data=data, files=files, timeout=timeout or self.timeout)
                j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                if r.status_code == 200 and j.get("ok"):
                    return j
                if r.status_code == 429:
                    wait = float(j.get("parameters", {}).get("retry_after", 5))
                    time.sleep(wait + 0.5)
                    continue
                if 400 <= r.status_code < 500:
                    log.error("Telegram %s failed: HTTP %s %s", method, r.status_code, str(j)[:200])
                    return None
            except (requests.ConnectionError, requests.Timeout, ValueError) as e:
                log.warning("Telegram %s network error (attempt %d): %s", method, attempt, e)
            time.sleep(delay)
            delay = min(delay * 2, 30)
        return None

    def send(self, text: str, html_mode: bool = False, chat_id: str | None = None,
             reply_markup: dict | None = None) -> bool:
        target = chat_id or self.chat_id
        if not (self.token and target):
            print("\n" + "=" * 40 + "\n" + text + "\n" + "=" * 40)
            return False
        ok = True
        for chunk in _chunks(text, html_mode):
            data: dict[str, Any] = {"chat_id": target, "text": chunk, "disable_web_page_preview": "true"}
            if html_mode:
                data["parse_mode"] = "HTML"
            if reply_markup is not None:
                data["reply_markup"] = json.dumps(reply_markup)
            ok = self._call("sendMessage", data) is not None and ok
        return ok

    def send_document(self, path: str | Path, caption: str = "", chat_id: str | None = None) -> bool:
        target = chat_id or self.chat_id
        if not (self.token and target):
            print(f"[document] {path} {caption}")
            return False
        with open(path, "rb") as fh:
            return self._call("sendDocument", {"chat_id": target, "caption": caption[:1000]},
                              files={"document": (Path(path).name, fh)}, timeout=60) is not None

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        if not self.token:
            return []
        data = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            data["offset"] = offset
        try:
            r = self.session.post(self._url("getUpdates"), data=data, timeout=timeout + 10)
            j = r.json()
            if j.get("ok"):
                return j.get("result", [])
            log.error("getUpdates error: %s", str(j)[:200])
            if r.status_code == 409:  # webhook set or another instance polling
                time.sleep(10)
        except (requests.ConnectionError, requests.Timeout, ValueError) as e:
            log.warning("getUpdates network error: %s", e)
            time.sleep(5)
        return []

    def delete_webhook(self) -> None:
        if self.token:
            self._call("deleteWebhook", {"drop_pending_updates": "false"})

    def set_menu_button(self, url: str, text: str = "Father App", chat_id: str | None = None) -> bool:
        """Chat menu button that opens the Mini App (menu launches are read-only: no sendData)."""
        target = chat_id or self.chat_id
        if not (self.token and target):
            return False
        mb = {"type": "web_app", "text": text, "web_app": {"url": url}}
        return self._call("setChatMenuButton", {"chat_id": target, "menu_button": json.dumps(mb)}) is not None

    def set_commands(self, commands: list[tuple[str, str]]) -> bool:
        if not self.token:
            return False
        cmds = [{"command": c, "description": d} for c, d in commands]
        return self._call("setMyCommands", {"commands": json.dumps(cmds)}) is not None

    def get_me(self) -> dict | None:
        j = self._call("getMe") if self.token else None
        return j.get("result") if j else None
