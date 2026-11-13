"""CLI:  python -m lms_scraper <command>

  setup     save your Thapar login on THIS laptop (password goes to the OS keychain) and test it
  once      check your marks once
  watch     keep checking every POLL_INTERVAL_SECONDS (+ jitter)
  show      print your latest stored marks
  recon     walk the whole flow, recording pages + network requests (for debugging)
  forget    remove your saved password from this laptop
  telegram  connect your own Telegram bot (finds your chat ID automatically, sends a test message)
  test-alert       send a sample "marks update" message through your notifier
  simulate-change  fake an old value in your saved marks so the next `once` sends a real alert
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import getpass
import json
import logging
import shutil
import sys
import time

from dotenv import set_key
from playwright.async_api import async_playwright

from .client import LMSClient, LoginError
from .config import ENV_FILE, KEYRING_SERVICE, ROOT, Settings, get_account
from .diff import Change
from .notifier import ConsoleNotifier, TelegramNotifier, make_notifier, telegram_call
from .recorder import Recorder
from .service import check_account, launch_browser, watch
from .store import Account, Store


def ensure_env_file() -> None:
    if not ENV_FILE.exists():
        shutil.copy(ROOT / ".env.example", ENV_FILE)


def telegram_setup() -> None:
    print("1. In Telegram, open @BotFather, send /newbot and follow the steps.\n"
          "2. Copy the bot token it gives you (looks like 123456:ABC-...).\n")
    token = input("Bot token: ").strip()
    try:
        bot = telegram_call(token, "getMe")
    except Exception as e:
        sys.exit(f"That token didn't work: {e}")
    print(f"\n3. Now open https://t.me/{bot['username']} and press START (or send it any message).")
    print("   Waiting up to 3 minutes...")
    chat_id, offset, deadline = None, None, time.monotonic() + 180
    while chat_id is None and time.monotonic() < deadline:
        params = {"timeout": 20, **({"offset": offset} if offset else {})}
        for u in telegram_call(token, "getUpdates", params, timeout=30):
            offset = u["update_id"] + 1
            chat = (u.get("message") or {}).get("chat")
            if chat:
                chat_id = str(chat["id"])
    if chat_id is None:
        sys.exit("No message received. Run `python -m lms_scraper telegram` again.")
    telegram_call(token, "sendMessage", {"chat_id": chat_id,
                                         "text": "✅ Connected! You'll get a message here whenever your marks change."})
    ensure_env_file()
    set_key(str(ENV_FILE), "TELEGRAM_BOT_TOKEN", token, quote_mode="never")
    set_key(str(ENV_FILE), "TELEGRAM_CHAT_ID", chat_id, quote_mode="never")
    print("\nConnected! A test message was sent to your Telegram. Saved to .env.\n"
          "For GitHub Actions, add these as repository secrets:\n"
          f"  TELEGRAM_BOT_TOKEN = (the token you pasted)\n  TELEGRAM_CHAT_ID   = {chat_id}")


def require_account() -> Account:
    acc = get_account()
    if not acc:
        sys.exit("No login saved on this laptop yet. Run:  python -m lms_scraper setup")
    return Account(*acc)


async def verify_login(settings: Settings, userid: str, password: str) -> None:
    async with async_playwright() as pw:
        browser = await launch_browser(pw, settings)
        try:
            async with LMSClient(browser, settings) as c:
                await c.login(userid, password)
        finally:
            await browser.close()


def setup(settings: Settings) -> None:
    import keyring

    print("Your login is stored only on this laptop (OS keychain). It is never sent anywhere except campus.thapar.edu.\n")
    userid = input("Thapar email / user ID: ").strip()
    password = getpass.getpass("LMS password (hidden): ")
    print("Checking login on campus.thapar.edu ...")
    try:
        asyncio.run(verify_login(settings, userid, password))
    except LoginError as e:
        sys.exit(f"Login failed: {e}\nNothing was saved. Run setup again.")

    ensure_env_file()
    set_key(str(ENV_FILE), "LMS_USERID", userid, quote_mode="never")
    try:
        keyring.set_password(KEYRING_SERVICE, userid, password)
        set_key(str(ENV_FILE), "LMS_PASSWORD", "", quote_mode="never")  # don't keep a plaintext copy
        print("Login OK. Password saved in your OS keychain.")
    except Exception as e:
        set_key(str(ENV_FILE), "LMS_PASSWORD", password, quote_mode="always")
        print(f"Login OK. No OS keychain available ({e}); password saved in .env on this laptop instead.")
    print("\nNext:  python -m lms_scraper telegram   (get alerts on Telegram)\n"
          "Then:  python -m lms_scraper once   (first run saves a baseline)\n"
          "Then:  python -m lms_scraper watch  (keeps checking)")


def test_alert() -> None:
    notifier = make_notifier()
    if not isinstance(notifier, TelegramNotifier):
        sys.exit("Telegram isn't configured. Run:  python -m lms_scraper telegram")
    sample = [
        Change("UCS301 - Data Structures (TEST)", "modified", "Mid Sem", fields=[("Score", "", "24")]),
        Change("UCS301 - Data Structures (TEST)", "added", "Quiz 2", after={"Assignment": "Quiz 2", "Score": "9"}),
    ]
    ok = asyncio.run(notifier.notify(Account("test", ""), "TEST MESSAGE: not real marks", sample))
    print("Sample alert sent; check Telegram." if ok else "Sending failed; see the error above.")


def simulate_change(store: Store) -> None:
    acc = require_account()
    hit = store.simulate_change(acc.user_id)
    if not hit:
        sys.exit("No saved marks yet. Run  python -m lms_scraper once  first.")
    subject, item, field = hit
    print(f"Saved copy altered: [{subject}] '{item}' {field} = '(simulated old value)'.\n"
          "Now run:  python -m lms_scraper once\n"
          "It will log in, see the real value differs, and send a Telegram alert "
          "('(simulated old value)' -> real value). That same run fixes the saved copy.")


def forget() -> None:
    import keyring

    acc = get_account()
    if acc:
        try:
            keyring.delete_password(KEYRING_SERVICE, acc[0])
        except Exception:
            pass
    if ENV_FILE.exists():
        set_key(str(ENV_FILE), "LMS_PASSWORD", "", quote_mode="never")
    print("Saved password removed from this laptop.")


async def recon(settings: Settings, store: Store) -> None:
    acc = require_account()
    recorder = Recorder(ROOT / "recon")
    async with async_playwright() as pw:
        browser = await launch_browser(pw, settings)
        try:
            await check_account(browser, settings, store, acc, ConsoleNotifier(), recorder)
        except Exception as e:
            logging.exception("Recon stopped: %r (pages captured so far are still saved)", e)
        finally:
            await recorder.close()
            await browser.close()
    print(f"\nRecon output: {recorder.dir}\nStart with SUMMARY.md and the *.png screenshots.")


def show(store: Store, user_id: str) -> None:
    rows = store.latest(user_id)
    if not rows:
        print("Nothing stored yet. Run:  python -m lms_scraper once")
    for r in rows:
        print(f"\n### {r['subject']}  (term: {r['term']}, updated {r['updated_at']})")
        for g in json.loads(r["data"]):
            if g.get("title"):
                print(f"  -- {g['title']}")
            print("  " + " | ".join(g["headers"]))
            for row in g["rows"]:
                print("  " + " | ".join(row.get(h, "") for h in g["headers"]))


def main() -> None:
    p = argparse.ArgumentParser(prog="lms_scraper")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("forget")
    sub.add_parser("telegram")
    sub.add_parser("test-alert")
    sub.add_parser("simulate-change")
    sub.add_parser("show")
    for name in ("recon", "once", "watch"):
        sp = sub.add_parser(name)
        sp.add_argument("--headed", action="store_true", help="show the browser window")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    if getattr(args, "headed", False):
        settings = dataclasses.replace(settings, headless=False)

    if args.cmd == "setup":
        return setup(settings)
    if args.cmd == "forget":
        return forget()
    if args.cmd == "telegram":
        return telegram_setup()
    if args.cmd == "test-alert":
        return test_alert()

    store = Store(settings.db_path)
    if args.cmd == "simulate-change":
        simulate_change(store)
    elif args.cmd == "show":
        show(store, require_account().user_id)
    elif args.cmd == "recon":
        asyncio.run(recon(settings, store))
    else:
        try:
            ok = asyncio.run(watch(settings, store, require_account(), make_notifier(), once=args.cmd == "once"))
            if not ok:
                sys.exit(1)  # makes the GitHub Actions run show a red ❌ instead of a false ✅
        except LoginError as e:
            sys.exit(f"Login failed: {e}\nIf you changed your password, run:  python -m lms_scraper setup")


if __name__ == "__main__":
    main()
