#!/usr/bin/env bash
# Deploy code/config to the droplet in ONE step: rsync + provision.
#
#   bash deploy/push.sh root@SERVER
#
# ALWAYS use this instead of a bare `rsync`. A raw rsync copies the client's
# file ownership onto the server, leaving /opt/rsi-midline-bot unwritable by
# the `rsibot` service user. SQLite then fails EVERY journal write with
# "attempt to write a readonly database" — but the bots keep placing orders,
# so the failure is SILENT: trades execute unrecorded and the journals drift
# out of sync with the live accounts (this bit us 2026-07-13 and again
# 2026-07-30, ~5 days of unjournaled trades each time).
#
# setup.sh fixes ownership (chown -R rsibot:rsibot) and now self-checks that
# rsibot can actually write the dir, failing loudly if not. Pairing rsync +
# setup.sh here makes the safe path the only path — you cannot rsync without
# then re-provisioning.
set -euo pipefail

target="${1:?usage: bash deploy/push.sh user@server}"
dest="/opt/rsi-midline-bot"

# Run from the repo root regardless of where this is invoked.
cd "$(dirname "$0")/.."

echo ">>> rsync -> $target:$dest"
rsync -a --exclude .venv --exclude .env --exclude 'trades*.db' \
    ./ "$target:$dest/"

echo ">>> provisioning (setup.sh: deps, ownership, writability self-check, restart)"
ssh "$target" "bash $dest/deploy/setup.sh"

echo ">>> deploy OK"
