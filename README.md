# Money Tracker Telegram Bot

A Telegram bot that tracks everything about your money: accounts and assets, expenses, income, transfers, net worth. It reads receipt photos and bank alerts with AI, sends daily, weekly and monthly reports with charts and AI advice, and has an interactive dashboard inside Telegram.

It works for anyone who opens it. Every Telegram user gets their own separate data, base currency, timezone and report schedule.

## What it does

| Feature | How you use it |
| --- | --- |
| Quick entry | `35 coffee`, `12.5 usd taxi card`, `+3000 salary`, `45 groceries yesterday #trip` |
| Auto-categories | Guessed from keywords, then from your own corrections (it learns), then AI |
| Transfers | `500 bank>cash`, or `1000 usd>bank 1350000` to record the amount you actually received |
| Multi-currency | Log any currency. Daily exchange rates are used for totals in your base currency |
| Receipt photos & payment screenshots | Send a photo or a Click/Payme/bank-app screenshot. With an AI key every line is read and categorized; without one, free Tesseract OCR reads the total, date and store |
| Bank alerts | Forward a bank SMS or notification text, or have your phone send it automatically (`/connect`) |
| Bank statements | Send a CSV or PDF statement, with the account name as the caption. Duplicates are skipped |
| No double counting | A receipt and a bank alert for the same purchase are matched and counted once |
| Reports | Sent automatically every day, every Sunday and on the 1st of the month, or on demand with `/report week`. Each report has a chart, an AI summary and suggestions |
| Dashboard | `/dashboard` opens interactive charts in Telegram: categories (tap to drill down), daily spending, 12-month income vs expenses, accounts |
| Ask anything | `? how much did I spend on eating out this month` |
| Net worth | `/networth`. Investments and property use `/value Stocks 15400` |
| Daily records | Every night the bot freezes each account's balance and your net worth. `/networth` shows the change vs 30 days, the start of the year and 1 year ago |
| Month-end statement | `/statement` (or `last`, `2026-10`, `2026`) shows opening vs closing net worth and what moved it: income, spending, savings, and value/exchange-rate changes. Includes a CSV. It's sent automatically on the 1st |
| Edit history | Every new entry, change and deletion is logged with the full record. See `/edits` |
| Backups | `/backup` sends a zip with all of your data: CSV files for Excel plus JSON. A backup is also sent automatically on the 1st of each month, so Telegram keeps a copy in your chat |
| Export / privacy | `/export` downloads a CSV. `/deleteme` wipes all of your data |

## Setup (about 15 minutes)

For a free server, follow **[DEPLOY.md](DEPLOY.md)** (Oracle Cloud Always Free, step by step).

1. **Create the bot.** In Telegram, open @BotFather, send `/newbot`, and copy the token.
2. **Optional: an AI key** at https://console.anthropic.com (prepaid credit; separate from a Claude Pro subscription). Without it the bot still works, and receipts are read with free OCR; AI alerts, AI advice and questions are off.
3. **Configure.** Run `cp .env.example .env`, then fill in `BOT_TOKEN` and `ANTHROPIC_API_KEY`.
4. **Run it with one of these options:**
   - **Docker (recommended on a server):** `docker compose up -d --build` starts the bot with PostgreSQL.
   - **Plain Python (for trying it out):**
     ```bash
     python3 -m venv .venv && . .venv/bin/activate
     pip install -r requirements.txt
     python -m app.main          # uses SQLite in ./data
     ```
5. **Start using it.** Open your bot in Telegram and send `/start`. Pick a base currency, then set your timezone with `/tz Asia/Seoul` (or your own).

### Dashboard and automatic bank alerts (optional)

Both need the bot's web server (port 8080) to be reachable over **HTTPS**:

- On a VPS, put a domain in front of it with Caddy, e.g. `bot.example.com { reverse_proxy localhost:8080 }`. Caddy adds HTTPS automatically.
- For quick testing, use `cloudflared tunnel --url http://localhost:8080`.

Set `WEBAPP_URL=https://bot.example.com` in `.env` and restart. Then:

- `/dashboard` and the button under each report open the interactive charts.
- `/connect` gives each user a private link. Their phone posts bank alerts to it:
  - **iPhone:** Shortcuts → Automation → Message from your bank → Get Contents of URL (POST, body = message).
  - **Android:** an SMS forwarder app or Tasker, sending to a webhook.

## Keeping your records for years

- **Database.** Use PostgreSQL, either the bundled one with Docker or Neon. All money is stored as exact decimals, never rounded floats.
- **Three independent copies.** The database itself, the monthly backup zip in your Telegram chat, and (with Docker) the daily `pg_dump` from DEPLOY.md. If one is lost, the others remain.
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
  main.py        starts the bot (long polling), scheduler and web server
  bot.py         Telegram commands, quick entry, receipts, statements, edit buttons
  parser.py      quick-entry grammar
  services.py    accounts, transactions, transfers, balances, net worth, stats, CSV export
  categories.py  default categories and keywords
  importers.py   bank alert saving with de-duplication, CSV/PDF statement import
  ai.py          Claude API: receipts, alerts, statements, categories, insights, questions
  reports.py     report text + AI advice;  charts.py  report chart image
  scheduler.py   per-user report times in their timezone
  webapp.py      Mini App API (Telegram initData verified) + /ingest webhook
  fx.py          daily exchange rates (open.er-api.com), cached in the DB
  models.py      database schema
tests/           parser, money logic, imports, reports, full bot conversation (pytest)
```

Run the tests with `pip install -r requirements-dev.txt && pytest`.

## Next steps (v2 ideas)

Budgets with alerts, recurring payments (rent, subscriptions), debts between people, savings goals, monthly net-worth history snapshots, open banking where your country allows it, and replies in more languages.
