# Money Tracker Telegram Bot

A Telegram bot that tracks everything about your money: accounts and assets, expenses, income, transfers, net worth. It reads receipt photos and bank alerts with AI, sends daily, weekly and monthly reports with charts and AI advice, and has an interactive dashboard inside Telegram.

It works for anyone who opens it. Every Telegram user gets their own separate data, base currency, timezone and report schedule.

## What it does

| Feature | How you use it |
| --- | --- |
| Quick entry | `35 coffee`, `12.5 usd taxi card`, `+3000 salary`, `45 groceries yesterday #trip` |
| Time of each entry | Every entry keeps the time it happened and the time it was filled in. New entries get the current time. Added late? Write `30k taxi yesterday 21:30` (or `9pm`), tap **📅 When → 🕐 Set the time** under an entry in the chat, or pick Date and Time in the app's Add and edit screens. History shows the time on each row, and the entry details say when it was filled in. The CSV export has both |
| Auto-categories | Guessed from keywords, then from your own corrections (it learns), then AI |
| Transfers | `500 bank>cash`, or `1000 usd>bank 1350000` to record the amount you actually received |
| Multi-currency | Log any currency. Daily exchange rates are used for totals in your base currency |
| Receipt photos & payment screenshots | Send a photo or a Click/Payme/bank-app screenshot, or take one with the camera inside the app. With an AI key every line is read and categorized. Without one, free OCR reads the total, date and store: OCR.space (if `OCR_SPACE_API_KEY` is set) with Tesseract as the fallback |
| Bank alerts | Forward a bank SMS or notification text, or have your phone send it automatically (`/connect`) |
| Bank statements | Send a CSV or PDF statement, with the account name as the caption. Duplicates are skipped |
| No double counting | A receipt and a bank alert for the same purchase are matched and counted once |
| Reports | Sent automatically every day, every Sunday and on the 1st of the month, or on demand with `/report week`. You choose the send times (24-hour) in the app. Every report has a line chart of the month so far (vs last month) and a pie chart by category. The monthly report adds bar charts comparing the month with the previous one. AI summary and suggestions when an AI key is set |
| Mini App | The **Open app** button next to the message box (or `/app`) opens a full app inside Telegram. It has an overview with charts, quick add with receipt scan and camera, searchable history with edit and delete, accounts with real-balance corrections, month statements, backups and settings. Swipe left on a row for **Edit**, **Archive** (accounts, work days, notes) and **Delete**. It also has Day / Night / Auto themes and can be added to the home screen |
| Overview | Tap **Expense**, **Income** or **Net worth** to switch what it shows. Pick Day, Week, Month, Year or any **Dates** range (e.g. Nov 1 – Nov 10). An "Other money moves" card shows transfers, balance corrections and debts. A random one of the 99 Names of Allah (Arabic with Uzbek meaning) sits at the top; tap it for another |
| Categories | Create, rename, re-emoji and delete categories in the app. Entries of a deleted category move to another one and are never lost |
| Debts | Money you lent or borrowed, kept apart from net worth: record repayments, edit or delete mistakes. Paid-off debts are archived |
| Work notes | The **Notes** tab records daily work. Each entry has a date, the person you worked for, the address (with a map link), the day's pay, optional hours and extra work (extra hours plus extra pay). Each day shows its average pay per hour. A day with no hours entered counts as 07:00–17:00 (10 h) and is marked as the default. Tick a day when you're paid: it moves to **Received** with the date and is added to income. Each person gets a separate list. A calendar shows the days you worked, a Week / Month card charts your earnings, and the weekly and monthly reports include a work section. Plain notes with colors, pins and search work too. Archived entries are hidden but still counted, and can be restored |
| Ask anything | `? how much did I spend on eating out this month` |
| Net worth | `/networth`. Investments and property use `/value Stocks 15400` |
| Daily records | Every night the bot freezes each account's balance and your net worth. `/networth` shows the change vs 30 days, the start of the year and 1 year ago |
| Month-end statement | `/statement` (or `last`, `2026-10`, `2026`) shows opening vs closing net worth and what moved it: income, spending, savings, and value/exchange-rate changes. Includes a CSV. It's sent automatically on the 1st |
| Edit history | Every new entry, change and deletion is logged with the full record. See `/edits` |
| Backups | `/backup` sends a zip with all of your data: CSV files for Excel (including `work_notes.csv`) plus JSON. A backup is also sent automatically on the 1st of each month, so Telegram keeps a copy in your chat |
| SQL tools | The data lives in PostgreSQL (Neon). Connect DBeaver, pgAdmin, DataGrip, Excel or Power BI with the connection details from the Neon console. Main tables: `transactions`, `accounts`, `categories`, `work_notes`, `snapshots`, `snapshot_lines`. MySQL Workbench can't connect to PostgreSQL, so import the backup CSVs into it instead |
| Export / privacy | `/export` downloads a CSV. `/deleteme` wipes all of your data |

## Setup (about 15 minutes)

To put it online for free, follow **[DEPLOY.md](DEPLOY.md)**: Render + Neon (easiest, HTTPS and the Mini App work automatically) or Oracle Cloud Always Free (24/7, phone-friendly guide in **[DEPLOY-ORACLE.md](DEPLOY-ORACLE.md)**).

