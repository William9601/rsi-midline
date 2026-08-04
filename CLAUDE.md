# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A long-only RSI-midline trading bot for Alpaca: buy when RSI crosses above a
level, sell when it crosses below (with optional band, trend-MA, volume,
higher-timeframe-trend, and trailing-stop filters). The **deployed** bot is one
module: `rsi_midline_bot.py`. Everything else is config (`profiles.json`, `.env`,
`deploy/env/`) or generated data (`trades.db`, `backtest_results.csv` — the
append-only experiment log every backtest writes to).

Three sibling **research-only** intraday bots, all built and **rejected** on
2026-07-31 (nothing deployed), live isolated under **`intraday/`** so they can't
clutter or interfere with the deployed day-bot: `intraday/mean_reversion_bot.py`
(session-VWAP mean reversion), `intraday/orb_bot.py` (Opening Range Breakout),
and `intraday/pairs_bot.py` (cross-sectional / market-neutral pairs reversion —
the only two-sided/shorting design; live trading is a stub, it never proved out).
They share `intraday/intraday_common.py` (one RTH-actionable mask for every
sub-daily bot) and **import** `rsi_midline_bot.py`'s pure helpers (a one-way
dependency — the main bot imports none of them; each moved module inserts the
repo root on `sys.path` so the import still resolves when run as
`python intraday/<bot>.py ...`). Each has its own Config / journal / profiles /
`intraday/*_backtest_results.csv`. Intraday work is **concluded** — all four
attempts (RSI-trend + these three) failed; see `docs/intraday-next-steps.md` and
the 2026-07-31 pairs findings below.

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
  `RTH_ONLY=false`; intraday baselines from 2026-07-13/14 used a stricter RTH
  mask that skipped last-pre-open-bar signals live does act on (corrected
  2026-07-15, not reproducible via env). A second identity check: the `active settings` backtest row
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
- `RTH_ONLY` (default true): intraday simulations act only on bars live can
  actually evaluate. Live polls the *latest completed* bar while the market
  is open, so a bar is actionable iff the market is open at some instant
  between its close and the next bar's close: every bar closing inside
  9:30–16:00 ET (exclusive of 16:00 — that bar completes at the bell), PLUS
  the last bar to complete at-or-before the open (the 8–9am 1Hour bar, the
  9:15–9:30 15Min bar), which the first post-open poll still sees as the
  newest bar. Earlier off-hours crosses are superseded before live can act —
  before this mask ~half of intraday backtest trades happened at times live
  could never act (2026-07-13 finding); the pre-open-bar case was confirmed
  live 2026-07-14 (QQQ/GLD/IWM 1Hour entries at 9:51 ET on the 8–9 bar) and
  added 2026-07-15 — **intraday backtests recorded between those dates
  dropped last-pre-open-bar signals live does take**. Fixed 9:30–16:00,
  half-day early closes ignored; a pre-open bar's fill is booked at its own
  close though live fills after 9:30; assumes POLL_SECONDS ≤ 30 min on
  1Hour bars. Indicators still warm up on every bar, matching live. Masked
  in `entry_exit_signals`, so both simulators and `tune` inherit it.
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

## Data caveats

- The account has Alpaca's **Algo Trader Plus** subscription (predates this
  project, confirmed 2026-07-13): bar fetches default to the consolidated
  **SIP** feed — real full-market prices AND volumes — so **every row in
  backtest_results.csv and all live trading have been SIP from day one**.
  Verified empirically: SPY 9:45 ET bar on 2026-07-13 is 1.49M shares on SIP
  vs 36k on IEX (closes 753.62 vs 753.59); the live journal price matches
  SIP, so the droplet's paper keys inherit the subscription too.
  `DATA_FEED=iex` fetches free-tier data (never used here; on 15Min bars it
  produces materially different RSI signals — feed is part of the system).
  If the subscription ever lapses, fetches silently fall back to IEX — the
  "volumes are a small unreliable slice" free-tier caveat would then apply.
  **The SIP subscription is a running cost of LIVE operation, not a
  backtest-only cost** (`loop`/`run` fetch SIP bars every pass to compute the
  RSI signals — the price series *is* the strategy). Only safe to cancel if the
  bot is being **retired**; while it trades, keep it. Quantified 2026-08-02 on
  the deployed 1Day config (55/35, QQQ/GLD/IWM/SPY/DBC, 7y frictionless,
  scratchpad-only — CSV not polluted): SIP->IEX dropped every symbol's return
  34-108 pts (QQQ +188.5%->+80.0%, SPY +180.5%->+103.2%) and shifted trade
  counts (crosses fire on different bars). Even buy & hold diverged (QQQ
  +298%->+174%), largely because IEX returned 7554 bars vs SIP's 8785 (~1yr
  less depth/symbol). And since orders fill at real *consolidated* prices
  regardless of data tier, an IEX-driven live bot decides on thin prices but
  fills elsewhere — a double mismatch that also breaks `replay` reconciliation.
