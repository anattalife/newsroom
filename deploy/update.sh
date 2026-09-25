#!/bin/bash
# Update the newsroom to a newer version. Takes a backup first.
#   From GitHub:      sudo bash /opt/newsroom/deploy/update.sh
#   From a new zip:   sudo bash /opt/newsroom/deploy/update.sh newsroom.zip
set -euo pipefail
cd /opt/newsroom
echo "== Backing up"
docker compose exec -T web python -m app backup
if [ -n "${1:-}" ]; then
  rm -rf /tmp/newsroom-unzip && mkdir /tmp/newsroom-unzip && unzip -q "$1" -d /tmp/newsroom-unzip
  SRC=$(dirname "$(find /tmp/newsroom-unzip -name docker-compose.yml | head -1)")
  # replace the code, never the data folder or .env
  rsync -a --delete --exclude data --exclude .env --exclude .git "$SRC"/ /opt/newsroom/ 2>/dev/null \
    || (cd "$SRC" && tar cf - --exclude=./data --exclude=./.env . | (cd /opt/newsroom && tar xf -))
elif [ -d .git ]; then
  git pull --ff-only
else
  echo "!! This install wasn't made from GitHub. Give the new zip:  sudo bash deploy/update.sh newsroom.zip"; exit 1
fi
docker compose up -d --build
echo "== Updated. If something looks wrong, restore last night's Lightsail snapshot or the backup in data/backups."