1. **Create the bot.** In Telegram, open @BotFather, send `/newbot`, and copy the token.
2. **Optional: an AI key** at https://console.anthropic.com (prepaid credit; separate from a Claude Pro subscription). Without it the bot still works, and receipts are read with free OCR; AI alerts, AI advice and questions are off.
3. **Get the code and configure.** Run `git clone https://github.com/akoone/Financial-Audit.git finance-bot && cd finance-bot`, then `cp .env.example .env`, and fill in `BOT_TOKEN` (plus `ANTHROPIC_API_KEY` and `DATABASE_URL` if you use them).
4. **Run it with one of these options:**
   - **Docker (recommended on a server):** `docker compose up -d --build` starts the bot with PostgreSQL.
   - **Plain Python (for trying it out):**
     ```bash
     python3 -m venv .venv && . .venv/bin/activate
     pip install -r requirements.txt
     python -m app.main          # uses SQLite in ./data
     ```
5. **Start using it.** Open your bot in Telegram and send `/start`. Pick a base currency, then set your timezone with `/tz Asia/Seoul` (or your own).

### Mini App and automatic bank alerts

On Render both work automatically. On your own server they need the bot's web server (port 8080) to be reachable over **HTTPS**:

- On a VPS, put a domain in front of it with Caddy, e.g. `bot.example.com { reverse_proxy localhost:8080 }`. Caddy adds HTTPS automatically.
- For quick testing, use `cloudflared tunnel --url http://localhost:8080`.

Set `WEBAPP_URL=https://bot.example.com` in `.env` and restart. Then:

- The **Open app** menu button, `/app` and the button under each report open the Mini App.
- `/connect` gives each user a private link. Their phone posts bank alerts to it:
  - **iPhone:** Shortcuts → Automation → Message from your bank → Get Contents of URL (POST, body = message).
  - **Android:** an SMS forwarder app or Tasker, sending to a webhook.

## Keeping your records for years

- **Database.** Use PostgreSQL, either Neon or the bundled one with Docker. All money is stored as exact decimals, never rounded floats. New versions add their new columns automatically on start, without touching existing data.
- **Three independent copies.** The database itself, the monthly backup zip in your Telegram chat, and (with Docker) the nightly `pg_dump` from DEPLOY-ORACLE.md. If one is lost, the others remain.
- **Frozen history.** Nightly snapshots record what every account held each day, in its own currency and in your base currency at that day's exchange rate. Old statements therefore never change, even when rates or later edits change today's numbers.
- **Readable without the bot.** The backup is plain CSV and JSON, so you can open it in Excel or import it anywhere, years from now.

## Running it publicly: costs and responsibility

- **AI costs.** AI calls are paid by whoever owns `ANTHROPIC_API_KEY`. `AI_DAILY_LIMIT` (default 40) caps calls per user per day. Scheduled reports don't count toward it. `AI_MODEL` defaults to `claude-opus-5-5`, and you can set a cheaper model there.
- **Making it private.** Set `ALLOWED_USER_IDS=111,222` to limit the bot to friends or family.
- **Storing other people's data.** The server owner holds everyone's financial records. Back up the database, and keep the server patched. The bot never asks for bank passwords or card numbers. Users can wipe their data at any time with `/deleteme`.
- **What goes to the AI service.** Receipt images, alert texts and summary numbers are sent to the AI API to be processed. Reports and questions send totals per category, not the full transaction history.

## Project layout

```
app/
  main.py        starts the bot (polling, or webhook on Render), scheduler and web server
  bot.py         Telegram commands, quick entry, receipts, statements, edit buttons
  parser.py      quick-entry grammar
  services.py    accounts, transactions, transfers, balances, net worth, stats, CSV export
  categories.py  default categories and keywords
  importers.py   bank alert saving with de-duplication, CSV/PDF statement import
  ai.py          Claude API: receipts, alerts, statements, categories, insights, questions
  reports.py     report text (money and work) + AI advice
  charts.py      report chart images: month-so-far line, pie, monthly bars, work chart
  ocr.py         free receipt reading: OCR.space, then Tesseract
  scheduler.py   per-user report times in their timezone
  webapp.py      Mini App API (Telegram initData verified), /ingest bank alerts, Telegram webhook
  static/app.html  the Mini App (one page, Telegram theme colors, ECharts)
  static/names99.json  the 99 Names of Allah shown on the overview
  fx.py          daily exchange rates (open.er-api.com), cached in the DB
  models.py      database schema
tests/           parser, money logic, imports, reports, full bot conversation (pytest)
```

Run the tests with `pip install -r requirements-dev.txt && pytest`.

## Next steps (v2 ideas)

Budgets with alerts, recurring payments (rent, subscriptions), savings goals, open banking where your country allows it, and replies in more languages (Uzbek, Russian).

Keeping the service awake on Render's free plan: the bot pings its own `/health` every 10 minutes; add an outside ping such as cron-job.org (see DEPLOY.md). On Oracle Cloud none of this is needed.
