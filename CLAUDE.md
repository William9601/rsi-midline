# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A long-only RSI-midline trading bot for Alpaca: buy when RSI crosses above a
level, sell when it crosses below (with optional band, trend-MA, volume,
higher-timeframe-trend, and trailing-stop filters). Single module:
`rsi_midline_bot.py`. Everything else is config (`profiles.json`, `.env`,
`deploy/env/`) or generated data (`trades.db`, `backtest_results.csv` — the
append-only experiment log every backtest writes to).

## Commands

```bash
.venv/bin/python rsi_midline_bot.py run        # one signal pass (places paper orders!)
.venv/bin/python rsi_midline_bot.py loop       # continuous; daily tf sleeps until 10 min before close
.venv/bin/python rsi_midline_bot.py backtest   # compare variants (never trades); appends to backtest_results.csv
.venv/bin/python rsi_midline_bot.py portfolio  # backtest all symbols on ONE shared account with live sizing
                                               # (equity curve, max DD, exposure); e.g. NOTIONAL_PCT=25 COST_BPS=5
.venv/bin/python rsi_midline_bot.py tune       # grid search + walk-forward; may write profiles.json
.venv/bin/python rsi_midline_bot.py tune --dry-run
.venv/bin/python rsi_midline_bot.py trades 50  # show trade journal
.venv/bin/python rsi_midline_bot.py pnl [db..] # round-trip P&L + losing-trade %
                                               # from journal(s); read-only
```

- The venv is Python **3.9** — keep `from __future__ import annotations`; no 3.10+ syntax at runtime.
- There is no test suite. Verification workflow: after touching `simulate()`,
  re-run `backtest` with unchanged settings and confirm the trail-off variant
  rows are **numerically identical** to before the change (this caught/validated
  past refactors). Baselines recorded before 2026-07-12 used raw bars and no
  friction: reproduce them with `BAR_ADJUSTMENT=raw COST_BPS=0`. A second identity check: the `active settings` backtest row
  must exactly match any hardcoded variant with the same parameters (e.g. the
  15Min profile reproduces `Band + vol + MA200`). Strategy-logic pieces
  (`rsi`, `crossover_signal`, `htf_trend_ok`) can be tested standalone with
  synthetic pandas Series/DataFrames — they don't need API keys.
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
  MA200 warmup. Friction is `COST_BPS` per side (default 0 = frictionless;
  a trade spanning a window edge is charged the full round trip).
- `simulate()` puts 100% into each symbol independently;
  `simulate_portfolio()` runs all symbols on one shared account with live
  sizing (`NOTIONAL_PCT` of current equity, cash-capped, exits before entries
  each bar, fractional shares) and reports equity curve, max drawdown, and
  exposure. Both consume `entry_exit_signals()` — the single source of signal
  truth; keep it that way.
- The HTF trend filter (`htf_trend_ok`) resamples the trading bars *up* to
  `HTF_TIMEFRAME` and aligns so each trading bar only sees HTF bars that had
  fully closed by the time the bar itself closed — preserve this no-look-ahead
  alignment (HTF bars are indexed at their end time; ffill onto
  `index + bar_len`). Disabled when trading `1Day`. Not in the tune grid;
  test HTF candidates via `backtest` env overrides (`active settings` row).
- `MA_TYPE` (sma/ema/hma, applies to the trend MA) and `HTF_RSI_LEVEL`
  (HTF RSI confirmation) are cfg-global, not per-variant `simulate()` params:
  when set they affect **every** backtest variant in the run, and variant
  names get a `[...]` suffix so `backtest_results.csv` rows stay unambiguous.
  Neither is in the tune grid.

## Live-trading invariants

- Signals are edge-triggered (cross, not level) and `enter`/`exit` skip when
  the position state already matches, so restarts and repeated passes are safe.
