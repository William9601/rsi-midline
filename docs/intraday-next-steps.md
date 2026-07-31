# Intraday next steps — CONCLUDED (kept as a record)

**Status (2026-07-31, updated): intraday work is closed — four-for-four null.**
Both directions scoped below were tried:
- **Option A (leveraged + risk-based sizing) — abandoned without building.** A
  sanity ORB backtest on TQQQ/SOXL/SPXL confirmed the *signal* is OOS-negative
  at 5bps there too; and since `simulate()` is 100%-invested per symbol, the
  per-symbol OOS % is sizing-independent, so risk-based sizing can only
  reallocate capital — it can't flip a negative per-trade expectancy. The
  premise (wrong instruments + wrong sizing) didn't survive contact.
- **Option B (cross-sectional / market-neutral) — BUILT as `pairs_bot.py`, null.**
  QQQ/SPY + IWM/SPY, rolling-OLS-beta spread reversion, two-sided book, flat
  overnight. Frictionless edge is a fraction of a percent/year on gross;
  collapses to −12 to −22% at 5bps (a pairs round trip is 4 fills); hedge ratio
  drifts OOS. Research-only — live shorting was deliberately never built.

Full evidence: the **2026-07-31 findings in `CLAUDE.md`** (RSI-trend, MR, ORB,
and pairs blocks) plus `mr_backtest_results.csv` / `orb_backtest_results.csv` /
`pairs_backtest_results.csv`.

**The conclusion:** this liquid-ETF retail setup is **friction-bound with no
durable intraday edge — absolute or relative.** The 1Day trend bot is the edge.
Do not reopen intraday without a genuinely different regime (a real transaction-
cost advantage, higher-vol instruments with proper risk sizing that a small
paper account can't mimic, or a data/latency edge none of this infra assumes).

**Location note:** all the research intraday code + data now lives under
`intraday/` (`intraday/mean_reversion_bot.py`, `intraday/orb_bot.py`,
`intraday/pairs_bot.py`, `intraday/intraday_common.py`, and the
`intraday/*_backtest_results.csv` logs), moved there to keep the deployed
`rsi_midline_bot.py` uncluttered. Run them as `python intraday/<bot>.py <mode>`
from the repo root. The file paths in the original scoping below predate the
move — prepend `intraday/`.

The original scoping of Options A/B is kept below for the record.

---

## What you can reuse

The intraday scaffolding is proven and directly reusable — don't rebuild it:

- `intraday_common.py` — `rth_actionable`, `session_last_actionable`,
  `session_ids`, RTH constants. **One live-actionable definition for every
  sub-daily bot.** Any new intraday bot MUST use these so it can't drift from
  live (the extended-hours gap that shipped once is exactly what this prevents).
- `orb_bot.py` / `mean_reversion_bot.py` are the template for a new bot: Config
  precedence (env > .env > profile > default), a `signals()`/`entry_exit_signals()`
  single source of truth feeding both `simulate()` and `simulate_portfolio()`,
  the close-based position state loop, per-bot journal + `*_backtest_results.csv`
  + profiles, and the `backtest`/`portfolio`/`run`/`loop`/`pnl` CLI. Copy the
  structure; swap the strategy math.
- Reuse `rsi_midline_bot.py`'s pure helpers (`rsi`, `ma_series`, `htf_trend_ok`,
  `load_dotenv`, `show_pnl`) by import, as both intraday bots already do.
- The offline engine tests in the scratchpad (`test_mr_engine.py`,
  `test_orb_engine.py`) are the pattern for verifying a new engine without API
  keys (dummy env keys, synthetic bars, assert no-look-ahead / RTH / flat-close
  / sim==portfolio / frictionless reproducibility). Rewrite them for the new
  strategy — this caught real bugs here.

## Non-negotiable norms (same as the daily bot)

- **Validate OOS at `COST_BPS=5` BEFORE any deploy scaffolding.** Friction is
  the binding constraint intraday (~180 trades/yr ≈ ~18% annual drag at 5bps).
  A frictionless-only "edge" is not an edge.
