# Move the bot to Oracle Cloud (free, 24/7), all from your phone

Render's free plan gives 750 hours a month and puts the bot to sleep. An **Oracle Cloud Always Free** server is a small computer in the cloud that is yours for free, never sleeps and has no hour limit.

**Your records don't move.** They live in your Neon database, and the new server uses the same Neon connection string. Only the program moves.

- Time: about 45 minutes, once.
- Everything below works in your **phone's browser** (Chrome or Safari). No computer and no app needed.
- Tip: Oracle's website is easier on a phone in **desktop mode**: in Chrome tap **⋮ → Desktop site**, in Safari tap **aA → Request Desktop Website**. Turn the phone sideways for the terminal.

Steps:

1. [Copy two things from Render](#1-copy-two-things-from-render)
2. [Create the Oracle account](#2-create-the-oracle-account)
3. [Create the free server](#3-create-the-free-server)
4. [Open the web ports](#4-open-the-web-ports-80-and-443)
5. [Get a free web address (DuckDNS)](#5-get-a-free-web-address-duckdns)
6. [Connect to the server from Cloud Shell](#6-connect-to-the-server-from-cloud-shell)
7. [Pause the Render copy](#7-pause-the-render-copy)
8. [Install the bot (one command)](#8-install-the-bot-one-command)
9. [Check it works](#9-check-that-it-works)
10. [Clean up Render](#10-clean-up)

---

## 1. Copy two things from Render

Open **dashboard.render.com** → **money-tracker-bot** → **Environment**. Tap the eye icon and copy these into a note on your phone (keep the note private):

- `BOT_TOKEN`
- `DATABASE_URL` (your Neon string, starts with `postgresql://`)
- `OCR_SPACE_API_KEY`, if you set one

## 2. Create the Oracle account

1. Go to **signup.cloud.oracle.com**.
2. Fill in your email, country and name. Verify the email.
3. **Home region:** pick one close to you and remember it. It **can't be changed later**, and free servers are only made there. Good choices from Uzbekistan: *Germany Central (Frankfurt)*, *UK South (London)*, *India West (Mumbai)* or *UAE East (Dubai)*.
4. Add a card. Oracle makes a small check (refunded) to prove you're a real person. **You are not charged** as long as you only create things marked **Always Free**, which is all this guide uses.
5. Wait for the "Your account is ready" email (usually minutes, sometimes a few hours), then sign in at **cloud.oracle.com**.

> If the card is refused, try another card (Visa/Mastercard that works online). Prepaid and virtual cards are often refused.

## 3. Create the free server

1. Tap **☰ (menu) → Compute → Instances → Create instance**.
2. **Name:** `money-bot`.
3. **Image and shape → Edit:**
   - **Change image** → **Ubuntu** → **Canonical Ubuntu 24.04** (not "Minimal") → **Select image**.
   - **Change shape** → **Ampere** → tick **VM.Standard.A1.Flex**, set **OCPUs = 2** and **Memory = 12 GB** → **Select shape**. It shows the *Always Free-eligible* label. (Up to 4 OCPUs and 24 GB are free in total.)
   - If you later get **"Out of capacity"**, either change **Placement** to another availability domain (AD-2, AD-3) and try again, or pick **Specialty and previous generation → VM.Standard.E2.1.Micro** (also free; smaller and slower, but the setup script adds swap so the bot fits).
4. **Networking:** leave everything as it is. Make sure **Automatically assign public IPv4 address** is on.
5. **Add SSH keys:** choose **Generate a key pair for me** and tap **Download private key**. Your phone saves a file like `ssh-key-2026-11-18.key` in Downloads. You'll need it in step 6.
6. **Boot volume:** leave the default.
7. Tap **Create**. After a minute or two the status turns green: **Running**.
8. On the instance page, copy the **Public IP address** (like `141.147.12.34`) into your note.

## 4. Open the web ports (80 and 443)

The Mini App needs HTTPS, which uses ports 80 and 443. Oracle blocks them until you allow them.

1. On the instance page, under **Primary VNIC**, tap the **Subnet** link (`subnet-2026...`).
2. Open **Security** (or scroll to **Security Lists**) → tap **Default Security List for vcn-...**.
3. **Security rules** → **Add Ingress Rules** and fill in:
   - Source CIDR: `0.0.0.0/0`
   - IP Protocol: **TCP**
   - Destination Port Range: `80`
4. Tap **+ Another Ingress Rule** and repeat with Destination Port Range `443`.
5. Tap **Add Ingress Rules**.

(The server's own firewall is opened by the setup script in step 8.)

## 5. Get a free web address (DuckDNS)

Telegram only opens the Mini App from an `https://` address with a name, not a bare IP.

1. Go to **duckdns.org** and sign in (Google is easiest).
2. In **sub domain**, type a name, for example `moneybot-akoone`, and tap **add domain**.
3. In the **current ip** box next to it, paste your server's **Public IP** and tap **update ip**.

Your address is now `moneybot-akoone.duckdns.org`. Copy it into your note. The server's IP stays the same as long as you don't delete the server, so you only do this once.

## 6. Connect to the server from Cloud Shell

**Cloud Shell** is a terminal inside Oracle's website, so you don't need any app.

1. Back on **cloud.oracle.com**, tap the **>_** icon at the top right (Developer tools → **Cloud Shell**). A black terminal opens at the bottom after about 30 seconds.
2. In the Cloud Shell window, tap the **gear ⚙ (or ☰) menu → Upload**, pick the `ssh-key-....key` file from your Downloads, and tap **Upload**.
3. Type (or paste) these, one line at a time, replacing `YOUR_IP`. To paste on a phone, long-press in the terminal and choose **Paste**.

```bash
mv ~/ssh-key-*.key ~/oracle.key && chmod 600 ~/oracle.key
```

```bash
ssh -i ~/oracle.key ubuntu@YOUR_IP
```

4. It asks *Are you sure you want to continue connecting?* → type `yes` and press Enter.

The prompt changes to `ubuntu@money-bot:~$`. You are now on your new server. Next time, open Cloud Shell and run only the `ssh` line.

> **Alternatives to Cloud Shell:** the free apps **Termius** (iPhone and Android) or **Termux** (Android). In Termius: *Keychain → Import key* (pick the `.key` file), then *Hosts → New host* with your IP, username `ubuntu` and that key. Everything below is the same.

## 7. Pause the Render copy

Only one copy of the bot can receive messages. In **dashboard.render.com** → **money-tracker-bot**, tap **Suspend** (or *Settings → Suspend Web Service*) and confirm. Don't delete it yet; you can tap **Resume** if anything goes wrong.

## 8. Install the bot (one command)

In the server terminal (`ubuntu@money-bot`), paste:

```bash
git clone https://github.com/akoone/financial-audit.git finance-bot && cd finance-bot && bash deploy/oracle/setup.sh
```

The script asks a few questions. Paste each answer from your note and press Enter:

| Question | Answer |
| --- | --- |
| Bot token | `BOT_TOKEN` from step 1 |
| Neon DATABASE_URL | `DATABASE_URL` from step 1 (this keeps all your records) |
| DuckDNS domain | e.g. `moneybot-akoone.duckdns.org` (without `https://`) |
| OCR.space key | from step 1, or just press Enter |
| Time zone | press Enter for `Asia/Tashkent` |
| Currency | press Enter for `UZS` |

Then it installs Docker, opens the server firewall, builds the bot and starts it. The first run takes **5 to 10 minutes** (longer on E2.1.Micro). It ends with **Done** and a list where `bot` and `caddy` show **Up** (and `db`, an empty spare database that is only used if you leave Neon later).

What the script set up for you:

- The bot, the database and **Caddy** (gets and renews the free HTTPS certificate) run in Docker and **start again by themselves after a reboot or crash**.
- A **backup every night at 03:00** into `~/backups` on the server (14 days kept), on top of the monthly backup zip the bot already sends to your Telegram.
- Your settings are in `~/finance-bot/.env`.

## 9. Check that it works

1. In Telegram, send `/start` to the bot. It answers within a second or two, and your old records are all there (`/balance`).
2. Tap **Open app** next to the message box. The Mini App opens from your DuckDNS address. If the button is missing, close and reopen the chat.
3. To watch the bot's log on the server: `sudo docker compose logs -f bot` (Ctrl+C to stop; on a phone keyboard Ctrl is usually in the extra-keys row). You should see `Database: PostgreSQL (Neon)` and `Polling mode`.

If something is wrong, see [Troubleshooting](#troubleshooting).

## 10. Clean up

Once the bot has worked for a day or two:

1. **The GitHub keep-awake ping** is already removed from the repository (it was only needed for Render, and fails with *503* once Render is suspended).
2. If you made a job on **cron-job.org** for the Render address, delete it.
3. If you set the Mini App in **@BotFather** (*Bot Settings → Configure Mini App*), change its URL to `https://YOUR-DOMAIN/app`.
4. Delete the Render service: **money-tracker-bot → Settings → Delete Web Service**.

---

## Everyday commands

Open Cloud Shell, run `ssh -i ~/oracle.key ubuntu@YOUR_IP`, then `cd finance-bot`.

| What | Command |
| --- | --- |
| Is it running? | `docker compose ps` |
| See the log | `docker compose logs -f --tail 50 bot` |
| Restart the bot | `docker compose restart bot` |
| Update to the newest version from GitHub | `git pull && docker compose up -d --build` |
| Change a setting | `nano .env`, then `docker compose up -d` |
| Back up now | `bash deploy/oracle/backup.sh` |
| List backups | `ls -lh ~/backups` |
| Stop everything | `docker compose down` (data stays) |

(If a `docker` command says *permission denied*, put `sudo` in front, or disconnect with `exit` and connect again once.)

In `nano`: move with the arrow keys, save with **Ctrl+O** then Enter, exit with **Ctrl+X**.

## Backups

You have three independent copies:

1. **Neon** itself (keeps 6 hours of restore history on the free plan).
2. **Nightly SQL backups** on the server in `~/backups/finance_YYYY-MM-DD.sql.gz`.
3. **Monthly backup zip** the bot sends to your Telegram chat (and `/backup` any time).

To download a server backup to your phone: in Cloud Shell (not on the server) run
`scp -i ~/oracle.key "ubuntu@YOUR_IP:backups/*.gz" ~/`, then **⚙ → Download** and type the file name.

## Keeping it free

- Only create things labelled **Always Free**. Oracle's free limits are 4 Ampere OCPUs, 24 GB memory, 200 GB of disk and 10 TB of traffic a month in total; this bot uses a small part.
- Oracle may reclaim Always Free servers that sit almost completely idle for a week. A bot that is always polling Telegram is rarely affected, but if you want to be sure, upgrade the account to **Pay As You Go** (*Billing → Upgrade*). You still pay nothing while you stay within the Always Free limits; setting a budget alert at $1 (*Billing → Budgets*) warns you if anything ever costs money.
- Ubuntu installs security updates by itself. Once a month, run `sudo apt update && sudo apt upgrade -y && sudo reboot`; the bot comes back on its own after the reboot.

## Optional: move your records from Neon onto the server

Neon is fine to keep. If you'd rather have everything on your own server (no Neon limits, faster replies), on the server inside `finance-bot`:

```bash
bash deploy/oracle/backup.sh
docker compose stop bot
gunzip -c ~/backups/finance_$(date +%F).sql.gz | docker compose exec -T db psql -q -U finance finance
nano .env
```

In `nano`, empty the line so it reads `DATABASE_URL=`, save and exit, then:

```bash
docker compose up -d
docker compose logs --tail 20 bot
```

The log should say `Database: PostgreSQL` (without "Neon"). A message like `unrecognized configuration parameter "transaction_timeout"` during the restore is harmless. Keep the Neon project for a few weeks as an extra copy before deleting it.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| **Out of capacity** when creating the server | Free Ampere servers are popular. Try another availability domain, try again later (early morning often works), or use **VM.Standard.E2.1.Micro**. |
| The bot doesn't answer, log says *Another copy of this bot receives messages at ...onrender.com* | The Render copy is still running. Suspend it (step 7). The new copy takes over within a minute. |
| Log says *Conflict: terminated by other getUpdates request* | Another copy runs in polling mode somewhere (an old test). Stop it. |
| **Open app** missing or the Mini App doesn't load | Check `docker compose logs caddy`. Usually the DuckDNS IP is wrong (step 5) or the ports aren't open (step 4). Fix it, then `docker compose restart caddy`. |
| The build stops with *Killed* | Not enough memory (E2.1.Micro). Run `bash deploy/oracle/setup.sh` again; it adds swap first and continues. |
| `ssh: Connection timed out` | Wrong IP, or the server is stopped. On the instance page check it is **Running**. |
| `Permission denied (publickey)` | Use the key you downloaded for *this* server and username `ubuntu`: `ssh -i ~/oracle.key ubuntu@YOUR_IP`. |
| I want to go back to Render | On the server: `cd finance-bot && docker compose down`. On Render: **Resume**. Your records are in Neon, so nothing is lost either way. |