- Intraday bars cover the extended 4am–8pm ET session. Off-hours bars are
  real consolidated prints on SIP, but live still can't act on them (market
  closed) — the `RTH_ONLY` mask is about market hours, not feed quality.
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
  MA50 kept as downtrend insurance. (SUPERSEDED 2026-07-30: on the 7y window
  MA50 is net-negative — the trend MA was removed from the 1Day profile; see
  the 2026-07-30 findings below.)
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

From the 2026-07-30 daily re-tune (QQQ/GLD/IWM/SPY, 7y/2555d, 70/30
walk-forward, COST_BPS=5; per-symbol `simulate()` + `portfolio` sim, both
the engine `replay` proved matches live; evidence rows in
`backtest_results.csv`, live P&L that motivated it in the journals):

- **The 1Day trend MA was removed (`TREND_MA_PERIOD` 50 -> 0).** Isolating the
  knob at a fixed 55/35 band over 7y: dropping MA50 improved OOS test avg
  +43.5% -> +49.0% and worst-symbol OOS +18.0% -> +31.9%, and full-account
  (portfolio, 25%/pos) return +124.7% -> +135.2% (CAGR +12.3 -> +13.0%) for
  only +1.6pts more max DD (-18.1% -> -19.7%, still far under buy & hold's
  -27%). `p14 55/35 no-MA` was the single most robust combo (highest
  worst-symbol OOS) on a plateau (55/30, 50/35, 60/30 no-MA also top-12);
  `no-MA > MA50 > MA200` at every sell level full-window incl. the 2022 bear
  and 2025 downtrend. The old "MA50 as insurance" call was a 3y-window
  artifact (that OOS period had no MA vetoes, so MA0/MA50 looked identical).
  RSI-14 and sell-35 retained — both plateau centers (p21 posts big train
  numbers but ~1 trade/yr/symbol ≈ buy & hold + crash filter; p10 churns).
- **Expanding the universe: surgical beats broad — diversification dilutes
  the edge.** The strategy's return is concentrated in a few strong trenders,
  so a broad add is counterproductive: an 8-symbol diversified set
  (SPY/QQQ/IWM/GLD/DBC/SLV/EEM/VNQ @12%) fell to +98% return vs current-4's
  +139%. **The one clean add was DBC** (broad commodities) — the only
  candidate both a decent trender on this strategy (+34% OOS, 54% win, 13
  trades) AND genuinely uncorrelated to the US-equity core (SPY/QQQ/IWM all
  crash together; adding equity like XLK ≈ QQQ just piles on correlated risk).
  `4 + DBC @20%` (slots 25 -> 20 so all 5 fit) held return flat (+138% vs
  +139%), cut max DD -19.7% -> -15.3% (best risk-adjusted in the test, Ret/DD
  9.05 vs 7.04), and raised activity ~22% (8.4 -> 10.2 trades/yr). Applied to
  the 1day instance 2026-07-30 (`deploy/env/1day.env(.example)`;
  `SYMBOLS=QQQ,GLD,IWM,SPY,DBC`, `NOTIONAL_PCT=20`).
