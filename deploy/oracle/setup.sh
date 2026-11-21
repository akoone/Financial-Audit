#!/usr/bin/env bash
# One-time setup of the money tracker bot on a fresh Ubuntu server (Oracle Cloud Always Free).
# Run from the repository folder:   bash deploy/oracle/setup.sh
# Safe to run again: steps that are already done are skipped. See DEPLOY-ORACLE.md.
set -euo pipefail
cd "$(dirname "$0")/../.."
say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

# 1. Swap: the small E2.1.Micro server has only 1 GB of memory, too little to build the image.
if [ "$(swapon --show | wc -l)" -eq 0 ] && [ "$(free -m | awk '/Mem:/{print $2}')" -lt 4000 ]; then
  say "Adding 2 GB of swap (extra memory on disk)"
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# 2. Docker (starts automatically after every reboot)
if ! command -v docker >/dev/null; then
  say "Installing Docker (2-3 minutes)"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi
sudo systemctl enable --now docker >/dev/null 2>&1 || true

# 3. Server firewall: Oracle's Ubuntu image blocks everything except SSH. Open 80 and 443 for HTTPS.
say "Opening ports 80 and 443 in the server firewall"
for p in 80 443; do
  sudo iptables -C INPUT -p tcp --dport $p -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 1 -p tcp --dport $p -j ACCEPT
done
sudo iptables -C INPUT -p udp --dport 443 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 1 -p udp --dport 443 -j ACCEPT
if command -v netfilter-persistent >/dev/null; then sudo netfilter-persistent save >/dev/null 2>&1 || true; fi

# 4. Settings (.env). Asks only for what's missing.
if [ ! -f .env ]; then
  say "A few questions (long-press to paste on a phone; press Enter to skip optional ones)"
  read -rp "Bot token from @BotFather: " TOKEN
  read -rp "Neon DATABASE_URL to keep your current records (Enter = new empty database on this server): " DBURL
  read -rp "Your DuckDNS domain, e.g. mybot.duckdns.org (Enter = no Mini App for now): " DOMAIN
  read -rp "OCR.space key (optional): " OCRKEY
  read -rp "Time zone [Asia/Tashkent]: " TZ_; TZ_=${TZ_:-Asia/Tashkent}
  read -rp "Currency [UZS]: " CUR; CUR=${CUR:-UZS}
  cat > .env <<ENV
BOT_TOKEN=$TOKEN
ALLOWED_USER_IDS=
AI_DAILY_LIMIT=40
DATABASE_URL=$DBURL
ANTHROPIC_API_KEY=
AI_MODEL=claude-haiku-5-5
OCR_SPACE_API_KEY=$OCRKEY
DEFAULT_TIMEZONE=$TZ_
DEFAULT_CURRENCY=$CUR
# Polling: this server is always on. It also takes messages over from the old Render copy.
BOT_MODE=polling
DOMAIN=$DOMAIN
WEBAPP_URL=${DOMAIN:+https://$DOMAIN}
COMPOSE_FILE=docker-compose.yml:docker-compose.oracle.yml
COMPOSE_PROFILES=${DOMAIN:+https}
ENV
  chmod 600 .env
  echo "Saved settings to .env (change later with: nano .env)"
fi
grep -q '^COMPOSE_FILE=' .env || echo 'COMPOSE_FILE=docker-compose.yml:docker-compose.oracle.yml' >> .env

# 5. Build and start
say "Building and starting the bot (first time: 5-10 minutes)"
sudo docker compose up -d --build

# 6. Nightly backup at 03:00
chmod +x deploy/oracle/backup.sh
JOB="0 3 * * * $(pwd)/deploy/oracle/backup.sh >> $HOME/backups.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'deploy/oracle/backup.sh' ; echo "$JOB" ) | crontab -
sudo usermod -aG docker "$USER"   # so cron (and you, after reconnecting) can run docker without sudo

say "Done. Bot status:"
sudo docker compose ps
echo
echo "Logs:  sudo docker compose logs -f bot   (Ctrl+C to stop watching)"
D=$(grep '^DOMAIN=' .env | cut -d= -f2 || true)
if [ -n "$D" ]; then echo "Mini App: https://$D/app  (the certificate can take a minute)"; fi
