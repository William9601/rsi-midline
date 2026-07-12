# Running the bots unattended on a cloud server

One systemd service per bot instance; each instance gets its settings from
its own env file in `deploy/env/`. Because the bot's precedence is
shell env > `.env` > `profiles.json`, the env files fully define each bot —
no code or config changes on the server.

## One-time setup

1. **Get a server.** Any Ubuntu 22.04+ VPS with 1 GB RAM is plenty
   (DigitalOcean, Hetzner, AWS Lightsail — roughly $4–6/month). The server's
   timezone doesn't matter: the bot asks Alpaca's clock API for market hours.

2. **Create the env files** locally from the examples:

   ```bash
   cp deploy/env/1day.env.example deploy/env/1day.env   # etc.
   ```

   Fill in Alpaca **paper** API keys. Important: bots sharing one account and
   symbol basket fight over positions (one bot's sell closes another bot's
   trade). Give each bot its own paper account, or non-overlapping `SYMBOLS`.
   Real env files are gitignored; only `.example` files are committed.

3. **Copy the repo and provision:**

   ```bash
   rsync -a --exclude .venv --exclude .env --exclude 'trades*.db' \
       ./ root@SERVER:/opt/rsi-midline-bot/
   ssh root@SERVER 'bash /opt/rsi-midline-bot/deploy/setup.sh'
   ```

   The script installs Python deps, creates a no-login `rsibot` user, locks
   env-file permissions, and enables `rsi-bot@<name>` for every
   `deploy/env/<name>.env` — enabled services start on boot, `Restart=always`
   revives them after crashes.

## Day-to-day

```bash
ssh root@SERVER systemctl status 'rsi-bot@*'            # are they alive?
ssh root@SERVER journalctl -u rsi-bot@15min --since today
ssh root@SERVER -t 'cd /opt/rsi-midline-bot && \
    TRADES_DB=trades-15min.db .venv/bin/python rsi_midline_bot.py trades 30'
ssh root@SERVER -t 'cd /opt/rsi-midline-bot && \
    .venv/bin/python rsi_midline_bot.py pnl trades-*.db'   # win/loss stats per bot
ssh root@SERVER systemctl stop rsi-bot@1hour            # pause one bot
```

To ship code/profile changes: re-run the same `rsync` + `setup.sh` pair
(idempotent; restarts the bots). Restarts are safe mid-day — signals are
edge-triggered and entry/exit skip when the position already matches.

## Safety

- Every committed env example sets `ALPACA_PAPER=true`. Going live means
  editing a server env file by hand — never commit a live key.
- `run`/`loop` place orders; everything else is read-only. The services only
  run `loop`.