- `run_once` must fetch enough history for **every** active filter's warmup —
  the HTF filter especially (a 1Day MA50 needs ~90 calendar days of intraday
  bars; an all-NaN MA silently vetoes every buy). This bug shipped once
  (caught 2026-07-12 before the bot ever traded); when adding a filter, size
  the warmup and log-verify the filter can actually pass on live data.
- Trailing stops are **server-side GTC orders on Alpaca** — required because
  the daily loop sleeps ~23h between passes. Consequences baked into the code:
  whole-share entry sizing when `TRAIL_PERCENT` is set (stops can't hold
  fractional shares), `exit()` cancels open orders before `close_position`,
  and `reconcile_stop_fills()` journals stop fills that happened while the
  bot was asleep (deduped by `order_id`).
- Sizing: `NOTIONAL_PCT` sizes each entry as a percent of *current* account
  equity, capped at available cash so entries never lean on margin (falls
  back to flat `NOTIONAL` when unset). The user's plan: paper accounts reset
  to $2,000 with `NOTIONAL_PCT=25` to mimic the intended live account.
- Every order is journaled to SQLite (`trades.db`, path via `TRADES_DB`) with
  its signal context (RSI, relative volume, active settings incl. `htf_ma`).
  rvol is recorded even when the volume filter is off — intentional, for
  later analysis. Each deployed instance gets its own journal file.

## Data caveats (free Alpaca tier)

- Historical bars come from the IEX feed: prices are near-consolidated but
  **volumes are a small unreliable slice** — treat volume-filter backtests
  with suspicion.
- Bars are split/dividend-adjusted by default since 2026-07-12
  (`BAR_ADJUSTMENT=all`; set `raw` to reproduce older baselines). Adjustment
  shifts results materially: dividends add 3-6 points to 3-yr ETF buy & hold,
  and ex-div gaps previously created/removed whole RSI-cross trades on
  QQQ/SPY. Shipped to the droplet 2026-07-12, before the paper-test cohort's
  first trade.

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

From the 2026-07-11 walk-forward profile search (70/30 split, QQQ/GLD/IWM/SPY,
evidence in `backtest_results.csv`):

- On 1Day, the asymmetric 55/40 band beat the tuned 60/40 out-of-sample
  (+20.7% vs +19.3% test avg) — now the profile. MA0 scored identically OOS;
  MA50 kept as downtrend insurance.
- Plain RSI setups on 1Hour overfit badly (train +19.8% → test −0.6%); the
  1Day-MA50 HTF confirmation is what keeps hourly variants positive OOS.
- A 1Hour MA50 HTF filter on 15Min bars ≈ the MA200 trend filter (both look
  back ~50 trading hours) — HTF only adds new information with shorter MAs
  (e.g. MA20).
- The three paper-trading candidates (one per timeframe) are encoded in
  `deploy/env/*.env.example`, each on its own Alpaca paper account to avoid
  position collisions.

From the 2026-07-12 filter experiments (QQQ/GLD/IWM/SPY, adjustment=all,
frictionless, `EXP *` rows in `backtest_results.csv`):

- Multi-timeframe RSI confirmation (`HTF_RSI_LEVEL`, HTF RSI must be > level
  to buy) was tested on 1Hour (1D RSI>50) and 15Min (1h and 1D RSI>50):
  **worse everywhere**, full-window and out-of-sample, alone or stacked on
  the HTF MA — it lags price and re-allows entries late. Rejected; the knob
  exists but stays 0.
- Trend-MA type (`MA_TYPE`: sma/ema/hma) on 1Day 55/40 over 1095d: HMA50 and
  EMA50 beat-or-match the deployed SMA50 on every symbol full-window (avg
  +76.7% / +74.5% vs +67.7%; unfiltered +68.7%). All differences sit in the
  2025 downtrend (training window) — the last-30% OOS window had no vetoes,
  so every MA50 variant scores identically OOS and the tune gate cannot
  distinguish them. HMA200 whipsaws badly (+16.7%); avoid HMA at long periods.
