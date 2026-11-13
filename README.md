# LMSentinel

Get a Telegram message the moment new marks show up on the Thapar (TIET) LMS, `campus.thapar.edu`.

> Unofficial student project, not affiliated with or endorsed by Thapar Institute. It only reads
> *your own* marks with *your own* login, at a gentle 15-minute interval. Use at your own risk.

**Your login stays yours.** It goes only to campus.thapar.edu (and, in cloud mode, into secrets
of *your own* GitHub repo). It is never sent to the author or anyone else. Alerts come from
*your own* Telegram bot.

There are two ways to run it. Pick one:

| | Cloud (GitHub Actions) | Laptop |
|---|---|---|
| Works when laptop is off | ✅ | ❌ |
| Checks | every ~15 min (GitHub may delay a few min) | every 15 min while running |
| Needs | a GitHub account | Python 3.10+ on Windows / macOS / Linux |

---

## Step 0: make your Telegram bot (both modes, 2 minutes)
1. In Telegram, open **@BotFather**, send `/newbot`, pick any name, and copy the **token**.
2. Find your chat ID. The easiest way is to run `python -m lms_scraper telegram` on any computer
   with this project: it asks for the token, you press **Start** in your bot, and it prints your
   chat ID and sends a test message. (Manual alternative: message your bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`.)

## Option A: Cloud (runs every 15 min even with your laptop off)
1. On [github.com/AkshK12/lmsentinel](https://github.com/AkshK12/lmsentinel) click **Use this template → Create a new repository**
   (this gives you your own copy; a fork won't run schedules by default).
2. In *your* new repo: **Settings → Secrets and variables → Actions → New repository secret**, and add:
   - `LMS_USERID`: your Thapar email
   - `LMS_PASSWORD`: your LMS password
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. **Actions** tab → enable workflows → **Check marks → Run workflow** to test it once.
   The first run saves your current marks; after that you get a Telegram message whenever they change.

Notes:
- Secrets are only readable by your repo's workflows. Logs never print marks, and the saved
  marks are cached **encrypted** with your LMS password.
- **Public repo:** Actions minutes are free and unlimited. GitHub pauses schedules after 60 days
  with no repo activity; it emails you first, and one click re-enables it.
- **Private repo:** free accounts get 2,000 Actions minutes/month (3,000 with the GitHub Student
  Pack), and each check costs about 1–2 minutes. Change the cron in
  `.github/workflows/check-marks.yml` to `*/30 * * * *` or `0 * * * *` to stay within that.
- Changed your LMS password? Update the `LMS_PASSWORD` secret.

## Option B: Laptop (Windows / macOS / Linux)
```
git clone https://github.com/AkshK12/lmsentinel.git
cd lmsentinel
```
- **Windows:** double-click `run.bat`
- **macOS / Linux:** `bash run.sh`

The first run installs everything, asks for your Thapar login (tested before saving, stored in
your OS keychain), offers Telegram setup, then keeps checking while the window is open.

### Commands
```
python -m lms_scraper setup      # save / update your login on this laptop
python -m lms_scraper telegram   # connect your Telegram bot
python -m lms_scraper once       # check once
python -m lms_scraper watch      # keep checking (POLL_INTERVAL_SECONDS in .env)
python -m lms_scraper show       # print your stored marks
python -m lms_scraper forget     # delete your saved password from this laptop
python -m lms_scraper recon --headed   # debug: record every page + request to recon/
```

## Telegram commands
Send these to your bot (type `/` in the chat to see the menu):

| Command | Reply |
|---|---|
| `/showmarks` | All your saved marks for the latest term |
| `/showmarks machine` | Only subjects whose name contains "machine" |
| `/help` | The list of commands |

- **Cloud mode:** commands are answered at the end of the next scheduled run (right after a fresh
  check), so the reply can take up to ~15 minutes (plus any GitHub delay).
- **Laptop mode (`watch`):** replies arrive within seconds.
- The bot answers **only your chat** (`TELEGRAM_CHAT_ID`); messages from anyone else are ignored.
- Use either laptop `watch` **or** the cloud workflow for one bot, not both at once.

## Testing alerts without waiting for new marks
```
python -m lms_scraper test-alert        # sends a sample message to your bot
python -m lms_scraper simulate-change   # fakes one old value in your SAVED marks...
python -m lms_scraper once              # ...so this real check sends a real alert and fixes the saved copy
```
In the cloud: **Actions → Check marks → Run workflow**, tick *Test alert*, then Run.
(The cloud needs one normal run first so it has saved marks.)

## How it works
Login → View My Grades → View My Assignments → latest term → each subject (back via *Change Class*
→ same term) → marks grid. Each subject's grid is SHA-256 hashed; when the hash changes it is diffed
row by row (new / updated / removed items) and logged. Changes that fail to send (Telegram down)
are retried on the next check. A wrong password is never retried, so the tool can't lock your account.

## Contributing
`python -m tests.test_offline` runs the full flow against a local mock of the PeopleSoft site.
Never commit `.env`, `data/`, `state/` or `recon/`.