- **Symbol selection is by trend-worthiness (OOS walk-forward), NOT
  transaction volume, and NOT a live daily scanner.** Volume is only a
  liquidity floor every large ETF clears; this strategy profits from trend
  persistence, so a high-volume chopper just adds whipsaw. A live market
  scanner is worse still: it can't be honestly backtested (survivorship +
  look-ahead — "today's liquid list run backward" is biased) and it breaks
  `replay`'s fixed-universe reconciliation. Re-select periodically OFFLINE
  (rank a candidate pool by OOS walk-forward, commit the new `SYMBOLS` with
  evidence) if the universe needs refreshing. Caveat on all of the above:
  the 7y OOS window is bull-heavy, so a bare "OOS > 0" filter is too weak —
  it failed to reject known choppers (XLE +6%, USO +7%); trust the *relative*
  ranking and require a real diversification mechanism, not just a positive
  number. TLT was the one outright DROP (OOS -10%).
- **Motivating context:** the losses prompting this work were entirely
  intraday — 1Hour (-7.0%, 82% losing) and 15Min (-7.2%, 80% losing) were
  whipsawing live exactly as backtests predicted, while the 1Day bot had
  barely traded. The highest-leverage fix for daily was frequency (more
  symbols) + shedding the return-draining MA, not a new exit knob.

From the 2026-07-31 intraday strategy search (two NEW standalone bots,
SPY/QQQ/IWM at 15Min + 5Min, 70/30 walk-forward, evidence in
`mr_backtest_results.csv` / `orb_backtest_results.csv`). Goal: find an intraday
edge in a *different* factor than the trend bot, which structurally whipsaws
intraday. **Both bots were validated and REJECTED — kept as tested research
engines, not deployed.** The engines are sound (offline checks: no look-ahead,
RTH-actionable parity with live, flat-at-close, single-symbol sim == portfolio
sim, frictionless reproducibility); the strategies simply have no edge.

