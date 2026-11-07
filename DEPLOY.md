# Run the bot free on Oracle Cloud (Docker + PostgreSQL)

This takes about 30 minutes, once. The bot then runs 24/7, restarts by itself after a reboot, and keeps all its data in PostgreSQL on your server.

## 1. Create the free server

1. Sign up at **cloud.oracle.com** and choose the **Always Free** account. Oracle asks for a card to verify you, but you aren't charged as long as you only use resources marked "Always Free".
   - Choose a **home region** close to you, such as Frankfurt. You can't change it later.
2. Go to **Menu → Compute → Instances → Create instance**.
   - **Image:** Canonical **Ubuntu 24.04**.
   - **Shape:** click *Change shape* → **Ampere → VM.Standard.A1.Flex**, with 1 OCPU and 6 GB memory (Always Free). If it says "out of capacity", pick **VM.Standard.E2.1.Micro** instead. That also works with the bot's database, but it's slower.
   - **Networking:** leave the defaults and keep **Assign a public IPv4 address** on.
   - **SSH keys:** click **Save private key** and keep the file (for example `oracle.key`).
3. Click **Create**. When the instance is *Running*, copy its **Public IP address**.

## 2. Connect to the server

On your computer, open a terminal. On Windows, use PowerShell.

```bash
chmod 600 oracle.key            # macOS/Linux only
ssh -i oracle.key ubuntu@YOUR_IP
```

## 3. Install Docker (on the server)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
exit
```

Connect again with the same `ssh` command so the permission takes effect.

## 4. Upload the bot

From **your computer**, in the folder containing `finance-bot.zip`, run:

```bash
scp -i oracle.key finance-bot.zip ubuntu@YOUR_IP:~
```

Then **on the server**:

```bash
sudo apt-get install -y unzip
unzip finance-bot.zip && cd finance-bot
cp .env.example .env
nano .env
```

In `.env`, set:

```
BOT_TOKEN=your token from @BotFather
ALLOWED_USER_IDS=                 # empty = open to everyone
ANTHROPIC_API_KEY=                # optional; empty = free OCR for receipts, no AI advice
AI_MODEL=claude-haiku-5-5         # cheapest model, if you add a key
DEFAULT_TIMEZONE=Asia/Tashkent
DEFAULT_CURRENCY=UZS
```

Save with `Ctrl+O`, then `Enter`, then `Ctrl+X`. You don't need to change `DATABASE_URL`, because Docker Compose points the bot at its own PostgreSQL automatically.

## 5. Start it

```bash
docker compose up -d --build
docker compose logs -f bot        # you should see "Run polling for bot @..."  (Ctrl+C to exit logs)
```

Open the bot in Telegram and send `/start`.

> Only one copy of the bot can run per token. If a test copy is still running somewhere else, stop it, or Telegram will show a "conflict" error in the logs.

## Using Neon instead of the bundled database (optional)

Neon (neon.tech) gives you a free hosted PostgreSQL. It's useful on hosts that wipe files, like Render, or if you want the data off your server.

1. Create a project at **neon.tech**, then click **Connect** and copy the connection string. It starts with `postgresql://...neon.tech/neondb?sslmode=require`.
2. Paste it into `.env` as is: `DATABASE_URL=postgresql://...` (the bot converts the format itself).
3. Run `docker compose up -d --build bot`. The bundled `db` container then just sits unused. The tables are created automatically on first start.

Neon's free plan pauses the database after a few minutes without use. The bot's first reply after a quiet period can take a second or two longer, but nothing else changes. Back up with Neon's dashboard (branches / restore) instead of `pg_dump` from the `db` container.

## 6. Everyday commands (on the server, inside `finance-bot`)

| What | Command |
| --- | --- |
| See logs | `docker compose logs -f bot` |
| Restart | `docker compose restart bot` |
| Update after you get new code | upload the new zip, unzip over the folder, then `docker compose up -d --build` |
| Back up the database | `docker compose exec -T db pg_dump -U finance finance > backup_$(date +%F).sql` |
| Restore a backup | `docker compose exec -T db psql -U finance finance < backup_2026-11-07.sql` |
| Stop | `docker compose down` (your data stays in the `pgdata` volume) |

To make a daily automatic backup at 03:00, run `crontab -e` and add this line:

```
0 3 * * * cd /home/ubuntu/finance-bot && docker compose exec -T db pg_dump -U finance finance > /home/ubuntu/backup_$(date +\%u).sql
```

This keeps the last 7 days.

## 7. Optional: dashboard and automatic bank alerts (needs HTTPS)

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

After that, `/dashboard` and the button under each report open the interactive charts. `/connect` gives each user their own link for sending bank alerts automatically.
