"""Telegram commands answered from the SAVED marks.

  /showmarks            all subjects of the latest term
  /showmarks <text>     only subjects whose name contains <text>  (e.g. /showmarks machine)
  /help                 list commands

Only messages from YOUR chat id (TELEGRAM_CHAT_ID) are answered; anyone else is ignored.

Cloud mode (GitHub Actions `once`): commands are answered at the end of the next scheduled run,
right after a fresh check, so the reply can take up to ~15 minutes.
Laptop mode (`watch`): a listener long-polls Telegram and replies within seconds.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging

from .diff import _key_col
from .notifier import chunk_lines, telegram_call
from .store import Store

log = logging.getLogger(__name__)

HELP = ("<b>LMSentinel commands</b>\n"
        "/showmarks: your saved marks for the latest term\n"
        "/showmarks <i>name</i>: only subjects containing <i>name</i> (e.g. /showmarks machine)\n"
        "/help: this message\n\n"
        "Marks come from the last check (every ~15 min). New or changed marks are sent automatically.")
BOT_COMMANDS = [{"command": "showmarks", "description": "Show my saved marks"},
                {"command": "help", "description": "How to use this bot"}]


def format_marks(rows, query: str = "") -> list[str]:
    """rows = Store.latest(): one row per (term, subject). Shows the most recently updated term."""
    if not rows:
        return ["No marks saved yet. They appear after the first successful check."]
    term = max(rows, key=lambda r: r["updated_at"])["term"]
    rows = [r for r in rows if r["term"] == term]
    if query:
        rows = [r for r in rows if query.lower() in r["subject"].lower()]
        if not rows:
            return [f"No subject matches <b>{html.escape(query)}</b>. Try /showmarks without a name."]
    lines = [f"📚 <b>Your marks</b> ({html.escape(term)})"]
    for r in rows:
        lines += ["", f"<b>{html.escape(r['subject'])}</b>"]
        shown = 0
        for g in json.loads(r["data"]):
            key = _key_col(g["headers"])
            for row in g["rows"]:
                vals = [f"{h}: {v}" for h, v in row.items() if h != key and v]
                name = row.get(key, "") or "(item)"
                lines.append(f"• {html.escape(name)}" + (f": {html.escape(', '.join(vals))}" if vals else ""))
                shown += 1
        if not shown:
            lines.append("• (no marks uploaded yet)")
    newest = max(r["updated_at"] for r in rows)
    lines += ["", f"<i>Last change saved: {newest.replace('T', ' ')[:16]} UTC</i>"]
    return chunk_lines(lines)


class CommandHandler:
    def __init__(self, token: str, chat_id: str, store: Store, user_id: str, api=telegram_call):
        self.token, self.chat_id = token, str(chat_id)
        self.store, self.user_id, self.api = store, user_id, api
        self.offset: int | None = None
        self._menu_set = False

    async def _call(self, method: str, params: dict, timeout: int = 30):
        return await asyncio.to_thread(self.api, self.token, method, params, timeout)

    async def _reply(self, text: str) -> None:
        await self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                         "disable_web_page_preview": True})

    async def _ensure_menu(self) -> None:
        """Register the '/' command menu shown in Telegram (once per process; harmless if it fails)."""
        if not self._menu_set:
            self._menu_set = True
            try:
                await self._call("setMyCommands", {"commands": BOT_COMMANDS})
            except Exception as e:
                log.info("Could not set bot command menu: %s", e)

    async def handle(self, text: str) -> None:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower() if parts else ""   # "/showmarks@MyBot" -> "/showmarks"
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/showmarks", "/marks"):
            for chunk in format_marks(self.store.latest(self.user_id), arg):
                await self._reply(chunk)
        elif cmd in ("/help", "/start"):
            await self._reply(HELP)
        elif cmd.startswith("/"):
            await self._reply("Unknown command. Send /help to see what I can do.")

    async def poll_once(self, wait: int = 0) -> int:
        """Fetch new messages (waiting up to `wait` s), answer commands, return how many were handled."""
        await self._ensure_menu()
        params = {"timeout": wait, "allowed_updates": ["message"]}
        if self.offset is not None:
            params["offset"] = self.offset
        updates = await self._call("getUpdates", params, timeout=wait + 15)
        handled = 0
        for u in updates:
            self.offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                log.warning("Ignored a message from a chat that is not TELEGRAM_CHAT_ID")
                continue
            text = msg.get("text") or ""
            if text.startswith("/"):
                try:
                    await self.handle(text)
                    handled += 1
                except Exception as e:
                    log.error("Could not answer %r: %s", text.split()[0], e)
        if updates and wait == 0:
            # acknowledge what we processed, so the next run doesn't answer it again
            await self._call("getUpdates", {"offset": self.offset, "timeout": 0, "limit": 1})
        if handled:
            log.info("Answered %d Telegram command(s)", handled)
        return handled

    async def listen(self) -> None:
        """Laptop mode: answer commands within seconds, forever (long polling)."""
        log.info("Listening for Telegram commands (/showmarks, /help)")
        while True:
            try:
                await self.poll_once(wait=25)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Telegram command polling failed: %s (retrying in 10 s)", e)
                await asyncio.sleep(10)
