# RSI Midline Trading Bot (Alpaca)

A long-only Python trading bot for Alpaca implementing the **RSI midline strategy**:

- **Buy** when RSI crosses **above 50** (momentum turning bullish)
- **Sell / close** when RSI crosses **below 50** (momentum turning bearish)

RSI uses Wilder's smoothing, and signals only fire on completed bars so a
still-forming intraday bar can't trigger a trade.

Optional filters gate the entries: an RSI band (buy/sell at different levels
instead of one midline), a trend moving average, a volume-confirmation
multiple, a **higher-timeframe trend check** (e.g. trade 15-minute bars only
while the 1-hour trend is up), and a server-side trailing stop.

## Setup

```bash
cd rsi-midline-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # add your Alpaca API keys
```

The bot loads `.env` automatically at startup — no need to source it.

Get free paper-trading API keys at https://app.alpaca.markets (Paper Trading →
API Keys). The bot defaults to the paper endpoint; it only trades real money if
you explicitly set `ALPACA_PAPER=false`.

## Usage

```bash
python rsi_midline_bot.py backtest   # sanity-check the strategy on history first
python rsi_midline_bot.py run       # evaluate signals once and place orders
python rsi_midline_bot.py loop      # keep running, checking every POLL_SECONDS
python rsi_midline_bot.py tune      # grid-search + walk-forward validation
python rsi_midline_bot.py trades    # show the trade journal (add a number for more rows)
```

### Backtesting variants & the experiment log

`backtest` compares a menu of strategy variants side-by-side (plain cross,
band, trend MA, volume, trailing stop, higher-timeframe trend confirmation)
plus an **`active settings`** row built from your resolved configuration.
Since shell variables override everything, that row lets you A/B any
candidate profile without touching config files:

```bash
TIMEFRAME=15Min RSI_BUY_LEVEL=60 RSI_SELL_LEVEL=40 HTF_MA_PERIOD=20 \
  python rsi_midline_bot.py backtest
```

Every run also appends one row per symbol/variant to `backtest_results.csv`
(timestamp, full parameter set, trades, win rate, return, buy & hold), so
experiments stay comparable long after the terminal scrollback is gone.

### Trade journal

Every order is recorded in `trades.db` (SQLite, gitignored) with the full
context that triggered it: price, RSI, relative volume, timeframe, the active
band/filter settings, and whether it was a paper trade. This is what lets you
later compare live behavior against what the backtests promised — e.g. join
trades into round trips and check the realized win rate per settings profile.
Query it directly with `sqlite3 trades.db "SELECT * FROM trades"` or via
`python rsi_midline_bot.py trades 50` for the last 50 entries. Set `TRADES_DB`
to change the location.

### Tune mode

`tune` grid-searches band levels × trend MA × volume filter × trailing stop
(81 combos) for the current `TIMEFRAME`, with honest walk-forward validation:

1. The winner is picked on the **first 70%** of history only (train).
2. It's then evaluated on the held-out **last 30%** (test) it never saw.
3. `profiles.json` is only updated if the winner also beats the *current*
   profile on that out-of-sample window — otherwise the run reports why and
   changes nothing. Use `--dry-run` to never write.

A combo that shines in train but collapses in test is overfit; the gate
exists to keep those out of your profiles. Set `TRAIN_SPLIT` (default `0.7`)
to change the split, and `BACKTEST_DAYS` to widen the history (use 1000+ for
`1Day`). The grid does **not** search the higher-timeframe filter — test
those candidates via `backtest` with env overrides (see above).

## Per-timeframe profiles

`profiles.json` records the tuned settings for each timeframe **and the
backtest evidence behind them**. On startup the bot applies the profile
matching your `TIMEFRAME`, so switching timeframes automatically switches the
band levels, volume filter, and trend filter to what tested best there.

Precedence: anything set explicitly in `.env` or the shell **overrides** the
profile — profiles only fill in settings you haven't chosen yourself. The
startup log shows exactly which values came from the profile.

Current findings (walk-forward tested 2026-07-11 on QQQ/GLD/IWM/SPY;
evidence rows in `backtest_results.csv`):

- **1Day** — band 55/40 + MA50. The asymmetric band beat the previous 60/40
  profile out-of-sample (+20.7% vs +19.3% test avg). Daily bars remain by far
  the strongest timeframe for this strategy.
