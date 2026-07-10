"""RSI midline strategy trading bot for Alpaca.

Strategy:
    - Compute RSI (Wilder smoothing) on closing prices.
    - BUY when RSI crosses above the midline (50) — momentum turning bullish.
    - SELL (close position) when RSI crosses below the midline.

Long-only. Signals are evaluated on completed bars only.

Usage:
    python rsi_midline_bot.py run        # evaluate signals once and trade
    python rsi_midline_bot.py loop       # run continuously on an interval
    python rsi_midline_bot.py backtest   # backtest the strategy on history
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("rsi-midline")


def load_dotenv() -> None:
    """Load KEY=VALUE pairs from a .env file next to this script.

    Values already present in the environment take precedence.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # Drop inline comments and surrounding quotes.
            value = value.split("#", 1)[0].strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TIMEFRAMES = {
    "1Min": TimeFrame(1, TimeFrameUnit.Minute),
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
    "1Day": TimeFrame(1, TimeFrameUnit.Day),
}


@dataclass
class Config:
    api_key: str = field(default_factory=lambda: os.environ["ALPACA_API_KEY"])
    secret_key: str = field(default_factory=lambda: os.environ["ALPACA_SECRET_KEY"])
    paper: bool = field(
        default_factory=lambda: os.environ.get("ALPACA_PAPER", "true").lower() != "false"
    )
    symbols: list[str] = field(
        default_factory=lambda: os.environ.get("SYMBOLS", "SPY,AAPL,MSFT").split(",")
    )
    timeframe: str = field(default_factory=lambda: os.environ.get("TIMEFRAME", "15Min"))
    rsi_period: int = field(default_factory=lambda: int(os.environ.get("RSI_PERIOD", "14")))
    midline: float = field(default_factory=lambda: float(os.environ.get("MIDLINE", "50")))
    # Dollar amount per new position.
    notional: float = field(default_factory=lambda: float(os.environ.get("NOTIONAL", "1000")))
    # Seconds between checks in loop mode.
    poll_seconds: int = field(default_factory=lambda: int(os.environ.get("POLL_SECONDS", "60")))
    # Days of history to backtest.
    backtest_days: int = field(default_factory=lambda: int(os.environ.get("BACKTEST_DAYS", "365")))


# ---------------------------------------------------------------------------
# Indicator
# ---------------------------------------------------------------------------

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss  # avg_loss of 0 gives inf, which maps to RSI 100 below
    return 100 - (100 / (1 + rs))


