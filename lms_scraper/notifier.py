"""Notifiers. Each student uses their OWN Telegram bot (token + chat ID in their own .env
or GitHub secrets), so nobody else ever sees their marks."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Protocol

from .diff import Change
from .store import Account

log = logging.getLogger(__name__)

IN_CI = os.getenv("GITHUB_ACTIONS") == "true"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 3900  # Telegram limit is 4096 chars per message


class Notifier(Protocol):
    async def notify(self, account: Account, term: str, changes: list[Change]) -> bool: ...


def telegram_call(token: str, method: str, params: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(
        TELEGRAM_API.format(token=token, method=method),
        data=json.dumps(params or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            raise RuntimeError(f"Telegram HTTP {e.code}") from None
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "Telegram error"))
    return body["result"]


def format_changes(term: str, changes: list[Change]) -> list[str]:
    """HTML messages grouped by subject, split to fit Telegram's length limit."""
    by_subject: dict[str, list[Change]] = defaultdict(list)
    for c in changes:
        by_subject[c.subject].append(c)
    lines = [f"📢 <b>Marks update</b> ({html.escape(term)})"]
    for subject, items in by_subject.items():
        lines += ["", f"<b>{html.escape(subject)}</b>"]
        lines += [f"• {html.escape(c.describe(), quote=False).replace('-&gt;', '→')}" for c in items]
    return chunk_lines(lines)


def chunk_lines(lines: list[str]) -> list[str]:
    """Pack lines into messages of at most MAX_LEN characters, breaking only between lines."""
    chunks, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > MAX_LEN and cur:
            chunks.append(cur)
            cur = ""
        cur += ln + "\n"
    return chunks + [cur] if cur else chunks


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    async def send(self, text: str) -> None:
        await asyncio.to_thread(telegram_call, self.token, "sendMessage",
                                {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                 "disable_web_page_preview": True})

    async def notify(self, account: Account, term: str, changes: list[Change]) -> bool:
        try:
            for chunk in format_changes(term, changes):
                await self.send(chunk)
            log.info("Sent %d change(s) to Telegram", len(changes))
            return True
        except Exception as e:  # stays 'unnotified' in the DB and is retried next check
            log.error("Telegram send failed (will retry next check): %s", e)
            return False


class ConsoleNotifier:
    async def notify(self, account: Account, term: str, changes: list[Change]) -> bool:
        if IN_CI:  # CI logs may be public: never print marks there
            print(f"{len(changes)} change(s) detected (details hidden in CI logs; configure Telegram)")
            return False
        print(f"\n=== {len(changes)} change(s) ({term}) ===")
        for c in changes:
            print("  " + c.to_text())
        return True


def make_notifier() -> Notifier:
    # tolerate values pasted with quotes/spaces (a common mistake when adding GitHub secrets)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip("'\"").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip("'\"").strip()
    if token and chat:
        return TelegramNotifier(token, chat)
    log.info("Telegram not configured (run: python -m lms_scraper telegram); printing changes to console")
    return ConsoleNotifier()