- **15Min** — profile keeps band 55/45 + volume ×1.5 + MA200. A leaner
  candidate (55/45 + 1-hour MA20 trend confirmation, no volume/MA200) tripled
  the training return with similar out-of-sample results and is being paper
  traded (see `deploy/env/`).
- **1Hour** — plain RSI setups overfit badly here (train +19.8% collapsed to
  −0.6% out-of-sample); a daily-trend confirmation (1Day MA50) is what keeps
  hourly variants positive. Profile entry still marked untested; the paper
  candidate lives in `deploy/env/`.
- **5Min / 1Min** — guesses, not backtested.

When you re-tune, update `settings`, `tuned`, and `notes` in `profiles.json`
so the evidence stays with the numbers.

## Configuration

Everything is set via environment variables (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `SYMBOLS` | `SPY,AAPL,MSFT` | Comma-separated tickers to trade |
| `TIMEFRAME` | `15Min` | Bar size: `1Min`, `5Min`, `15Min`, `1Hour`, `1Day` |
| `RSI_PERIOD` | `14` | RSI lookback period |
| `MIDLINE` | `50` | Crossover threshold |
| `RSI_BUY_LEVEL` | `MIDLINE` | Band variant: buy when RSI crosses above this |
| `RSI_SELL_LEVEL` | `MIDLINE` | Band variant: sell when RSI crosses below this |
| `TREND_MA_PERIOD` | `0` (off) | Only buy when price is above this MA (in bars) |
| `VOLUME_MULT` | `0` (off) | Only buy when signal-bar volume ≥ this × recent average |
| `VOLUME_LOOKBACK` | `20` | Bars used for the average-volume baseline |
| `TRAIL_PERCENT` | `0` (off) | Trailing stop % below high-water mark (server-side GTC) |
| `HTF_MA_PERIOD` | `0` (off) | Higher-timeframe trend confirmation: only buy when the close is above this MA on `HTF_TIMEFRAME` bars (intraday timeframes only) |
| `HTF_TIMEFRAME` | `1Hour` | Higher timeframe for that check: `1Hour`, `4Hour`, `1Day` |
| `NOTIONAL` | `1000` | Dollars per new position |
| `NOTIONAL_PCT` | `0` (off) | Size each entry as this % of current account equity instead (overrides `NOTIONAL`; capped by available cash so entries never use margin) |
| `TRADES_DB` | `trades.db` | Trade journal location |
| `POLL_SECONDS` | `60` | Loop-mode check interval |
| `BACKTEST_DAYS` | `365` | History window for backtests |

### Trailing stops

With `TRAIL_PERCENT` set, each entry places a **server-side** trailing stop on
Alpaca (GTC), so the position stays protected while the bot sleeps between
passes — important on the daily timeframe, where the bot only wakes near the
close. Details: entries switch to whole-share sizing (stop orders can't hold
fractional shares); an RSI exit cancels the stop before closing; stop fills
that happen while the bot is away are journaled on the next pass. Note the
tuner optimizes *return*, where stops rarely win — their real job is capping
drawdown, so setting a wide one (e.g. 10-15%) manually as disaster insurance
is reasonable even if the backtest says it costs a little return.

## How it works

1. Each pass, the bot checks the market clock and skips if closed.
2. It fetches recent bars for each symbol, drops any still-forming bar, and
   computes RSI on the closes.
3. Buy signals then pass through whichever filters are enabled (volume,
   trend MA, higher-timeframe trend); vetoes are logged with the reason.
4. If a buy survives, it buys `NOTIONAL` dollars — or `NOTIONAL_PCT` of
   current equity — as a market order; a sell-cross closes the position.
   Repeat signals with an existing/empty position are ignored, so restarts
   are safe.

## Running unattended (cloud)

`deploy/` contains a systemd-based deployment kit: one service per bot
instance, each fully defined by an env file in `deploy/env/` (the committed
`.example` files encode the three current paper-trading candidates). Works on
any small Ubuntu VPS (~$6/month). See `deploy/README.md` for the runbook.

## Caveats

- This is a starting point, not financial advice. Backtest results ignore
  slippage and assume next-bar-close fills; live results will differ.
- The RSI-50 cross is a trend-following filter — it whipsaws in sideways
  markets, especially on intraday timeframes. Use `backtest` mode to compare
  the plain cross against the band (`RSI_BUY_LEVEL`/`RSI_SELL_LEVEL`) and
  trend-filter (`TREND_MA_PERIOD`) variants before picking settings.
- Keep `ALPACA_PAPER=true` until you've watched it run for a while.
