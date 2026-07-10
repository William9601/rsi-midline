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
    python rsi_midline_bot.py tune       # grid-search + walk-forward validate,
                                         # updating profiles.json if the winner
                                         # holds up out-of-sample (--dry-run to
                                         # only report)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import warnings

# macOS system Python links against LibreSSL; the warning is harmless noise.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
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


def apply_profile(timeframe: str) -> None:
    """Fill in tuned per-timeframe defaults from profiles.json.

    Settings already present in the environment (shell or .env) always win,
    so a profile only supplies the knobs you haven't set yourself.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        profile = json.load(f).get(timeframe)
    if not profile:
        log.warning("No profile for timeframe %s in profiles.json", timeframe)
        return
    applied = {k: v for k, v in profile.get("settings", {}).items()
               if k not in os.environ}
    for key, value in applied.items():
        os.environ[key] = str(value)
    log.info("Profile %s (%s, tuned %s): applied %s",
             timeframe, profile.get("status", "?"), profile.get("tuned", "?"),
             applied or "nothing (all set explicitly)")


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
    # Band variant: buy above RSI_BUY_LEVEL, sell below RSI_SELL_LEVEL.
    # Both default to the midline (plain crossover strategy).
    rsi_buy: float = field(default_factory=lambda: float(
        os.environ.get("RSI_BUY_LEVEL", os.environ.get("MIDLINE", "50"))))
    rsi_sell: float = field(default_factory=lambda: float(
        os.environ.get("RSI_SELL_LEVEL", os.environ.get("MIDLINE", "50"))))
    # Trend filter: only buy when price is above this moving average (0 = off).
    trend_ma: int = field(default_factory=lambda: int(os.environ.get("TREND_MA_PERIOD", "0")))
    # Volume filter: only buy when the signal bar's volume is at least this
    # multiple of the recent average (0 = off).
    vol_mult: float = field(default_factory=lambda: float(os.environ.get("VOLUME_MULT", "0")))
    vol_lookback: int = field(default_factory=lambda: int(os.environ.get("VOLUME_LOOKBACK", "20")))
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


def crossover_signal(
    rsi_series: pd.Series, buy_level: float, sell_level: float | None = None
) -> str | None:
    """Return 'buy'/'sell' if RSI crossed a level on the latest bar.

    With buy_level == sell_level this is the plain midline crossover; with
    e.g. 55/45 it becomes a band that ignores wobbles around the middle.
    """
    if sell_level is None:
        sell_level = buy_level
    if len(rsi_series) < 2 or rsi_series.iloc[-2:].isna().any():
        return None
    prev, curr = rsi_series.iloc[-2], rsi_series.iloc[-1]
    if prev <= buy_level < curr:
        return "buy"
    if prev >= sell_level > curr:
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

    def closed_bars(self, symbol: str, bars: pd.DataFrame) -> pd.DataFrame | None:
        """Bars for one symbol, dropping the still-forming last bar."""
        try:
            df = bars.xs(symbol, level="symbol")
        except KeyError:
            return None
        # For intraday timeframes the most recent bar may still be forming;
        # drop it so signals only fire on completed bars.
        if self.timeframe.unit != TimeFrameUnit.Day:
            bar_len = timedelta(minutes=self.timeframe.amount_value) \
                if self.timeframe.unit == TimeFrameUnit.Minute \
                else timedelta(hours=self.timeframe.amount_value)
            last_start = df.index[-1]
            if datetime.now(timezone.utc) < last_start + bar_len:
                df = df.iloc[:-1]
        return df

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
        # Enough history to warm up the RSI and trend MA regardless of timeframe.
        if self.timeframe.unit == TimeFrameUnit.Day:
            days = max(200, int((self.cfg.trend_ma + self.cfg.rsi_period) * 1.6) + 30)
        else:
            days = 60 if self.cfg.trend_ma else 30
        bars = self.fetch_bars(self.cfg.symbols, days=days)
        for symbol in self.cfg.symbols:
            symbol = symbol.strip().upper()
            df = self.closed_bars(symbol, bars)
            if df is None or len(df) < self.cfg.rsi_period + 2:
                log.warning("%s: not enough data, skipping", symbol)
                continue
            closes = df["close"]
            r = rsi(closes, self.cfg.rsi_period)
            signal = crossover_signal(r, self.cfg.rsi_buy, self.cfg.rsi_sell)
            if signal == "buy" and self.cfg.vol_mult:
                # Volume on the signal bar vs the average of the bars before it.
                avg_vol = df["volume"].shift(1).rolling(self.cfg.vol_lookback).mean().iloc[-1]
                rvol = df["volume"].iloc[-1] / avg_vol if avg_vol else float("nan")
                if not rvol >= self.cfg.vol_mult:
                    log.info("%s: buy signal vetoed by volume filter (rvol %.2f < %.2f)",
                             symbol, rvol, self.cfg.vol_mult)
                    signal = None
            if signal == "buy" and self.cfg.trend_ma:
                ma = closes.rolling(self.cfg.trend_ma).mean().iloc[-1]
                if pd.isna(ma) or closes.iloc[-1] <= ma:
                    log.info("%s: buy signal vetoed by trend filter (price %.2f <= MA%d %.2f)",
                             symbol, closes.iloc[-1], self.cfg.trend_ma, ma)
                    signal = None
            log.info("%s: RSI=%.1f signal=%s", symbol, r.iloc[-1], signal or "none")
            if signal == "buy":
                self.enter(symbol)
            elif signal == "sell":
                self.exit(symbol)

    def seconds_until_daily_eval(self) -> float:
        """Time until the daily-bar evaluation window (10 min before close)."""
        clock = self.trading.get_clock()
        target = clock.next_close - timedelta(minutes=10)
        return (target - clock.timestamp).total_seconds()

    def run_loop(self) -> None:
        log.info(
            "Starting loop: symbols=%s timeframe=%s RSI(%d) buy/sell=%.0f/%.0f paper=%s",
            self.cfg.symbols, self.cfg.timeframe, self.cfg.rsi_period,
            self.cfg.rsi_buy, self.cfg.rsi_sell, self.cfg.paper,
        )
        while True:
            try:
                if self.timeframe.unit == TimeFrameUnit.Day:
                    # Daily bars: evaluate once per day just before the close,
                    # when the bar is nearly complete. Sleeping through the
                    # rest of the day avoids trading a half-formed bar.
                    wait = self.seconds_until_daily_eval()
                    if wait > 0:
                        log.info("Daily timeframe: next evaluation in %.1f hours "
                                 "(10 min before market close)", wait / 3600)
                        time.sleep(wait)
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Pass failed; retrying next cycle")
            time.sleep(self.cfg.poll_seconds)

    # -- backtest -------------------------------------------------------------

    def simulate(
        self,
        df: pd.DataFrame,
        buy_level: float,
        sell_level: float,
        ma_period: int,
        vol_mult: float = 0,
        eval_from: int = 0,
        eval_to: int | None = None,
    ) -> tuple[int, float, float]:
        """Simulate one variant; returns (trades, win rate %, total return %).

        In the market from the bar after RSI crosses above buy_level until the
        bar after it crosses below sell_level (signals act on the next bar).
        eval_from/eval_to restrict *scoring* to a bar range while indicators
        still warm up on the full history (used for walk-forward splits).
        """
        closes = df["close"]
        r = rsi(closes, self.cfg.rsi_period)
        cross_up = (r > buy_level) & (r.shift(1) <= buy_level)
        cross_down = (r < sell_level) & (r.shift(1) >= sell_level)
        if ma_period:
            cross_up &= closes > closes.rolling(ma_period).mean()
        if vol_mult:
            avg_vol = df["volume"].shift(1).rolling(self.cfg.vol_lookback).mean()
            cross_up &= df["volume"] >= avg_vol * vol_mult
        state = pd.Series(np.nan, index=closes.index)
        state[cross_up] = 1.0
        state[cross_down] = 0.0
        in_market = state.ffill().fillna(0).shift(1, fill_value=0).astype(bool)
        bar_returns = closes.pct_change().fillna(0)
        if eval_from or eval_to is not None:
            in_market = in_market.iloc[eval_from:eval_to]
            bar_returns = bar_returns.iloc[eval_from:eval_to]
        total = ((1 + bar_returns * in_market).prod() - 1) * 100
        # Group consecutive in-market bars into trades for the win rate.
        grp = (in_market != in_market.shift()).cumsum()
        trade_returns = (1 + bar_returns[in_market]).groupby(grp[in_market]).prod() - 1
        trades = len(trade_returns)
        win_rate = float((trade_returns > 0).mean() * 100) if trades else 0.0
        return trades, win_rate, total

    def backtest(self) -> None:
        log.info(
            "Fetching %d days of %s bars for %s — this can take a minute...",
            self.cfg.backtest_days, self.cfg.timeframe, ",".join(self.cfg.symbols),
        )
        bars = self.fetch_bars(self.cfg.symbols, days=self.cfg.backtest_days)
        log.info("Fetched %d bars", len(bars))
        m = self.cfg.midline
        vm = self.cfg.vol_mult or 1.5
        variants = [
            (f"RSI {m:g} cross", m, m, 0, 0),
            ("Band 55/45", 55, 45, 0, 0),
            (f"RSI {m:g} + MA200", m, m, 200, 0),
            (f"RSI {m:g} + vol x{vm:g}", m, m, 0, vm),
            (f"Band + vol x{vm:g}", 55, 45, 0, vm),
            (f"Band + vol + MA200", 55, 45, 200, vm),
        ]
        print(f"\nBacktest: RSI({self.cfg.rsi_period}), timeframe={self.cfg.timeframe}, "
              f"last {self.cfg.backtest_days} days\n(MA200 = 200 bars of this timeframe; "
              f"vol = signal-bar volume vs {self.cfg.vol_lookback}-bar average)")
        for symbol in self.cfg.symbols:
            symbol = symbol.strip().upper()
            try:
                df = bars.xs(symbol, level="symbol")
            except KeyError:
                print(f"\n{symbol}: no data")
                continue
            closes = df["close"]
            hold = (closes.iloc[-1] / closes.iloc[0] - 1) * 100
            print(f"\n{symbol} — buy & hold: {hold:+.1f}%")
            header = f"  {'variant':<22}{'trades':>7}{'win %':>8}{'return':>9}"
            print(header)
            print("  " + "-" * (len(header) - 2))
            for name, buy, sell, ma, vol in variants:
                trades, win_rate, total = self.simulate(df, buy, sell, ma, vol)
                print(f"  {name:<22}{trades:>7}{win_rate:>7.1f}%{total:>+8.1f}%")
        print("\nNote: backtest ignores slippage, commissions, and fills at next-bar close.\n")

    # -- tuning ---------------------------------------------------------------

    GRID_BANDS = [(50, 50), (55, 45), (60, 40)]
    GRID_MAS = [0, 50, 200]
    GRID_VOLS = [0, 1.5, 2.0]

    def tune(self, write: bool = True) -> None:
        """Grid-search parameters with walk-forward validation.

        Optimizes on the first TRAIN_SPLIT of history, validates the winner on
        the held-out remainder, and only updates profiles.json if the winner
        also beats the current profile on the out-of-sample window.
        """
        cfg = self.cfg
        split_frac = float(os.environ.get("TRAIN_SPLIT", "0.7"))
        log.info("Fetching %d days of %s bars for %s — this can take a minute...",
                 cfg.backtest_days, cfg.timeframe, ",".join(cfg.symbols))
        bars = self.fetch_bars(cfg.symbols, days=cfg.backtest_days)
        dfs: dict[str, pd.DataFrame] = {}
        for symbol in cfg.symbols:
            symbol = symbol.strip().upper()
            try:
                dfs[symbol] = bars.xs(symbol, level="symbol")
            except KeyError:
                log.warning("%s: no data, excluded from tuning", symbol)
        if not dfs:
            sys.exit("No data for any symbol")
        splits = {s: int(len(df) * split_frac) for s, df in dfs.items()}

        def avg_scores(buy: float, sell: float, ma: int, vol: float) -> dict:
            train, test, train_trades, test_trades = [], [], 0, 0
            for sym, df in dfs.items():
                sp = splits[sym]
                n_tr, _, r_tr = self.simulate(df, buy, sell, ma, vol, eval_to=sp)
                n_te, _, r_te = self.simulate(df, buy, sell, ma, vol, eval_from=sp)
                train.append(r_tr); test.append(r_te)
                train_trades += n_tr; test_trades += n_te
            return {"buy": buy, "sell": sell, "ma": ma, "vol": vol,
                    "train": sum(train) / len(train), "test": sum(test) / len(test),
                    "train_trades": train_trades, "test_trades": test_trades}

        results = [avg_scores(buy, sell, ma, vol)
                   for buy, sell in self.GRID_BANDS
                   for ma in self.GRID_MAS
                   for vol in self.GRID_VOLS]
        # A combo that barely ever trades can "win" by doing nothing; require
        # some in-sample activity before considering it.
        active = [r for r in results if r["train_trades"] >= 2 * len(dfs)] or results
        # Winner is chosen on the training window ONLY (no peeking at test).
        active.sort(key=lambda r: r["train"], reverse=True)
        winner = active[0]

        hold_test = sum(
            (df["close"].iloc[-1] / df["close"].iloc[splits[s]] - 1) * 100
            for s, df in dfs.items()
        ) / len(dfs)

        print(f"\nTune: {cfg.timeframe}, {cfg.backtest_days} days, "
              f"{len(results)} combos, train {split_frac:.0%} / test {1 - split_frac:.0%} "
              f"(returns averaged across {', '.join(dfs)})\n")
        header = (f"  {'buy/sell':<10}{'MA':>5}{'vol':>6}"
                  f"{'train %':>10}{'test %':>9}{'test trades':>13}")
        print("  top 5 by TRAIN return — test column is out-of-sample:")
        print(header)
        print("  " + "-" * (len(header) - 2))
        for r in active[:5]:
            print(f"  {r['buy']:g}/{r['sell']:<7g}{r['ma']:>5}{r['vol']:>6g}"
                  f"{r['train']:>+9.1f}%{r['test']:>+8.1f}%{r['test_trades']:>13}")
        print(f"\n  buy & hold on test window: {hold_test:+.1f}%")

        # Gate: current profile settings evaluated on the same test window.
        current = self._current_profile_combo()
        cur = avg_scores(*current)
        print(f"  current profile ({current[0]:g}/{current[1]:g}, MA {current[2]}, "
              f"vol x{current[3]:g}) on test window: {cur['test']:+.1f}%")

        if winner["test"] < winner["train"] / 3:
            print("\n  WARNING: winner's out-of-sample return is far below its "
                  "training return — likely overfit to the training window.")
        if (winner["buy"], winner["sell"], winner["ma"], winner["vol"]) == current:
            print("\nVerdict: current profile is already the winner — nothing to update.\n")
            return
        if winner["test"] <= cur["test"]:
            print("\nVerdict: winner does NOT beat the current profile out-of-sample "
                  f"({winner['test']:+.1f}% vs {cur['test']:+.1f}%) — keeping profiles.json as is.\n")
            return
        print(f"\nVerdict: winner beats current profile out-of-sample "
              f"({winner['test']:+.1f}% vs {cur['test']:+.1f}%).")
        if write:
            self._write_profile(winner, hold_test, split_frac, list(dfs))
            print(f"Updated profiles.json [{cfg.timeframe}].\n")
        else:
            print("Dry run — profiles.json not modified.\n")

    def _profiles_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")

    def _current_profile_combo(self) -> tuple[float, float, int, float]:
        """Current profile's (buy, sell, ma, vol), falling back to plain midline."""
        try:
            with open(self._profiles_path()) as f:
                s = json.load(f)[self.cfg.timeframe]["settings"]
            return (float(s.get("RSI_BUY_LEVEL", self.cfg.midline)),
                    float(s.get("RSI_SELL_LEVEL", self.cfg.midline)),
                    int(s.get("TREND_MA_PERIOD", 0)),
                    float(s.get("VOLUME_MULT", 0)))
        except (OSError, KeyError, ValueError):
            return (self.cfg.midline, self.cfg.midline, 0, 0)

    def _write_profile(self, winner: dict, hold_test: float,
                       split_frac: float, symbols: list[str]) -> None:
        path = self._profiles_path()
        data = {}
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
        old = data.get(self.cfg.timeframe, {}).get("settings", {})
        settings = {
            "RSI_BUY_LEVEL": f"{winner['buy']:g}",
            "RSI_SELL_LEVEL": f"{winner['sell']:g}",
            "TREND_MA_PERIOD": str(winner["ma"]),
            "VOLUME_MULT": f"{winner['vol']:g}",
            "VOLUME_LOOKBACK": str(self.cfg.vol_lookback),
        }
        if "POLL_SECONDS" in old:  # tune doesn't search this; keep the old value
            settings["POLL_SECONDS"] = old["POLL_SECONDS"]
        data[self.cfg.timeframe] = {
            "tuned": datetime.now().strftime("%Y-%m-%d"),
            "status": "backtested (walk-forward)",
            "notes": (
                f"Auto-tuned on {','.join(symbols)} over {self.cfg.backtest_days}d of "
                f"{self.cfg.timeframe} bars, {split_frac:.0%}/{1 - split_frac:.0%} "
                f"walk-forward split. Train avg {winner['train']:+.1f}%, "
                f"out-of-sample test avg {winner['test']:+.1f}% vs buy&hold "
                f"{hold_test:+.1f}% on the test window ({winner['test_trades']} test trades). "
                f"Grid: bands {self.GRID_BANDS}, MA {self.GRID_MAS}, vol {self.GRID_VOLS}."
            ),
            "settings": settings,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")


# ---------------------------------------------------------------------------

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode not in ("run", "loop", "backtest", "tune"):
        print(__doc__)
        sys.exit(1)
    load_dotenv()
    if not os.environ.get("ALPACA_API_KEY") or not os.environ.get("ALPACA_SECRET_KEY"):
        sys.exit(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.\n"
            "Add them to rsi-midline-bot/.env (see .env.example) or export them "
            "in your shell."
        )
    apply_profile(os.environ.get("TIMEFRAME", "15Min"))
    cfg = Config()
    bot = RsiMidlineBot(cfg)
    if not cfg.paper and mode in ("run", "loop"):
        log.warning("LIVE TRADING MODE — real money at risk")
    if mode == "run":
        bot.run_once()
    elif mode == "loop":
        bot.run_loop()
    elif mode == "tune":
        bot.tune(write="--dry-run" not in sys.argv[2:])
    else:
        bot.backtest()


if __name__ == "__main__":
    main()
