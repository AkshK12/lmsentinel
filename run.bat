@echo off
rem One-click start for Windows: installs on first run, asks for your login once, then keeps checking.
cd /d "%~dp0"
if not exist .venv (
    echo Installing (first run only^)...
    python -m venv .venv || goto :nopython
    .venv\Scripts\python -m pip install -q -r requirements.txt
    .venv\Scripts\python -m playwright install chromium
)
.venv\Scripts\python -c "from lms_scraper.config import get_account; raise SystemExit(0 if get_account() else 1)"
if errorlevel 1 .venv\Scripts\python -m lms_scraper setup
findstr /r /c:"^TELEGRAM_CHAT_ID=." .env >nul 2>&1 || (
    choice /m "Set up Telegram alerts now"
    if not errorlevel 2 .venv\Scripts\python -m lms_scraper telegram
)
.venv\Scripts\python -m lms_scraper watch
pause
goto :eof
:nopython
echo Python 3.10+ is required: https://www.python.org/downloads/  (tick "Add python.exe to PATH")
pause
