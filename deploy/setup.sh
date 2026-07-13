#!/usr/bin/env bash
# One-shot provisioning for a fresh Ubuntu/Debian VPS. Run as root from the
# repo after copying it to the server:
#
#   rsync -a --exclude .venv --exclude .env --exclude 'trades*.db' \
#       ./ root@SERVER:/opt/rsi-midline-bot/
#   ssh root@SERVER 'bash /opt/rsi-midline-bot/deploy/setup.sh'
#
# Idempotent: re-run after code updates to reinstall deps + restart bots.
set -euo pipefail
cd /opt/rsi-midline-bot

apt-get update -q
apt-get install -yq python3-venv

id -u rsibot >/dev/null 2>&1 || \
    useradd --system --home /opt/rsi-midline-bot --shell /usr/sbin/nologin rsibot

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

# Env files contain API keys: readable by the service user only.
chown -R rsibot:rsibot /opt/rsi-midline-bot
chmod 600 deploy/env/*.env 2>/dev/null || true

cp deploy/rsi-bot@.service /etc/systemd/system/
# Weekly replay check: re-simulate each bot's live period, diff vs journal.
cp deploy/rsi-replay.service deploy/rsi-replay.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now rsi-replay.timer

# Start one instance per real env file (examples don't count).
started=0
for f in deploy/env/*.env; do
    [ -e "$f" ] || continue
    name=$(basename "$f" .env)
    if grep -q 'key_here' "$f"; then
        echo "SKIP $name: $f still has placeholder API keys"
        continue
    fi
    systemctl enable --now "rsi-bot@$name"
    systemctl restart "rsi-bot@$name"
    echo "STARTED rsi-bot@$name (logs: journalctl -u rsi-bot@$name -f)"
    started=$((started + 1))
done
[ "$started" -gt 0 ] || echo "No bots started: create deploy/env/<name>.env from the .example files first."
