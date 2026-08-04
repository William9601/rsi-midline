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

3. **Copy the repo and provision — always with `push.sh`:**

   ```bash
   bash deploy/push.sh root@SERVER
   ```

   This rsyncs the repo (excluding `.venv`, `.env`, `trades*.db`) and then
   runs `setup.sh` on the server, as one atomic step. `setup.sh` installs
   Python deps, creates a no-login `rsibot` user, locks env-file permissions,
   fixes ownership, verifies `rsibot` can write the dir, and enables
   `rsi-bot@<name>` for every `deploy/env/<name>.env` — enabled services start
   on boot, `Restart=always` revives them after crashes.

   > ⚠️ **Never run a bare `rsync` to the server.** rsync copies your Mac's
   > file ownership onto `/opt/rsi-midline-bot`, leaving it unwritable by
   > `rsibot`. SQLite journaling then fails on *every* write while the bots
   > keep trading — so trades execute **unrecorded** and silently, and the
   > journals drift out of sync with the live accounts (this happened
   > 2026-07-13 and 2026-07-30). `push.sh` always re-runs `setup.sh`, which
   > re-chowns and self-checks writability, so the safe path is the only path.

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

To ship code/profile changes: re-run `bash deploy/push.sh root@SERVER`
(idempotent; re-chowns, self-checks writability, restarts the bots).
Restarts are safe mid-day — signals are edge-triggered and entry/exit skip
when the position already matches.

## Weekly replay check (live vs backtest)

`setup.sh` also installs `rsi-replay.timer`: every Saturday 12:00 UTC (the
market week is closed, so every bar the bots could act on is final),
`deploy/replay-check.sh` re-simulates each instance's live period with its
exact env config and diffs the simulated trade list against its journal —
the production version of the repo's "identical rows" verification habit.
Any `SIM-ONLY`/`LIVE-ONLY` row is a live-vs-backtest divergence caught
within days; the service exits non-zero so it shows up in
`systemctl --failed`.

```bash
ssh root@SERVER systemctl status rsi-replay.timer        # next scheduled run
ssh root@SERVER systemctl start rsi-replay               # run it right now
ssh root@SERVER journalctl -u rsi-replay                 # full reports
ssh root@SERVER cat /opt/rsi-midline-bot/replay-report.txt
```

Each env file's `REPLAY_SINCE` is the moment that instance went live with
its current config — **update it whenever you swap a bot's strategy
settings**, or the replay will simulate the new config over trades the old
one took and report drift.

## Safety

- Every committed env example sets `ALPACA_PAPER=true`. Going live means
  editing a server env file by hand — never commit a live key.
- `run`/`loop` place orders; everything else is read-only. The bot services
  only run `loop`; the replay timer only runs `replay` (never trades).