def crossover_signal(rsi_series: pd.Series, midline: float) -> str | None:
    """Return 'buy'/'sell' if RSI crossed the midline on the latest bar."""
    if len(rsi_series) < 2 or rsi_series.iloc[-2:].isna().any():
        return None
    prev, curr = rsi_series.iloc[-2], rsi_series.iloc[-1]
    if prev <= midline < curr:
        return "buy"
    if prev >= midline > curr:
        return "sell"
    return None


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class RsiMidlineBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.trading = TradingClient(cfg.api_key, cfg.secret_key, paper=cfg.paper)
        self.data = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
        if cfg.timeframe not in TIMEFRAMES:
            raise ValueError(f"TIMEFRAME must be one of {list(TIMEFRAMES)}")
        self.timeframe = TIMEFRAMES[cfg.timeframe]

    # -- data ---------------------------------------------------------------

    def fetch_bars(self, symbols: list[str], days: int) -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=self.timeframe,
            start=datetime.now(timezone.utc) - timedelta(days=days),
        )
        return self.data.get_stock_bars(req).df

    def closed_bars(self, symbol: str, bars: pd.DataFrame) -> pd.Series | None:
        """Closing prices for one symbol, dropping the still-forming last bar."""
        try:
            df = bars.xs(symbol, level="symbol")
        except KeyError:
            return None
        closes = df["close"]
        # For intraday timeframes the most recent bar may still be forming;
        # drop it so signals only fire on completed bars.
        if self.timeframe.unit != TimeFrameUnit.Day:
            bar_len = timedelta(minutes=self.timeframe.amount_value) \
                if self.timeframe.unit == TimeFrameUnit.Minute \
                else timedelta(hours=self.timeframe.amount_value)
            last_start = closes.index[-1]
            if datetime.now(timezone.utc) < last_start + bar_len:
                closes = closes.iloc[:-1]
        return closes

    # -- trading ------------------------------------------------------------

    def position_qty(self, symbol: str) -> float:
        for pos in self.trading.get_all_positions():
            if pos.symbol == symbol:
                return float(pos.qty)
        return 0.0

    def enter(self, symbol: str) -> None:
        if self.position_qty(symbol) > 0:
            log.info("%s: buy signal but already long, skipping", symbol)
            return
        order = self.trading.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                notional=self.cfg.notional,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        )
        log.info("%s: BUY $%.2f submitted (order %s)", symbol, self.cfg.notional, order.id)

    def exit(self, symbol: str) -> None:
        if self.position_qty(symbol) <= 0:
            log.info("%s: sell signal but no position, skipping", symbol)
            return
        self.trading.close_position(symbol)
        log.info("%s: SELL — position closed", symbol)

    # -- main passes ----------------------------------------------------------

    def run_once(self) -> None:
        clock = self.trading.get_clock()
        if not clock.is_open:
            log.info("Market closed (next open %s), skipping pass", clock.next_open)
            return
        # Enough history to warm up the RSI regardless of timeframe.
        bars = self.fetch_bars(self.cfg.symbols, days=30 if self.timeframe.unit != TimeFrameUnit.Day else 200)
        for symbol in self.cfg.symbols:
            symbol = symbol.strip().upper()
            closes = self.closed_bars(symbol, bars)
            if closes is None or len(closes) < self.cfg.rsi_period + 2:
                log.warning("%s: not enough data, skipping", symbol)
                continue
            r = rsi(closes, self.cfg.rsi_period)
            signal = crossover_signal(r, self.cfg.midline)
            log.info("%s: RSI=%.1f signal=%s", symbol, r.iloc[-1], signal or "none")
            if signal == "buy":
                self.enter(symbol)
            elif signal == "sell":
                self.exit(symbol)

    def run_loop(self) -> None:
        log.info(
            "Starting loop: symbols=%s timeframe=%s RSI(%d) midline=%.0f paper=%s",
            self.cfg.symbols, self.cfg.timeframe, self.cfg.rsi_period,
            self.cfg.midline, self.cfg.paper,
        )
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Pass failed; retrying next cycle")
            time.sleep(self.cfg.poll_seconds)

    # -- backtest -------------------------------------------------------------

    def backtest(self) -> None:
        bars = self.fetch_bars(self.cfg.symbols, days=self.cfg.backtest_days)
        print(f"\nBacktest: RSI({self.cfg.rsi_period}) midline={self.cfg.midline} "
              f"timeframe={self.cfg.timeframe}, last {self.cfg.backtest_days} days\n")
        header = f"{'symbol':<8}{'trades':>7}{'win %':>8}{'strategy %':>12}{'buy&hold %':>12}"
        print(header)
        print("-" * len(header))
        for symbol in self.cfg.symbols:
            symbol = symbol.strip().upper()
            try:
                closes = bars.xs(symbol, level="symbol")["close"]
            except KeyError:
                print(f"{symbol:<8}  no data")
                continue
            r = rsi(closes, self.cfg.rsi_period)
            above = r > self.cfg.midline
            # In the market from the bar after a cross above until the bar
            # after a cross below (signals act on the next bar's move).
            in_market = above.shift(1, fill_value=False)
            bar_returns = closes.pct_change().fillna(0)
            strat_curve = (1 + bar_returns * in_market).cumprod()
            hold_curve = (1 + bar_returns).cumprod()

            entries = above & ~above.shift(1, fill_value=False)
            trades = int(entries.sum())
            # Per-trade returns for win rate
            wins = 0
            entry_price = None
            for i in range(1, len(closes)):
                if above.iloc[i - 1] and entry_price is None:
                    entry_price = closes.iloc[i]
                elif not above.iloc[i - 1] and entry_price is not None:
                    if closes.iloc[i] > entry_price:
                        wins += 1
                    entry_price = None
            closed = trades - (1 if entry_price is not None else 0)
            win_rate = (wins / closed * 100) if closed else 0.0
            print(f"{symbol:<8}{trades:>7}{win_rate:>7.1f}%"
                  f"{(strat_curve.iloc[-1] - 1) * 100:>11.1f}%"
                  f"{(hold_curve.iloc[-1] - 1) * 100:>11.1f}%")
        print("\nNote: backtest ignores slippage, commissions, and fills at next-bar close.\n")


# ---------------------------------------------------------------------------

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode not in ("run", "loop", "backtest"):
        print(__doc__)
        sys.exit(1)
    load_dotenv()
    if not os.environ.get("ALPACA_API_KEY") or not os.environ.get("ALPACA_SECRET_KEY"):
        sys.exit(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.\n"
            "Add them to rsi-midline-bot/.env (see .env.example) or export them "
            "in your shell."
        )
    cfg = Config()
    bot = RsiMidlineBot(cfg)
    if not cfg.paper and mode != "backtest":
        log.warning("LIVE TRADING MODE — real money at risk")
    if mode == "run":
        bot.run_once()
    elif mode == "loop":
        bot.run_loop()
    else:
        bot.backtest()


if __name__ == "__main__":
    main()
