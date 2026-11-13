#!/usr/bin/env bash
# One-click start for macOS/Linux: installs on first run, asks for your login once, then keeps checking.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    echo "Installing (first run only)..."
    python3 -m venv .venv
    .venv/bin/python -m pip install -q -r requirements.txt
    .venv/bin/python -m playwright install chromium
fi
if ! .venv/bin/python -c "from lms_scraper.config import get_account; raise SystemExit(0 if get_account() else 1)"; then
    .venv/bin/python -m lms_scraper setup
fi
if ! grep -qE '^TELEGRAM_CHAT_ID=.+' .env 2>/dev/null; then
    read -r -p "Set up Telegram alerts now? [Y/n] " ans
    case "$ans" in [nN]*) ;; *) .venv/bin/python -m lms_scraper telegram ;; esac
fi
exec .venv/bin/python -m lms_scraper watch
