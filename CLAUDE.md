# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A long-only RSI-midline trading bot for Alpaca: buy when RSI crosses above a
level, sell when it crosses below (with optional band, trend-MA, volume, and
trailing-stop filters). Single module: `rsi_midline_bot.py`. Everything else
is config (`profiles.json`, `.env`) or generated data (`trades.db`).

## Commands

```bash
.venv/bin/python rsi_midline_bot.py run        # one signal pass (places paper orders!)
.venv/bin/python rsi_midline_bot.py loop       # continuous; daily tf sleeps until 10 min before close
.venv/bin/python rsi_midline_bot.py backtest   # compare strategy variants, read-only
.venv/bin/python rsi_midline_bot.py tune       # grid search + walk-forward; may write profiles.json
.venv/bin/python rsi_midline_bot.py tune --dry-run
.venv/bin/python rsi_midline_bot.py trades 50  # show trade journal
```

- The venv is Python **3.9** — keep `from __future__ import annotations`; no 3.10+ syntax at runtime.
- There is no test suite. Verification workflow: after touching `simulate()`,
  re-run `backtest` with unchanged settings and confirm the trail-off variant
  rows are **numerically identical** to before the change (this caught/validated
  past refactors). Strategy-logic pieces (`rsi`, `crossover_signal`) can be
  tested standalone with synthetic pandas Series — they don't need API keys.
- Override any setting per-invocation via env: `TIMEFRAME=15Min BACKTEST_DAYS=1095 .venv/bin/python rsi_midline_bot.py backtest`.
- Fetching a year of 15Min bars takes ~1 min of silence; daily is seconds. Use generous timeouts.
- Unattended cloud deployment (systemd, one service per bot instance): see
  `deploy/README.md`. Per-instance settings live in `deploy/env/*.env`
  (gitignored; `.example` files are committed).

## Critical safety notes

- `run` and `loop` place real orders (paper account by default; live only if
  `ALPACA_PAPER=false`). `backtest`, `tune`, and `trades` never trade.
- `.env` contains real Alpaca API keys. **Do not read it into context** — edit
  it with `sed` and inspect it with `grep -vE 'KEY'`.
- `.env`, `.venv/`, and `trades.db` are gitignored on purpose. Never commit them.

## Configuration precedence (the core architecture)

Settings resolve in this order, implemented across `load_dotenv()`,
`apply_profile()`, and `Config` defaults in `main()`:

1. Shell environment variables (highest)
2. `.env` (auto-loaded; only fills unset vars)
3. `profiles.json` entry for the active `TIMEFRAME` (only fills still-unset vars)
4. `Config` dataclass defaults

So the same code trades a plain RSI-50 cross on `1Day` and a band+filters
setup on `15Min` purely by profile. When adding a new setting, wire all four
layers: env var name in `Config`, `.env.example`, the profile `settings`
dicts, and (if tunable) the tune grid + `_write_profile` + `_current_profile_combo`.

`profiles.json` is settings **plus evidence**: each entry's `notes`/`tuned`/`status`
records the backtest that justified it. The only sanctioned automated writer is
`tune`, which is gated: the grid winner is picked on the train split only and
written **only if it beats the current profile on the held-out test window**.
Don't bypass that gate; when editing profiles by hand, update the notes.

## Simulation semantics (`simulate()`)

- Entry/exit at the **signal bar's close** (the `in_market` array marks the
  bar *after* a cross; bar-return at t is close[t]/close[t-1]). This matches
  live behavior, which evaluates completed bars (intraday drops the forming
  bar; daily trades ~10 min before close). Preserve this parity when changing
  either side — no look-ahead.
- The position state machine is a Python loop (not vectorized) because the
  trailing stop is path-dependent on the trade's high-water mark.
- `eval_from`/`eval_to` restrict *scoring* to a bar range while indicators
  warm up on full history — this is how walk-forward splits avoid losing the
  MA200 warmup. Backtest/tune returns ignore slippage, commissions, dividends.

## Live-trading invariants

- Signals are edge-triggered (cross, not level) and `enter`/`exit` skip when
  the position state already matches, so restarts and repeated passes are safe.
- Trailing stops are **server-side GTC orders on Alpaca** — required because
  the daily loop sleeps ~23h between passes. Consequences baked into the code:
  whole-share entry sizing when `TRAIL_PERCENT` is set (stops can't hold
  fractional shares), `exit()` cancels open orders before `close_position`,
  and `reconcile_stop_fills()` journals stop fills that happened while the
  bot was asleep (deduped by `order_id`).
- Every order is journaled to SQLite (`trades.db`) with its signal context
  (RSI, relative volume, active settings). rvol is recorded even when the
  volume filter is off — intentional, for later analysis.

## Data caveats (free Alpaca tier)

- Historical bars come from the IEX feed: prices are near-consolidated but
  **volumes are a small unreliable slice** — treat volume-filter backtests
  with suspicion.
- Bars are raw/unadjusted (no `adjustment` param passed). Fine for the current
  ETF basket; a latent landmine if individual stocks that split are added.

## Established strategy findings (don't re-derive)

From backtests on QQQ/GLD/IWM/SPY (and earlier SPY/AAPL/MSFT), documented in
`profiles.json` notes:

- Daily bars vastly outperform intraday for this strategy; 15Min raw churns
  ~900 trades/yr/symbol and loses.
- The volume filter helps intraday but is far too strict on daily bars.
- Trailing stops don't improve *returns* out-of-sample on daily; their role is
  drawdown insurance. The tuner optimizes return only — say so when relevant.
- The strategy typically captures ~60-70% of a bull market and earns its keep
  by exiting downtrends (works on trending assets; whipsaws badly on choppy
  ones like XLE). Expect it to trail buy & hold in bull years.