- **Walk-forward, ranked on train, judged on the held-out test window.** Both
  failures above looked fine in-sample; the test window is where they died.
- **A null result is a valid, valuable outcome — report it honestly and stop.**
  Do not fit knobs until the test window turns green.
- Fills are close-based in these engines (stops/targets on bar closes) —
  optimistic on gap-through bars. Risk-based sizing (option A) makes the fill
  model matter more; consider modeling the intrabar stop touch (bar low ≤ stop)
  if you go there.

---

## Option A — Leveraged / high-volatility instruments with risk-based sizing

**Why:** the published ORB edge (Zarattini & Aziz, 2023) is on *leveraged*,
high-volatility single names (e.g. TQQQ), sized to a fixed **risk per trade**
(distance to stop), not a fixed % of equity. Our ORB used liquid ETFs and
`NOTIONAL_PCT` sizing — both dampen exactly what that edge feeds on. This is the
smallest step from what already exists.

**Concrete plan:**
1. Universe: high-volatility / leveraged ETFs — e.g. TQQQ, SOXL, SPXL, and
   maybe high-beta single names. Confirm the account can trade them on paper.
2. Add **risk-based sizing** to `orb_bot.py` (new module or a config mode):
   size each entry so `(entry - stop) * shares = RISK_PCT * equity` (e.g. risk
   1% of equity per trade), instead of `NOTIONAL_PCT`. This changes the
   position-sizing in both `simulate_portfolio()` and live `position_notional()`.
3. Test the same ORB variants + an ATR-normalized entry filter (only take
   breakouts with an opening range wide enough / a volume surge — the paper
   gates on relative volume).
4. Same gate: walk-forward at 5bps, then portfolio DD/exposure.

**Risks to flag to the user up front:** leveraged ETFs have decay and gap risk;
a small paper account mimicking a modest live account means position sizing and
max-DD tolerance need explicit sign-off. This is a higher-risk profile than the
daily bot — get agreement on the universe and RISK_PCT before building.

## Option B — Cross-sectional / market-neutral reversion

**Why:** the surviving statistical edges intraday are usually *relative*
(spread between correlated instruments) rather than *absolute* (one name's
price). SPY/QQQ/IWM all crash together, so a single-name long-only bet is really
one market bet; a spread bet can be closer to market-neutral.

**Concrete plan:**
1. Pick a correlated pair/basket (e.g. QQQ vs SPY, or a sector vs its index).
   Compute the spread (ratio or hedge-ratio residual), z-score it intraday.
2. Enter when the spread is stretched (long the cheap leg, short the rich leg),
   exit on reversion / stop / session close.
3. **This needs shorting** — a real departure from the long-only infra:
   `submit_order` short legs, margin/borrow checks, per-leg journaling, and a
   simulator that models both legs. Bigger build; the `simulate_portfolio`
   two-sided accounting is the hardest new piece.
4. Same OOS-at-5bps gate, plus check the hedge ratio is stable out-of-sample
   (a fitted ratio that drifts is another overfit trap).

**Risks:** shorting, borrow availability/cost, and a materially larger code
surface (two-sided fills, margin). Highest effort of the two.

---

## Recommendation

Start with **Option A** — it's the smallest delta from the working ORB engine,
it directly addresses why the published edge didn't show up here (wrong
instruments + wrong sizing), and it doesn't require shorting. If it also comes
up null, that's strong evidence to **stop intraday work entirely** and treat the
daily trend bot as the edge — which is where the accumulated evidence already
points. Only reach for Option B if the user specifically wants a market-neutral
book and accepts the shorting/complexity cost.

**First commands for Option A** (after confirming universe with the user):
```bash
# sanity: does ORB behave differently on a leveraged name, current engine?
TIMEFRAME=5Min BACKTEST_DAYS=365 COST_BPS=5 SYMBOLS=TQQQ,SOXL,SPXL \
  .venv/bin/python orb_bot.py backtest
```
Then implement risk-based sizing and re-run. Expect to re-derive, not trust,
every number.
