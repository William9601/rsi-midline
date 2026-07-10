# RSI Midline Trading Bot (Alpaca)

A long-only Python trading bot for Alpaca implementing the **RSI midline strategy**:

- **Buy** when RSI crosses **above 50** (momentum turning bullish)
- **Sell / close** when RSI crosses **below 50** (momentum turning bearish)

RSI uses Wilder's smoothing, and signals only fire on completed bars so a
still-forming intraday bar can't trigger a trade.

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
```

## Per-timeframe profiles

`profiles.json` records the tuned settings for each timeframe **and the
backtest evidence behind them**. On startup the bot applies the profile
matching your `TIMEFRAME`, so switching timeframes automatically switches the
band levels, volume filter, and trend filter to what tested best there.

Precedence: anything set explicitly in `.env` or the shell **overrides** the
profile — profiles only fill in settings you haven't chosen yourself. The
startup log shows exactly which values came from the profile.

Current findings (tuned 2026-07-10 on SPY/AAPL/MSFT):

- **1Day** — plain RSI-50 cross, all filters off. Filters (band, MA200,
  volume) all *reduced* returns on daily bars; the volume filter especially
  is far too strict there.
- **15Min** — band 55/45 + volume ×1.5 + MA200. Volume confirmation was the
  single biggest intraday improvement.
- **1Hour / 5Min / 1Min** — marked `not backtested` in the file; guesses
  inherited from the 15Min findings. Run `backtest` on them before trusting.

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
| `NOTIONAL` | `1000` | Dollars per new position |
| `POLL_SECONDS` | `60` | Loop-mode check interval |
| `BACKTEST_DAYS` | `365` | History window for backtests |

## How it works

1. Each pass, the bot checks the market clock and skips if closed.
2. It fetches recent bars for each symbol, drops any still-forming bar, and
   computes RSI on the closes.
3. If RSI crossed above the midline on the latest completed bar it buys
   `NOTIONAL` dollars (market order); if it crossed below, it closes the
   position. Repeat signals with an existing/empty position are ignored, so
   restarts are safe.

## Caveats

- This is a starting point, not financial advice. Backtest results ignore
  slippage and assume next-bar-close fills; live results will differ.
- The RSI-50 cross is a trend-following filter — it whipsaws in sideways
  markets, especially on intraday timeframes. Use `backtest` mode to compare
  the plain cross against the band (`RSI_BUY_LEVEL`/`RSI_SELL_LEVEL`) and
  trend-filter (`TREND_MA_PERIOD`) variants before picking settings.
- Keep `ALPACA_PAPER=true` until you've watched it run for a while.
