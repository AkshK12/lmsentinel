"""Scrape -> hash -> diff -> store -> notify, for the account configured on this laptop."""
from __future__ import annotations

import asyncio
import json
import logging
import random

from playwright.async_api import Browser, async_playwright

from .client import LMSClient, LoginError, ScrapeResult
from .commands import CommandHandler
from .config import Settings
from .diff import Change, diff_grids, hash_grids
from .notifier import Notifier, TelegramNotifier
from .store import Account, Store

log = logging.getLogger(__name__)


def apply_result(store: Store, user_id: str, result: ScrapeResult) -> list[Change]:
    baseline = not store.has_any_snapshot(user_id)
    changes: list[Change] = []
    for subj in result.subjects:
        h = hash_grids(subj.grids)
        prev = store.get_snapshot(user_id, result.term, subj.name)
        if prev is None:
            if not baseline:
                changes.append(Change(subj.name, "new_subject", subj.name))
                changes += diff_grids(subj.name, [], subj.grids)
        elif prev[0] != h:
            changes += diff_grids(subj.name, prev[1], subj.grids)
        if prev is None or prev[0] != h:
            store.save_snapshot(user_id, result.term, subj.name, h, subj.grids)
    if changes:
        store.log_changes(user_id, result.term, changes)
    if baseline:
        log.info("Baseline saved (%d subjects); future changes will be reported", len(result.subjects))
    return changes


async def launch_browser(pw, settings: Settings) -> Browser:
    """BROWSER_CHANNEL=chrome uses an installed Google Chrome (preinstalled on GitHub runners)."""
    if settings.browser_channel:
        try:
            return await pw.chromium.launch(headless=settings.headless, channel=settings.browser_channel)
        except Exception as e:
            log.warning("Browser channel %r unavailable (%s); using bundled Chromium", settings.browser_channel, e)
    return await pw.chromium.launch(headless=settings.headless)


async def deliver_pending(store: Store, account: Account, notifier: Notifier) -> None:
    """Send every not-yet-delivered change; if sending fails they stay pending for the next check."""
    rows = store.unnotified(account.user_id)
    by_term: dict[str, list] = {}
    for r in rows:
        by_term.setdefault(r["term"], []).append(r)
    for term, items in by_term.items():
        changes = [Change(**json.loads(r["detail"])) for r in items]
        if await notifier.notify(account, term, changes):
            store.mark_notified([r["id"] for r in items])


async def check_account(browser: Browser, settings: Settings, store: Store, account: Account,
                        notifier: Notifier, recorder=None) -> list[Change]:
    async with LMSClient(browser, settings, recorder) as client:
        result = await client.scrape(account.user_id, account.password)
    changes = apply_result(store, account.user_id, result)
    if not changes:
        log.info("No changes (%d subjects checked)", len(result.subjects))
    await deliver_pending(store, account, notifier)
    return changes


async def run_cycle(browser: Browser, settings: Settings, store: Store, account: Account,
                    notifier: Notifier) -> bool:
    """True if the check succeeded (after at most 2 attempts)."""
    for attempt in (1, 2):
        try:
            await check_account(browser, settings, store, account, notifier)
            return True
        except LoginError:
            raise  # never retry a bad password: avoids locking the account
        except Exception as e:
            log.warning("Attempt %d failed: %r", attempt, e)
            if attempt == 1:
                await asyncio.sleep(10)
    log.error("Check failed; nothing was saved this time")
    return False


async def watch(settings: Settings, store: Store, account: Account, notifier: Notifier,
                once: bool = False) -> bool:
    """Loop forever, or with once=True run a single check and return whether it succeeded.
    With Telegram configured, /showmarks etc. are answered: after the check in `once` mode
    (cloud), or within seconds by a background listener in `watch` mode (laptop)."""
    commands = None
    if isinstance(notifier, TelegramNotifier):
        commands = CommandHandler(notifier.token, notifier.chat_id, store, account.user_id)
    listener = None
    async with async_playwright() as pw:
        browser = await launch_browser(pw, settings)
        try:
            if commands and not once:
                listener = asyncio.create_task(commands.listen())
            while True:
                if not browser.is_connected():
                    browser = await launch_browser(pw, settings)
                ok = await run_cycle(browser, settings, store, account, notifier)
                if once:
                    if commands:
                        try:
                            await commands.poll_once()
                        except Exception as e:  # never fail the check because of a chat command
                            log.warning("Could not process Telegram commands: %s", e)
                    return ok
                delay = settings.poll_interval + random.uniform(0, settings.poll_jitter)
                log.info("Next check in %.0f s", delay)
                await asyncio.sleep(delay)
        finally:
            if listener:
                listener.cancel()
            await browser.close()
