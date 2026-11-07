# Put the bot online for free

Two free ways. **A (Render + Neon)** is the easiest: no server to manage, HTTPS is automatic, so the Mini App works straight away. **B (Oracle Cloud)** is a full server that never sleeps, but takes longer to set up.

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

A free Render service sleeps after 15 minutes without visitors. Messages still wake it (the first reply then takes up to a minute), but the evening reports and the nightly balance record only run while it's awake. A free pinger fixes that:

1. Sign up at **cron-job.org** (free).
2. Create a cron job: URL `https://YOUR-SERVICE.onrender.com/health`, every **10 minutes**.

Render gives 750 free hours a month, enough for one service running all month.

### A4. Optional: show the app on the bot's profile

In @BotFather: **/mybots → your bot → Bot Settings → Configure Mini App → Enable Mini App**, and enter `https://YOUR-SERVICE.onrender.com/app`. The bot's profile then shows an **Open App** button too.

### A5. Stop any other copy

Only one copy of the bot should run per token. When the Render copy starts it takes over messages automatically; a copy started later on another machine waits instead of stealing them (it logs *Another copy of this bot receives messages at ...*).

### Backups with this setup

- Neon keeps its own restore history (6 hours on the free plan).
- The bot sends a full backup zip to your Telegram chat on the 1st of every month, and on demand from **Statement → Send a full backup** in the app or `/backup`.

## B. Oracle Cloud Always Free (Docker + PostgreSQL)

This takes about 30 minutes, once. Use it if you want a server that never sleeps. The bot then runs 24/7, restarts by itself after a reboot, and keeps all its data in PostgreSQL on your server.

### B1. Create the free server

1. Sign up at **cloud.oracle.com** and choose the **Always Free** account. Oracle asks for a card to verify you, but you aren't charged as long as you only use resources marked "Always Free".
   - Choose a **home region** close to you, such as Frankfurt. You can't change it later.
2. Go to **Menu → Compute → Instances → Create instance**.
   - **Image:** Canonical **Ubuntu 24.04**.
   - **Shape:** click *Change shape* → **Ampere → VM.Standard.A1.Flex**, with 1 OCPU and 6 GB memory (Always Free). If it says "out of capacity", pick **VM.Standard.E2.1.Micro** instead. That also works with the bot's database, but it's slower.
   - **Networking:** leave the defaults and keep **Assign a public IPv4 address** on.
   - **SSH keys:** click **Save private key** and keep the file (for example `oracle.key`).
3. Click **Create**. When the instance is *Running*, copy its **Public IP address**.

### B2. Connect to the server

On your computer, open a terminal. On Windows, use PowerShell.

```bash
chmod 600 oracle.key            # macOS/Linux only
ssh -i oracle.key ubuntu@YOUR_IP
```

### B3. Install Docker (on the server)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
exit
```

Connect again with the same `ssh` command so the permission takes effect.

### B4. Get the bot code (on the server)

```bash
git clone https://github.com/akoone/Financial-Audit.git finance-bot
cd finance-bot
cp .env.example .env
nano .env
```

In `.env`, set:

```
BOT_TOKEN=your token from @BotFather
ALLOWED_USER_IDS=                 # empty = open to everyone
ANTHROPIC_API_KEY=                # optional; empty = free OCR for receipts, no AI advice
OCR_SPACE_API_KEY=                # optional, free key from ocr.space/ocrapi; better receipt reading
AI_MODEL=claude-haiku-5-5         # cheapest model, if you add a key
DEFAULT_TIMEZONE=Asia/Tashkent
DEFAULT_CURRENCY=UZS
DATABASE_URL=                     # empty = bundled PostgreSQL; or paste your Neon connection string
```

Save with `Ctrl+O`, then `Enter`, then `Ctrl+X`. If `DATABASE_URL` is empty, Docker Compose points the bot at its own PostgreSQL automatically. For Neon, see "Using Neon" below.

### B5. Start it

```bash
docker compose up -d --build
docker compose logs -f bot        # you should see "Run polling for bot @..."  (Ctrl+C to exit logs)
```

Open the bot in Telegram and send `/start`.

> Only one copy of the bot can run per token. If a test copy is still running somewhere else, stop it, or Telegram will show a "conflict" error in the logs.

### Using Neon instead of the bundled database (optional)

Neon (neon.tech) gives you a free hosted PostgreSQL. It's useful on hosts that wipe files, like Render, or if you want the data off your server.

1. Create a project at **neon.tech**, then click **Connect** and copy the connection string. It starts with `postgresql://...neon.tech/neondb?sslmode=require`.
2. Paste it into `.env` as is: `DATABASE_URL=postgresql://...` (the bot converts the format itself).
3. Run `docker compose up -d --build bot`. The bundled `db` container then just sits unused. The tables are created automatically on first start.

Neon's free plan pauses the database after a few minutes without use. The bot's first reply after a quiet period can take a second or two longer, but nothing else changes. Back up with Neon's dashboard (branches / restore) instead of `pg_dump` from the `db` container.

### B6. Everyday commands (on the server, inside `finance-bot`)

| What | Command |
| --- | --- |
| See logs | `docker compose logs -f bot` |
| Restart | `docker compose restart bot` |
| Update to the latest version | `git pull && docker compose up -d --build` |
| Back up the database | `docker compose exec -T db pg_dump -U finance finance > backup_$(date +%F).sql` |
| Restore a backup | `docker compose exec -T db psql -U finance finance < backup_2026-11-07.sql` |
| Stop | `docker compose down` (your data stays in the `pgdata` volume) |

To make a daily automatic backup at 03:00, run `crontab -e` and add this line:

```
0 3 * * * cd /home/ubuntu/finance-bot && docker compose exec -T db pg_dump -U finance finance > /home/ubuntu/backup_$(date +\%u).sql
```

This keeps the last 7 days.

### B7. The Mini App and automatic bank alerts (needs HTTPS)

1. Get a free domain name pointing to your server's IP. For example, create `mybot.duckdns.org` at **duckdns.org**.
2. Open ports 80 and 443:
   - In Oracle: **Instance → Subnet → Default Security List → Add Ingress Rules**. Add source `0.0.0.0/0`, TCP, port `80`, and another rule for `443`.
   - On the server, run:
     ```bash
     sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
     sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
     sudo netfilter-persistent save
     ```
3. Install Caddy, which adds HTTPS automatically:
   ```bash
   sudo apt-get install -y caddy
   echo "mybot.duckdns.org {
     reverse_proxy localhost:8080
   }" | sudo tee /etc/caddy/Caddyfile
   sudo systemctl restart caddy
   ```
4. In `.env`, set `WEBAPP_URL=https://mybot.duckdns.org`, then run `docker compose up -d`.

After that, the **Open app** button appears next to the message box, and `/app` and the button under each report open the Mini App. `/connect` gives each user their own link for sending bank alerts automatically.
