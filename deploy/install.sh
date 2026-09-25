#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Newsroom installer for an AWS Lightsail server (Ubuntu 22.04 or 24.04).
#
#  Usual way (see INSTALL-AWS.md): in the server's browser terminal run
#    curl -fsSL https://raw.githubusercontent.com/YOU/REPO/main/deploy/install.sh | \
#      sudo DOMAIN=yourdomain.com SETUP_CODE=your-phrase REPO_URL=https://github.com/YOU/REPO.git bash
#
#  Or paste this file into Lightsail's "launch script" box after editing the lines below,
#  or copy a zip to the server yourself and run:  sudo bash install.sh newsroom.zip
#
#  Progress is written to /var/log/newsroom-install.log
# ─────────────────────────────────────────────────────────────────────────────

# ====== EDIT THESE ======
DOMAIN="${DOMAIN:-news.example.com}"            # your domain, without https://
SETUP_CODE="${SETUP_CODE:-pick-a-secret-phrase}" # typed once in the setup wizard; stops anyone else claiming your site
REPO_URL="${REPO_URL:-}"                         # your GitHub repository address
# ========================

set -euo pipefail
ZIP="${1:-}"
exec > >(tee -a /var/log/newsroom-install.log) 2>&1
echo "== Newsroom install started $(date)"

if [ "$DOMAIN" = "news.example.com" ] || [ "$SETUP_CODE" = "pick-a-secret-phrase" ]; then
  echo "!! Set DOMAIN and SETUP_CODE (see INSTALL-AWS.md)."; exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl git unzip

# Docker
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

# A little swap so building never runs out of memory on small servers
if ! swapon --show | grep -q swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

# Get the code
mkdir -p /opt
if [ -n "$ZIP" ]; then
  rm -rf /tmp/newsroom-unzip && mkdir /tmp/newsroom-unzip
  unzip -q "$ZIP" -d /tmp/newsroom-unzip
  SRC=$(dirname "$(find /tmp/newsroom-unzip -name docker-compose.yml | head -1)")
  mkdir -p /opt/newsroom && cp -a "$SRC"/. /opt/newsroom/
elif [ -n "$REPO_URL" ]; then
  [ -d /opt/newsroom/.git ] || git clone "$REPO_URL" /opt/newsroom
else
  echo "!! Set REPO_URL, or give a zip file:  sudo bash install.sh newsroom.zip"; exit 1
fi
cd /opt/newsroom

# Server settings
cat > .env <<EOF
DOMAIN=$DOMAIN
SETUP_CODE=$SETUP_CODE
EOF
chmod 600 .env
mkdir -p data && chown 1000:1000 data

docker compose up -d --build

# Daily snapshot of the data folder is handled by the app (3 a.m.); nothing else to schedule.
echo "== Done $(date). Point $DOMAIN at this server's static IP, then open https://$DOMAIN"
