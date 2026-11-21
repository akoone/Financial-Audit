# Put the bot online for free

Two free ways. **A (Render + Neon)** is the easiest: no server to manage, HTTPS is automatic, so the Mini App works straight away. **B (Oracle Cloud)** is a full server that never sleeps and has no monthly hour limit; it takes longer to set up, once.

## A. Render + Neon (recommended, about 15 minutes)

Your records live in **Neon** (a hosted PostgreSQL database). The bot itself runs on **Render**. Render's free disk is wiped at every restart, which is fine, because nothing is stored there.

### A1. Get the database connection string (Neon)

1. Sign in at **neon.tech** and open your project (or create one; pick the region closest to you).
2. Click **Connect**, keep **Connection pooling** on, and copy the string. It looks like
   `postgresql://neondb_owner:...@ep-...-pooler....neon.tech/neondb?sslmode=require&channel_binding=require`.
   Paste it as is; the bot converts it and uses the right settings for Neon's pooler.

Neon's free plan includes 1 GB of storage per project (years of personal records) and pauses the database after 5 minutes without queries. The bot's first reply after a pause takes a second longer. The bot's health check never touches the database, so pings don't use up Neon's free compute hours.

### A2. Create the bot service (Render)

1. Sign in at **render.com** with your GitHub account.
2. Click **New → Blueprint**, pick the repository **akoone/Financial-Audit**, and click **Apply**. Render reads `render.yaml` from the repo.
3. It asks for the secret values:
   - `BOT_TOKEN`: your token from @BotFather.
   - `DATABASE_URL`: the Neon string from A1.
   - `ANTHROPIC_API_KEY`: leave empty for free mode (receipts are read with free OCR).
   - `OCR_SPACE_API_KEY` (optional, free): reads receipt photos better than the built-in reader. Get a key at https://ocr.space/ocrapi (fill in your email, the key arrives by email; free plan 25,000 images a month). The built-in Tesseract reader stays as the backup.
4. Wait for the first deploy (about 5 minutes). In **Logs** you should see `Database: PostgreSQL (Neon)` and `Webhook mode`. Open the service's `https://....onrender.com` address: it says *Money tracker bot is running*.

That's it. In Telegram, open the bot: there's now an **Open app** button next to the message box. Every push to GitHub redeploys automatically.

> Render is in the Ohio region in `render.yaml`, near Neon's `us-east-2`. If your Neon project is in Europe, change `region` to `frankfurt` before applying.

### A3. Keep it awake (needed for automatic reports)

A free Render service sleeps after 15 minutes without visitors. Messages still wake it (the first reply then takes up to a minute), but the evening reports and the nightly balance record only run while it's awake. The bot pings itself at `/health` every 10 minutes while it runs (turn off with `KEEP_AWAKE=false`), but that can't wake it once Render has put it to sleep. For an outside ping, sign up at **cron-job.org** (free) and create a job for `https://YOUR-SERVICE.onrender.com/health` every **10 minutes**.

Render gives 750 free hours a month, enough for one service running all month.

### A4. Optional: show the app on the bot's profile

In @BotFather: **/mybots → your bot → Bot Settings → Configure Mini App → Enable Mini App**, and enter `https://YOUR-SERVICE.onrender.com/app`. The bot's profile then shows an **Open App** button too.

### A5. Stop any other copy

Only one copy of the bot should run per token. When the Render copy starts it takes over messages automatically; a copy started later on another machine waits instead of stealing them (it logs *Another copy of this bot receives messages at ...*).

### Backups with this setup

- Neon keeps its own restore history (6 hours on the free plan).
- The bot sends a full backup zip to your Telegram chat on the 1st of every month, and on demand from **Statement → Send a full backup** in the app or `/backup`.

## B. Oracle Cloud Always Free (24/7, no hour limit)

A free server that never sleeps, with Docker, automatic HTTPS (Caddy + DuckDNS), restart after reboot and nightly backups. It can all be done from a phone, and one script does the server setup.

Follow **[DEPLOY-ORACLE.md](DEPLOY-ORACLE.md)**. Moving from Render keeps all records, because they stay in Neon.