- **Mean reversion (`mean_reversion_bot.py`) — null, worse than ORB.** Buy
  z-score dips below session VWAP (z <= -ENTRY_Z) with RSI2 oversold; exit to
  VWAP / z-stop / % stop / session close; flat overnight; optional daily-trend
  regime gate. **Negative even frictionless, in- AND out-of-sample**, every
  variant/symbol/timeframe (15Min OOS -2 to -4% frictionless, -7 to -10% at
  5bps; 5Min same-or-worse). Root cause is a **structural asymmetry**: exit-at-
  VWAP caps each winner at ~2σ (~0.3%) while a dip that keeps falling is
  force-closed at the session bell for a much bigger loss — ~50-57% win rate
  but negative expectancy *before* any cost. Adding a stop made it far worse
  (churn + realized knife-catches). The regime filter halved the loss (confirms
  it's knife-catching in downtrends) but stayed negative. Not fixable by tuning.
- **Opening Range Breakout (`orb_bot.py`) — null, but an in-sample tease.**
  Define the first OR_MINUTES of RTH as the opening range; buy when a bar
  *closes* above the range high (at most once/day); stop at OR low / % / R;
  optional R target; flat at close. Time-anchored momentum (fires ~1x/day), so
  it dodges the continuous-whipsaw problem. **Frictionless it shows a real
  in-sample tendency** (train +6 to +16%, win 52-61%) **but no OOS persistence**
  — the held-out test window is flat-to-negative *even frictionless* (-0.4 to
  -4.7%). At 5bps, ~180 trades/yr = ~18% friction drag buries it (-7 to -18%).
  Portfolio (25%/pos): frictionless only **+5.7% vs buy&hold +24.5%** (14% avg
  exposure, mostly in cash); **-7.3% at 5bps**. No lever moved OOS: OR length
  (5/15/30), stop style (OR-low / 0.5R), 2R target, or the regime gate — all
  negative held-out. Fills are close-based (stops/targets on closes), which is
  *optimistic* on gap-through bars, so the real result is no better.
- **Verdict (three-for-three against intraday, later four).** RSI-trend lost
  live, mean reversion is null, ORB is null — all confirm sub-daily is
  **friction-bound with no durable long-only edge for this liquid-ETF retail
  setup**. The 1Day trend bot is the edge. The two scoped escapes from that
  design space were tried next: Option A (leveraged/high-vol + risk-based sizing)
  was pre-checked and abandoned; Option B (cross-sectional/market-neutral) was
  built as `pairs_bot.py` and also came up null — see the pairs findings below,
  which close out intraday work. When re-deriving any intraday number, remember
  fills are close-based and the 7y-bull-heavy + RTH-mask caveats apply.

From the 2026-07-31 market-neutral pairs search (`pairs_bot.py`, a THIRD
standalone intraday bot and the first two-sided/shorting design; QQQ/SPY +
IWM/SPY at 5Min + 15Min, 365d, 70/30 walk-forward; evidence in
`pairs_backtest_results.csv`). Goal (Option B from `docs/intraday-next-steps.md`):
find a *relative* (spread) edge where absolute long-only bets failed, since
SPY/QQQ/IWM crash together so a single-name bet is really one market bet. Built
research-first per the project norm — the two-sided *edge* is fully backtestable
by simulating both legs, so **no live shorting infra was built** (`run`/`loop`
are stubs that refuse). **Validated and REJECTED — null, friction-bound.**

- **Strategy.** Spread `s = logA − beta·logB`, beta a ROLLING OLS hedge ratio
  (Cov/Var over BETA_LOOKBACK bars); z-score `s` over Z_LOOKBACK. z ≤ −ENTRY_Z
  (A cheap vs B) → long A / short B; z ≥ +ENTRY_Z → short A / long B; exit on
  reversion (|z| ≤ EXIT_Z) / blow-out stop (|z| ≥ STOP_Z) / session close (flat
  overnight). A 3-state machine (flat / long-spread / short-spread) with
  beta-neutral two-sided P&L quoted **on gross exposure** (|long$|+|short$|).
- **Engine is sound** (offline `test_pairs_engine.py`: beta/z no look-ahead,
  RTH-actionable parity, flat-at-close, 3-state correctness incl. no direct
  long↔short flip, single-pair `simulate()` return-on-gross reconciles with the
  two-sided `simulate_portfolio()` MTM, frictionless reproducibility).
- **Frictionless the "edge" is economically negligible** — a fraction of a
  percent to ~2-3% per YEAR on gross, ~50% win rate: 5Min QQQ/SPY test +1.7%,
  IWM/SPY +0.1%; 15Min test +0.2-1.9%. Portfolio (25%/pair gross) only **+0.9%**
  frictionless over the year at 10% avg gross exposure.
- **At 5bps it collapses, every pair / timeframe / variant** (a pairs round trip
  is 4 fills = 2×COST_BPS of gross; ~500 trades/yr on 5Min, ~190 on 15Min).
  5Min test −12 to −17%, 15Min test −2 to −9%; **portfolio +0.9% → −21.7%** at
  5bps, win rate 50% → 11%. The spread signal is far too small to clear even
  modest friction. No lever helped (ENTRY_Z 1.5-2.5, EXIT_Z, STOP_Z, Z_LOOKBACK,
  static β=1 vs rolling — static was worse everywhere).
- **Hedge ratio is unstable OOS** (the overfit tell flagged in the doc): entry
  beta drifts train→test (QQQ/SPY 1.30±0.26 → 1.63±0.43 on 15Min) with wide
  dispersion — another reason not to trust the thin frictionless edge.
- **Verdict (four-for-four against intraday) → intraday work is DONE.** Long-only
  trend (live), VWAP mean reversion (null), ORB (null), and now market-neutral
  pairs (null) all confirm this liquid-ETF retail setup is friction-bound with no
  durable intraday edge, absolute OR relative. The 1Day trend bot is the edge —
  do not reopen intraday without a genuinely different regime (a real transaction
  cost advantage, higher-vol instruments with proper risk sizing, or a
  data/latency edge none of this infra assumes). Option A (leveraged + risk
  sizing) was pre-checked this session and left unbuilt: the ORB *signal* is
  OOS-negative on TQQQ/SOXL/SPXL too, and since `simulate()` is 100%-invested
  per symbol, risk-based sizing only reallocates — it can't flip a negative
  per-trade expectancy.
