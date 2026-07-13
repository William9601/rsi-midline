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
.venv/bin/python rsi_midline_bot.py replay     # re-simulate the live period with
                                               # the resolved config and diff vs the
                                               # journal (TRADES_DB); read-only;
                                               # exit 1 on any mismatch
```

- The venv is Python **3.9** — keep `from __future__ import annotations`; no 3.10+ syntax at runtime.
- There is no test suite. Verification workflow: after touching `simulate()`,
  re-run `backtest` with unchanged settings and confirm the trail-off variant
  rows are **numerically identical** to before the change (this caught/validated
  past refactors). Baselines recorded before 2026-07-12 used raw bars and no
  friction: reproduce them with `BAR_ADJUSTMENT=raw COST_BPS=0`; intraday
  baselines recorded before 2026-07-13 also traded extended-hours bars — add
  `RTH_ONLY=false`. A second identity check: the `active settings` backtest row
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
- `RTH_ONLY` (default true): intraday simulations act only on bars whose
  close falls inside regular NY hours (9:30–16:00 ET, fixed — half-day early
  closes are ignored). IEX bars span 4am–8pm ET, but live `run_once` skips
  closed markets and signals are edge-triggered on the latest bar, so an
  off-hours cross is **missed live, not delayed** — before this mask ~half of
  intraday backtest trades happened at times live could never act
  (2026-07-13 finding). Indicators still warm up on every bar, matching live.
  Masked in `entry_exit_signals`, so both simulators and `tune` inherit it;
  a bar ending exactly at 16:00 is not actionable (completes at the bell).
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

## Replay verification (live vs backtest, weekly)

`replay` re-simulates the live period (flat start at `REPLAY_SINCE`, the
instance's go-live moment) with the same `entry_exit_signals()` the backtest
uses, then diffs simulated trade events against the journal bar-by-bar —
`SIM-ONLY`/`LIVE-ONLY` rows are live-vs-backtest divergences (this is the
check that would have caught the extended-hours gap on day one). Deployed as
`rsi-replay.timer` (Saturdays; `deploy/replay-check.sh` loops over
`deploy/env/*.env`). Maintenance rules:

- `_sim_trade_events` must mirror live's decision sequence: it consumes
  `entry_exit_signals()` and replicates `enter`/`exit` position-state
  skipping. When changing live behavior or `simulate()`, keep it in step.
- `_live_fetch_days()` is the shared warmup-depth source for `run_once` and
  `replay` — change fetch sizing there only.
- Journal timestamps map to signal bars via `_bar_for_ts` (intraday: last
  bar-close ≤ ts; daily: the ET-date bar, since live evaluates ~15:50 ET).
- **Update `REPLAY_SINCE` in the instance env file on every config swap**,
  or replay simulates the new config over the old config's trades.
- Expected honest disagreements (explain, don't "fix"): daily rows evaluated
  10 min pre-close on a near-final bar; `BAR_ADJUSTMENT=all` re-adjusts
  history after each dividend; trailing-stop fills at intraday prices.

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
  with suspicion. Intraday bars cover the extended 4am–8pm ET session;
  pre-market bars are thin (a 15Min "close" can be one odd-lot print). See
  `RTH_ONLY` above for how the simulators handle this.
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
- Midline-rejection entries (buy the RSI hook-up from a 40–50 pullback
  instead of / in addition to the cross above 55; tested via a scratch
  subclass overriding `entry_exit_signals`, 1Day 1095d, exits unchanged):
  as a **replacement** it roughly halves returns (avg +29% vs +68%
  full-window, +14% vs +22% OOS) — rejection-only entries sit out V-shaped
  recoveries where RSI regains 50+ without ever pulling back into the zone,
  and this strategy's edge is being aboard those legs. As an **addition**
  (cross OR hook) it's a wash (OOS avg +22.6% vs +22.4%). Rejected;
  `EXP hook`/`EXP cross|hook` rows in `backtest_results.csv`.
- Dynamic exits (1Day 1095d, deployed entries, custom simulator validated
  bit-for-bit against `simulate()` on the plain and trail-8% exits):
  Chandelier ATR trails lose to the plain RSI-cross-below-40 exit — 2xATR
  whipsaws (9→23 trades/symbol, avg +37% vs +68% full, +14% vs +22% OOS,
  and *worse* max DD on QQQ/SPY); 3xATR is milder but still worse (+45%
  full / +17% OOS). With any ATR trail on, the RSI exit almost never fires
  first, so it's effectively a replacement, and friction makes the extra
  churn worse. Selling 50% at RSI>=70 cuts avg max DD −12.8%→−9.2% but
  costs a third of full-window return and ~36% of OOS (+14.3% vs +22.4%)
  — RSI>70 is where the trend edge lives; even return/DD doesn't improve.
  Rejected; `EXP exit`/`EXP ATR`/`EXP part50` rows in `backtest_results.csv`.
- 630-combo sweep (RSI period 10/14/21 x buy 50-60 x sell 30-45 x MA
  0/20/50/100 sma/ema, 1Day 1095d + 2555d validation, 70/30 walk-forward;
  `EXP swp`/`EXP swp7y` rows): the deployed sell-40 exit is too shallow —
  **sell 30-35 beats sell 40 across nearly every period/band/MA and every
  symbol, on both windows, in both the 3y and 7y tests** (7y test avg:
  deployed +45.0% vs +58.7% for p14 55/30 ema50, +56.9% for 55/35 noMA,
  +55.1% for p21 52/35 sma50; test-min improves too). It's a plateau, not
  a spike, so unlikely to be overfit. Caveats: RSI-21 deep-sell variants
  drop to 3-8 trades/7y/symbol (basically buy & hold + crash filter; in the
  1095d OOS window they were long throughout, so that window can't rank
  them); all variants still trail 7y buy & hold. Candidate change:
  RSI_SELL_LEVEL 40 -> 35 (or 30) on the 1Day profile — not applied, and
  the `tune` grid (GRID_BANDS, fixed rsi_period=14) cannot see these combos.
- Intraday sweeps (same grid philosophy + HTF dimension, 365d, 70/30,
  frictionless with a COST_BPS=5 shortlist pass; `EXP swp1h`/`EXP swp15m`
  rows): **deep sell (30-35) is confirmed on all three timeframes.**
  1Hour: deployed bot#3 (p14 60/40 + 1D-MA50) is weak (train +1.0%, test
  +4.1%, train goes negative with friction); **p21 60/40 plain** (RSI-21,
  no filters) beats it on both windows and survives friction (train
  +9.2%/+7.3%, test +8.8%/+8.0%, 102 trades vs 118) — RSI-21's smoothing
  appears to do the HTF filter's whipsaw-suppression job; p14 50/30 has
  the best test (+12.2%/+11.2% with friction, test-min +0.5%) but mediocre
  train. 15Min: friction kills everything — deployed bot#2 (p14 55/45 +
  1h-MA20) backtests **negative at COST_BPS=5** (train -2.3%, test -1.4%,
  770 trades/yr) and no combo fixes it (best survivors are low-churn
  sell-30 variants at ~+4.5% test with flat train). 15Min is
  friction-bound; the paper test will measure real fills.
- Extended-hours audit (2026-07-13, time-of-day trade reconstruction on the
  deployed intraday variants): before `RTH_ONLY`, ~half of intraday backtest
  trades executed outside 9:30–16:00 ET where live never acts — **every
  intraday backtest and sweep recorded before 2026-07-13 (incl. `EXP swp15m`/
  `EXP swp1h`) overstates trade count ~2x and describes a different system**;
  conclusions survived a live-faithful re-check (15Min deployed rules,
  frictionless: QQQ +5.9→+5.0%, GLD +34.9→+23.2%, IWM +18.9→+15.8%, SPY
  +5.4→+10.3%, trades roughly halved) but re-derive any intraday number
  before relying on it. Same audit: losing trades overwhelmingly get
  *realized* at session opens (overnight gap-downs exiting on the first
  bars — on 15Min the 4am and 9:30–10:30 exit buckets held ~74% of
  cumulative losses; the 4am "fills" were thin pre-market prints live could
  never get). Entry-time-of-day patterns were noise by comparison; first-30min
  15Min entries ≈ breakeven pre-cost. 1Min loses even frictionless
  (−17%/90d, 2486 trades). Untested candidate: exit-before-close variant to
  kill overnight gap risk (would also give up overnight drift).
- RTH re-sweep (2026-07-13, `EXP swpRTH15m`/`EXP swpRTH1h` rows: 432 combos
  per timeframe, 365d, 70/30 walk-forward ranked on train only, friction
  applied analytically at 5bps): under live-faithful RTH rules friction no
  longer kills everything intraday (265/432 15Min combos positive on both
  windows at c5 — the all-hours sim had doubled the friction bill). But
  15Min train→test decay is severe (best train ~+16% → test +1-3% avg):
  treat any 15Min "edge" as noise. Deployed 15Min bot#2 ranks 197/432
  (test c5 −3.0%). **The deployed 1Hour p21 60/40 (bot#3) was picked on
  all-hours evidence that did not survive the correction**: under RTH rules
  it ranks 218/432 with train c5 +0.9% (test +5.2%, test-min −7.6%). Most
  robust 1Hour region now: **p14 55/45 plain** (train +8.9, test +4.5,
  test-min +4.1 — positive on every symbol in both windows, 44 trades/yr)
  sitting on a plateau (p10 50/45, p10 55/40, p10 55/45 v1.5 also positive
  both windows). **Applied 2026-07-13**: bot#3 swapped to p14 55/45 plain
  (droplet env + example + 1Hour profile) before its first trade. 1Day
  numbers are untouched by RTH and remain the best timeframe.
- Structural tests (2026-07-13, `EXP struct15m`/`struct5m`/`struct5m2y`
  rows; 730d fetch so year-1 = 2024-07→2025-07 was never used by any
  selection decision; COST_BPS=5 analytic, RTH rules): **the RTH-sweep
  15Min winners collapse on the fresh year** (p10 60/35 MA50: +19.3%
  familiar year → −10.5% fresh; p14 60/40 MA50 likewise) — overfit
  confirmed by data. Only the low-churn deep-sell family stays positive on
  both years on 15Min (p21 50/35: +4.6/+17.3; p14 50/30 v1.5: +5.2/+13.1)
  but that ≈ buy & hold with worse per-symbol risk. The same family on
  5Min looked strong on the familiar year (+8/+11.5 with friction) and
  deflated on the fresh one (+1.8/−2.7 avg, worst symbol −17.6%).
  **Exit-before-close is a disaster everywhere** (−5% to −26%, both years,
  both timeframes, every config): overnight drift is this strategy's profit
  engine and gap risk is the cost of it — do not engineer it away.
  Skip-entries-before-10:00 ≈ noise (±1-2pts; mildly helpful on 5Min).
  Verdict: **sub-1Hour timeframes are ruled out for edge**; the 15Min bot
  remains deployed only as a live friction-measurement experiment.
- Time stops / stagnation exits (2026-07-13, `EXP tstop1d`/`EXP tstop1h`
  rows; deployed configs, 2555d/730d, 70/30, c5): **pure time stops (exit
  after N bars regardless) are ruinous** (1Day full 7y +119%→+21..+72%;
  1Hour OOS negative) — more proof the edge is holding through dull
  stretches. **Stagnation exits (exit at N bars only if price ≤ entry) do
  cut losses as hypothesized** — 1Day stag-N10: loss total −31.3%→−25.1%,
  OOS worst-symbol +23.4%→+28.0%, full 7y +119→+131 — but OOS *average*
  is slightly worse (+41.6→+39.8) because freed dead trades sometimes
  wake up: risk improves, return doesn't. On 1Hour every variant is worse
  OOS. Rejected for deployment (no return edge, one more fitted knob);
  1Day stag-N10 is the only borderline candidate if drawdown ever becomes
  the binding constraint.
- Trend-MA type (`MA_TYPE`: sma/ema/hma) on 1Day 55/40 over 1095d: HMA50 and
  EMA50 beat-or-match the deployed SMA50 on every symbol full-window (avg
  +76.7% / +74.5% vs +67.7%; unfiltered +68.7%). All differences sit in the
  2025 downtrend (training window) — the last-30% OOS window had no vetoes,
  so every MA50 variant scores identically OOS and the tune gate cannot
  distinguish them. HMA200 whipsaws badly (+16.7%); avoid HMA at long periods.
