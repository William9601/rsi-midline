#!/usr/bin/env bash
# Weekly live-vs-backtest reconciliation: for every deployed instance, replay
# the simulation over its live period with its exact env config and diff the
# simulated trade list against its journal. Any mismatch is a strategy/data
# bug caught within days instead of surfacing in the P&L months later.
#
# Run by rsi-replay.timer (Saturdays, when the week's bars are final); run it
# manually any time — it is read-only:
#
#   systemctl start rsi-replay        # or: bash deploy/replay-check.sh
#   journalctl -u rsi-replay          # full reports
#   cat /opt/rsi-midline-bot/replay-report.txt
set -uo pipefail
cd /opt/rsi-midline-bot

report=replay-report.txt
: > "$report"
fail=0
ran=0
for f in deploy/env/*.env; do
    [ -e "$f" ] || continue
    name=$(basename "$f" .env)
    if grep -q 'key_here' "$f"; then
        continue
    fi
    echo "##### $name ($(date -u +%FT%TZ)) #####" | tee -a "$report"
    # Subshell so one instance's env can't leak into the next.
    if ! (set -a; . "$f"; set +a
          exec .venv/bin/python rsi_midline_bot.py replay) 2>&1 | tee -a "$report"
    then
        fail=1
    fi
    ran=$((ran + 1))
done
if [ "$ran" -eq 0 ]; then
    echo "replay-check: no deployed env files found" | tee -a "$report"
    fail=1
fi
exit $fail
