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
    python rsi_midline_bot.py portfolio  # backtest all symbols on ONE shared
                                         # account with live sizing (equity
                                         # curve, max drawdown, exposure)
    python rsi_midline_bot.py tune       # grid-search + walk-forward validate,
                                         # updating profiles.json if the winner
                                         # holds up out-of-sample (--dry-run to
                                         # only report)
    python rsi_midline_bot.py trades     # show the trade journal (SQLite)
    python rsi_midline_bot.py pnl [db..] # round-trip P&L / losing-trade stats
                                         # from the journal(s)
    python rsi_midline_bot.py replay [since]  # re-simulate the live period
                                         # with the resolved config and diff
                                         # the sim's trades vs the journal
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import sys
import time
import warnings

# macOS system Python links against LibreSSL; the warning is harmless noise.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (OrderSide, OrderStatus, OrderType,
                                  QueryOrderStatus, TimeInForce)
from alpaca.trading.requests import (GetOrdersRequest, MarketOrderRequest,
                                     TrailingStopOrderRequest)

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

# Higher timeframes the HTF trend filter can resample the trading bars into.
HTF_RULES = {"1Hour": "1h", "4Hour": "4h", "1Day": "1D"}


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
    # Moving-average type for the trend filter: sma | ema | hma.
    ma_type: str = field(default_factory=lambda: os.environ.get("MA_TYPE", "sma"))
    # Volume filter: only buy when the signal bar's volume is at least this
    # multiple of the recent average (0 = off).
    vol_mult: float = field(default_factory=lambda: float(os.environ.get("VOLUME_MULT", "0")))
    vol_lookback: int = field(default_factory=lambda: int(os.environ.get("VOLUME_LOOKBACK", "20")))
    # Higher-timeframe trend confirmation: only buy when the close is above
    # this MA on HTF_TIMEFRAME bars resampled from the trading bars (0 = off).
    htf_ma: int = field(default_factory=lambda: int(os.environ.get("HTF_MA_PERIOD", "0")))
    htf_timeframe: str = field(default_factory=lambda: os.environ.get("HTF_TIMEFRAME", "1Hour"))
    # Multi-timeframe RSI confirmation: only buy when RSI(RSI_PERIOD) on
    # HTF_TIMEFRAME bars is above this level (0 = off). Same no-look-ahead
    # resampling as the HTF MA filter; intraday trading timeframes only.
    htf_rsi_level: float = field(
        default_factory=lambda: float(os.environ.get("HTF_RSI_LEVEL", "0")))
    # Trailing stop: place a server-side GTC trailing stop this % below the
    # high-water mark after each entry (0 = off). Whole-share sizing applies.
    trail_percent: float = field(default_factory=lambda: float(os.environ.get("TRAIL_PERCENT", "0")))
    # Dollar amount per new position.
    notional: float = field(default_factory=lambda: float(os.environ.get("NOTIONAL", "1000")))
    # Position size as a percent of current account equity (0 = off, use the
    # flat NOTIONAL). Capped by available cash so entries never use margin.
    notional_pct: float = field(default_factory=lambda: float(os.environ.get("NOTIONAL_PCT", "0")))
    # Seconds between checks in loop mode.
    poll_seconds: int = field(default_factory=lambda: int(os.environ.get("POLL_SECONDS", "60")))
    # Days of history to backtest.
    backtest_days: int = field(default_factory=lambda: int(os.environ.get("BACKTEST_DAYS", "365")))
    # Simulator friction, in basis points of trade value charged on each side
    # (entry and exit). 0 = frictionless, which reproduces legacy backtest
    # numbers exactly. Live trading ignores this (fills are what they are).
    cost_bps: float = field(default_factory=lambda: float(os.environ.get("COST_BPS", "0")))
    # Alpaca bar adjustment: raw | split | dividend | all. 'all' removes
    # split/dividend gaps that would otherwise feed the RSI as fake moves.
    # Use 'raw' to reproduce backtests recorded before this setting existed.
    bar_adjustment: str = field(
        default_factory=lambda: os.environ.get("BAR_ADJUSTMENT", "all"))
    # Alpaca data feed: iex | sip | '' (empty = the account subscription's
    # default — SIP full-market data on Algo Trader Plus, IEX otherwise).
    # Every recorded result is SIP; 'iex' is for feed-difference experiments.
    data_feed: str = field(
        default_factory=lambda: os.environ.get("DATA_FEED", ""))
    # Simulators only: restrict entries/exits to bars the live bot could act
    # on (bar close inside regular NY trading hours). IEX bars span 4am-8pm
    # ET, but live `run_once` skips closed markets and signals are
    # edge-triggered on the latest bar, so an off-hours cross never fires
    # live — it is missed, not delayed. "false" reproduces intraday
    # backtests recorded before 2026-07-13, which traded all hours.
    rth_only: bool = field(
        default_factory=lambda: os.environ.get("RTH_ONLY", "true").lower() != "false")
    # Starting cash for the `portfolio` simulator (mirrors the planned live
    # account; not used by live trading, which reads real account equity).
    start_equity: float = field(
        default_factory=lambda: float(os.environ.get("START_EQUITY", "2000")))


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


def wma(s: pd.Series, period: int) -> pd.Series:
    """Linearly weighted moving average (most recent bar weighted highest)."""
    w = np.arange(1, period + 1, dtype=float)
    return s.rolling(period).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def ma_series(close: pd.Series, period: int, kind: str = "sma") -> pd.Series:
    """Moving average of the requested type: sma | ema | hma.

    HMA = WMA(2*WMA(n/2) - WMA(n), sqrt(n)) — Hull's reduced-lag average.
    """
    if kind == "sma":
        return close.rolling(period).mean()
    if kind == "ema":
        return close.ewm(span=period, min_periods=period, adjust=False).mean()
    if kind == "hma":
        raw = 2 * wma(close, max(period // 2, 1)) - wma(close, period)
        return wma(raw, max(int(np.sqrt(period)), 1))
    raise ValueError(f"MA_TYPE must be sma, ema, or hma (got {kind!r})")


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


def _htf_align(ok: pd.Series, index: pd.DatetimeIndex,
               bar_len: timedelta) -> pd.Series:
    """Align a boolean HTF-bar series back onto the trading-bar index so each
    trading bar only sees HTF bars that had fully closed by the time the
    trading bar itself closed — no look-ahead."""
    # A bar indexed at t closes at t + bar_len; HTF bars are indexed at their
    # end time, so ffill picks the latest HTF bar that ended by then. Bars
    # before the first HTF close reindex to NaN, which != 1.0 -> False.
    aligned = ok.astype(float).reindex(index + bar_len, method="ffill")
    return pd.Series(aligned.to_numpy() == 1.0, index=index)


def _htf_closes(df: pd.DataFrame, rule: str) -> pd.Series:
    return df["close"].resample(rule, label="right", closed="left").last().dropna()


def htf_trend_ok(df: pd.DataFrame, rule: str, ma_period: int,
                 bar_len: timedelta) -> pd.Series:
    """True where the higher-timeframe trend is up (HTF close above its MA).

    Resamples the trading-timeframe bars up to the higher timeframe. NaN
    warmup (fewer than ma_period HTF bars) counts as trend-down, like the
    trend MA.
    """
    htf_close = _htf_closes(df, rule)
    ok = htf_close > htf_close.rolling(ma_period).mean()
    return _htf_align(ok, df.index, bar_len)


def htf_rsi_ok(df: pd.DataFrame, rule: str, level: float, period: int,
               bar_len: timedelta) -> pd.Series:
    """True where RSI on the higher timeframe is above `level`.

    Same resample/align (no look-ahead) as htf_trend_ok; RSI warmup counts
    as not-ok."""
    htf_close = _htf_closes(df, rule)
    ok = rsi(htf_close, period) > level
    return _htf_align(ok, df.index, bar_len)


# ---------------------------------------------------------------------------
# Trade journal
# ---------------------------------------------------------------------------

class TradeLog:
    """SQLite journal of every order, with the signal context that caused it."""

    COLUMNS = ("ts", "symbol", "side", "price", "notional", "qty", "order_id",
               "rsi", "rvol", "timeframe", "rsi_buy", "rsi_sell", "trend_ma",
               "vol_mult", "htf_ma", "ma_type", "htf_rsi", "paper")

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,          -- UTC ISO timestamp
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,        -- buy / sell
                price REAL,                -- last close when the signal fired
                notional REAL,             -- dollars submitted (buys)
                qty REAL,                  -- shares closed (sells)
                order_id TEXT,
                rsi REAL,                  -- RSI on the signal bar
                rvol REAL,                 -- signal-bar volume / recent average
                timeframe TEXT,
                rsi_buy REAL, rsi_sell REAL, trend_ma INTEGER, vol_mult REAL,
                htf_ma INTEGER,            -- higher-timeframe trend MA (0 = off)
                ma_type TEXT,              -- trend-MA type: sma / ema / hma
                htf_rsi REAL,              -- higher-timeframe RSI level (0 = off)
                paper INTEGER
            )""")
        # Migrate journals created before newer filter columns existed.
        for ddl in ("ALTER TABLE trades ADD COLUMN htf_ma INTEGER",
                    "ALTER TABLE trades ADD COLUMN ma_type TEXT",
                    "ALTER TABLE trades ADD COLUMN htf_rsi REAL"):
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    def record(self, **f) -> None:
        f["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            f"INSERT INTO trades ({','.join(self.COLUMNS)}) "
            f"VALUES ({','.join('?' * len(self.COLUMNS))})",
            tuple(f.get(c) for c in self.COLUMNS),
        )
        self.conn.commit()

    def has_order(self, order_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM trades WHERE order_id = ?", (order_id,)
        ).fetchone() is not None

    def recent(self, limit: int = 20) -> list[tuple]:
        return self.conn.execute(
            f"SELECT {','.join(self.COLUMNS)} FROM trades "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()[::-1]


def show_pnl(db_paths: list[str] | None = None) -> None:
    """Round-trip P&L from the journal: pair each sell with its open buy.

    Long-only with one position per symbol, so pairing is trivial. Prices
    are the journaled signal-bar closes (trailing-stop rows use the real
    fill), so returns exclude slippage/fees — treat as close-to-close.
    Accepts multiple DB paths to report each deployed instance in turn;
    opens them read-only so it never migrates someone else's journal.
    Needs no API keys — main() dispatches it before the credential check.
    """
    default = os.environ.get("TRADES_DB") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "trades.db")
    for path in db_paths or [default]:
        print(f"\n=== {path} ===")
        if not os.path.exists(path):
            print("  no such file")
            continue
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT ts, symbol, side, price FROM trades ORDER BY id"
        ).fetchall()
        conn.close()
        open_pos: dict[str, tuple[str, float]] = {}
        trips: list[tuple] = []
        orphans = 0
        for ts, sym, side, price in rows:
            if price is None:
                orphans += 1
            elif side == "buy":
                # Double buys shouldn't happen (enter() skips when long);
                # keep the first entry if one slips through.
                open_pos.setdefault(sym, (ts, price))
            elif sym in open_pos:
                ets, ep = open_pos.pop(sym)
                trips.append((sym, ets, ts, ep, price, (price / ep - 1) * 100))
            else:
                orphans += 1  # sell with no journaled buy
        if not trips and not open_pos:
            print(f"  no round trips yet ({len(rows)} journal rows)")
            continue
        if trips:
            header = (f"  {'symbol':<7}{'entry (UTC)':<18}{'exit (UTC)':<18}"
                      f"{'in':>9}{'out':>9}{'return':>9}{'held':>8}")
            print(header)
            print("  " + "-" * (len(header) - 2))
        for sym, ets, xts, ep, xp, ret in trips:
            held = (datetime.fromisoformat(xts) - datetime.fromisoformat(ets))
            held_s = (f"{held.days}d" if held.days
                      else f"{held.seconds // 3600}h{held.seconds % 3600 // 60:02d}")
            print(f"  {sym:<7}{ets[:16]:<18}{xts[:16]:<18}"
                  f"{ep:>9.2f}{xp:>9.2f}{ret:>+8.2f}%{held_s:>8}")
        for sym, (ets, ep) in sorted(open_pos.items()):
            print(f"  {sym:<7}{ets[:16]:<18}{'(still open)':<18}{ep:>9.2f}")
        if trips:
            rets = [t[5] for t in trips]
            losses = [r for r in rets if r <= 0]
            wins = [r for r in rets if r > 0]
            cum = float(np.prod([1 + r / 100 for r in rets]) - 1) * 100
            print(f"\n  {len(trips)} closed: {len(wins)} won, {len(losses)} "
                  f"lost ({len(losses) / len(trips) * 100:.0f}% losing)"
                  + (f" | avg win {sum(wins) / len(wins):+.2f}%" if wins else "")
                  + (f" | avg loss {sum(losses) / len(losses):+.2f}%" if losses else "")
                  + f" | expectancy {sum(rets) / len(rets):+.2f}%/trade"
                  f" | compounded {cum:+.2f}%")
        if orphans:
            print(f"  ({orphans} unmatched rows skipped — e.g. sells of "
                  f"positions opened before journaling)")


def _utc_ts(s: str) -> pd.Timestamp:
    """Parse an ISO timestamp (journal `ts`, REPLAY_SINCE) as tz-aware UTC."""
    ts = pd.Timestamp(s.replace("Z", "+00:00"))
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")


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
        if (cfg.htf_ma or cfg.htf_rsi_level) and cfg.htf_timeframe not in HTF_RULES:
            raise ValueError(f"HTF_TIMEFRAME must be one of {list(HTF_RULES)}")
        if cfg.ma_type not in ("sma", "ema", "hma"):
            raise ValueError(f"MA_TYPE must be sma, ema, or hma (got {cfg.ma_type!r})")
        if cfg.bar_adjustment not in [a.value for a in Adjustment]:
            raise ValueError(
                f"BAR_ADJUSTMENT must be one of {[a.value for a in Adjustment]}")
        if cfg.data_feed and cfg.data_feed not in [f.value for f in DataFeed]:
            raise ValueError(
                f"DATA_FEED must be one of {[f.value for f in DataFeed]} or unset")
        self.timeframe = TIMEFRAMES[cfg.timeframe]
        db_path = os.environ.get("TRADES_DB") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "trades.db")
        self.journal = TradeLog(db_path)

    # -- data ---------------------------------------------------------------

    def fetch_bars(self, symbols: list[str], days: int) -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=self.timeframe,
            start=datetime.now(timezone.utc) - timedelta(days=days),
            adjustment=Adjustment(self.cfg.bar_adjustment),
            # None lets the server pick the subscription default (SIP on
            # Algo Trader Plus); DATA_FEED=iex reproduces free-tier data.
            feed=DataFeed(self.cfg.data_feed) if self.cfg.data_feed else None,
        )
        return self.data.get_stock_bars(req).df

    def bar_len(self) -> timedelta:
        """Duration of one bar of the trading timeframe."""
        if self.timeframe.unit == TimeFrameUnit.Minute:
            return timedelta(minutes=self.timeframe.amount_value)
        if self.timeframe.unit == TimeFrameUnit.Hour:
            return timedelta(hours=self.timeframe.amount_value)
        return timedelta(days=self.timeframe.amount_value)

    def closed_bars(self, symbol: str, bars: pd.DataFrame) -> pd.DataFrame | None:
        """Bars for one symbol, dropping the still-forming last bar."""
        try:
            df = bars.xs(symbol, level="symbol")
        except KeyError:
            return None
        # For intraday timeframes the most recent bar may still be forming;
        # drop it so signals only fire on completed bars.
        if self.timeframe.unit != TimeFrameUnit.Day:
            last_start = df.index[-1]
            if datetime.now(timezone.utc) < last_start + self.bar_len():
                df = df.iloc[:-1]
        return df

    # -- trading ------------------------------------------------------------

    def position_qty(self, symbol: str) -> float:
        for pos in self.trading.get_all_positions():
            if pos.symbol == symbol:
                return float(pos.qty)
        return 0.0

    def _log_trade(self, symbol: str, side: str, ctx: dict, **extra) -> None:
        self.journal.record(
            symbol=symbol, side=side, price=ctx.get("price"),
            rsi=ctx.get("rsi"), rvol=ctx.get("rvol"),
            timeframe=self.cfg.timeframe, rsi_buy=self.cfg.rsi_buy,
            rsi_sell=self.cfg.rsi_sell, trend_ma=self.cfg.trend_ma,
            vol_mult=self.cfg.vol_mult, htf_ma=self.cfg.htf_ma,
            ma_type=self.cfg.ma_type, htf_rsi=self.cfg.htf_rsi_level,
            paper=int(self.cfg.paper), **extra,
        )

    def position_notional(self) -> float:
        """Dollars for a new position.

        NOTIONAL_PCT sizes off current equity so the account compounds like
        the backtest assumes, capped by available cash so a burst of signals
        can't lean on margin. Flat NOTIONAL keeps the legacy behavior.
        """
        if not self.cfg.notional_pct:
            return self.cfg.notional
        acct = self.trading.get_account()
        notional = float(acct.equity) * self.cfg.notional_pct / 100
        cash = float(acct.cash)
        if notional > cash:
            log.info("sizing: %.0f%% of equity $%.2f capped by cash to $%.2f",
                     self.cfg.notional_pct, float(acct.equity), cash)
            notional = cash
        return round(notional, 2)

    def enter(self, symbol: str, ctx: dict) -> None:
        if self.position_qty(symbol) > 0:
            log.info("%s: buy signal but already long, skipping", symbol)
            return
        notional = self.position_notional()
        if notional < 1:  # Alpaca's minimum for notional orders
            log.warning("%s: buy skipped — position size $%.2f below $1 minimum "
                        "(no free cash?)", symbol, notional)
            return
        if self.cfg.trail_percent:
            # Stop orders need whole shares, so size the entry in shares too.
            qty = int(notional // ctx["price"])
            if qty < 1:
                log.warning("%s: $%.0f buys < 1 share at %.2f, skipping",
                            symbol, notional, ctx["price"])
                return
            request = MarketOrderRequest(symbol=symbol, qty=qty,
                                         side=OrderSide.BUY,
                                         time_in_force=TimeInForce.DAY)
        else:
            qty = None
            request = MarketOrderRequest(symbol=symbol, notional=notional,
                                         side=OrderSide.BUY,
                                         time_in_force=TimeInForce.DAY)
        order = self.trading.submit_order(request)
        self._log_trade(symbol, "buy", ctx, notional=notional,
                        qty=qty, order_id=str(order.id))
        log.info("%s: BUY $%.2f submitted (order %s)", symbol, notional, order.id)
        if self.cfg.trail_percent:
            self._place_trailing_stop(symbol, order.id, qty)

    def _place_trailing_stop(self, symbol: str, entry_order_id, qty: int) -> None:
        """Attach a server-side trailing stop once the entry fills.

        GTC on Alpaca's servers, so the position stays protected while the
        bot sleeps between passes.
        """
        for _ in range(30):
            entry = self.trading.get_order_by_id(entry_order_id)
            if entry.status == OrderStatus.FILLED:
                break
            if entry.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED,
                                OrderStatus.REJECTED):
                log.warning("%s: entry order %s ended %s — no stop placed",
                            symbol, entry_order_id, entry.status)
                return
            time.sleep(1)
        else:
            log.warning("%s: entry not filled after 30s — no stop placed; "
                        "position is UNPROTECTED until the next signal", symbol)
            return
        stop = self.trading.submit_order(TrailingStopOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL,
            trail_percent=self.cfg.trail_percent, time_in_force=TimeInForce.GTC,
        ))
        log.info("%s: trailing stop %.1f%% placed for %d shares (order %s)",
                 symbol, self.cfg.trail_percent, qty, stop.id)

    def _cancel_open_orders(self, symbol: str) -> None:
        open_orders = self.trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
        for order in open_orders:
            self.trading.cancel_order_by_id(order.id)
            log.info("%s: canceled open %s order %s", symbol, order.type, order.id)

    def exit(self, symbol: str, ctx: dict) -> None:
        qty = self.position_qty(symbol)
        if qty <= 0:
            log.info("%s: sell signal but no position, skipping", symbol)
            return
        # Release shares held by the trailing stop before closing.
        self._cancel_open_orders(symbol)
        order = self.trading.close_position(symbol)
        self._log_trade(symbol, "sell", ctx, qty=qty, order_id=str(order.id))
        log.info("%s: SELL — position closed (%.4f shares)", symbol, qty)

    def reconcile_stop_fills(self) -> None:
        """Journal trailing-stop sells that filled while the bot was asleep."""
        symbols = {s.strip().upper() for s in self.cfg.symbols}
        closed = self.trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=100))
        for order in closed:
            if (order.type == OrderType.TRAILING_STOP
                    and order.status == OrderStatus.FILLED
                    and order.symbol in symbols
                    and not self.journal.has_order(str(order.id))):
                self._log_trade(order.symbol, "sell", {
                    "price": float(order.filled_avg_price)}, qty=float(order.filled_qty),
                    order_id=str(order.id))
                log.info("%s: trailing stop FIRED at %.2f while bot was away "
                         "(%.0f shares) — journaled", order.symbol,
                         float(order.filled_avg_price), float(order.filled_qty))

    # -- main passes ----------------------------------------------------------

    def _live_fetch_days(self) -> int:
        """History depth a live pass fetches: enough to warm up the RSI, the
        trend MA and the HTF filters. `replay` adds its window on top of this
        so the re-simulation warms up on the same depth live had."""
        if self.timeframe.unit == TimeFrameUnit.Day:
            return max(200, int((self.cfg.trend_ma + self.cfg.rsi_period) * 1.6) + 30)
        days = 60 if self.cfg.trend_ma else 30
        if self.cfg.htf_ma or self.cfg.htf_rsi_level:
            # The HTF filters need enough *higher-timeframe* bars or their
            # MA/RSI is all-NaN and every buy gets vetoed (a 1Day MA50
            # needs ~50 trading days — far more than the base 30 calendar).
            # Wilder RSI needs ~3x its period to converge after warmup.
            htf_bars = max(self.cfg.htf_ma,
                           3 * self.cfg.rsi_period if self.cfg.htf_rsi_level else 0)
            htf_hours = {"1Hour": 1, "4Hour": 4, "1Day": 6.5}[self.cfg.htf_timeframe]
            htf_trading_days = htf_bars * htf_hours / 6.5
            days = max(days, int(htf_trading_days * 1.6) + 10)
        return days

    def run_once(self) -> None:
        if self.cfg.trail_percent:
            self.reconcile_stop_fills()
        clock = self.trading.get_clock()
        if not clock.is_open:
            log.info("Market closed (next open %s), skipping pass", clock.next_open)
            return
        # Enough history to warm up the RSI and trend MA regardless of timeframe.
        bars = self.fetch_bars(self.cfg.symbols, days=self._live_fetch_days())
        for symbol in self.cfg.symbols:
            symbol = symbol.strip().upper()
            try:
                self._eval_symbol(symbol, bars)
            except Exception:
                # One symbol's failure (rejected order, API hiccup) must not
                # cost the remaining symbols their pass — on the daily
                # timeframe that would forfeit the whole trading day.
                log.exception("%s: pass failed; continuing with next symbol",
                              symbol)

    def _eval_symbol(self, symbol: str, bars: pd.DataFrame) -> None:
            df = self.closed_bars(symbol, bars)
            if df is None or len(df) < self.cfg.rsi_period + 2:
                log.warning("%s: not enough data, skipping", symbol)
                return
            closes = df["close"]
            r = rsi(closes, self.cfg.rsi_period)
            # Volume on the latest bar vs the average of the bars before it,
            # computed regardless of the filter so it lands in the journal.
            avg_vol = df["volume"].shift(1).rolling(self.cfg.vol_lookback).mean().iloc[-1]
            rvol = float(df["volume"].iloc[-1] / avg_vol) \
                if avg_vol and not pd.isna(avg_vol) else None
            signal = crossover_signal(r, self.cfg.rsi_buy, self.cfg.rsi_sell)
            if signal == "buy" and self.cfg.vol_mult:
                if rvol is None or rvol < self.cfg.vol_mult:
                    log.info("%s: buy signal vetoed by volume filter (rvol %s < %.2f)",
                             symbol, f"{rvol:.2f}" if rvol else "n/a", self.cfg.vol_mult)
                    signal = None
            if signal == "buy" and self.cfg.trend_ma:
                ma = ma_series(closes, self.cfg.trend_ma, self.cfg.ma_type).iloc[-1]
                if pd.isna(ma) or closes.iloc[-1] <= ma:
                    log.info("%s: buy signal vetoed by trend filter (price %.2f <= %s%d %.2f)",
                             symbol, closes.iloc[-1], self.cfg.ma_type.upper(),
                             self.cfg.trend_ma, ma)
                    signal = None
            # HTF confirmation only applies when resampling *up* from an
            # intraday timeframe — same gate the backtest applies.
            if (signal == "buy" and self.cfg.htf_ma
                    and self.timeframe.unit != TimeFrameUnit.Day):
                htf_up = htf_trend_ok(df, HTF_RULES[self.cfg.htf_timeframe],
                                      self.cfg.htf_ma, self.bar_len()).iloc[-1]
                if not htf_up:
                    log.info("%s: buy signal vetoed by HTF trend filter "
                             "(%s close below MA%d)", symbol,
                             self.cfg.htf_timeframe, self.cfg.htf_ma)
                    signal = None
            if (signal == "buy" and self.cfg.htf_rsi_level
                    and self.timeframe.unit != TimeFrameUnit.Day):
                rsi_up = htf_rsi_ok(df, HTF_RULES[self.cfg.htf_timeframe],
                                    self.cfg.htf_rsi_level, self.cfg.rsi_period,
                                    self.bar_len()).iloc[-1]
                if not rsi_up:
                    log.info("%s: buy signal vetoed by HTF RSI filter "
                             "(%s RSI below %g)", symbol,
                             self.cfg.htf_timeframe, self.cfg.htf_rsi_level)
                    signal = None
            log.info("%s: RSI=%.1f signal=%s", symbol, r.iloc[-1], signal or "none")
            ctx = {"price": float(closes.iloc[-1]), "rsi": float(r.iloc[-1]), "rvol": rvol}
            if signal == "buy":
                self.enter(symbol, ctx)
            elif signal == "sell":
                self.exit(symbol, ctx)

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

    def entry_exit_signals(
        self,
        df: pd.DataFrame,
        buy_level: float,
        sell_level: float,
        ma_period: int,
        vol_mult: float = 0,
        htf_ma: int = 0,
    ) -> tuple[pd.Series, pd.Series]:
        """Boolean (cross_up, cross_down) Series for one symbol, entry filters
        applied. Single source of signal truth for both simulators."""
        closes = df["close"]
        r = rsi(closes, self.cfg.rsi_period)
        cross_up = (r > buy_level) & (r.shift(1) <= buy_level)
        cross_down = (r < sell_level) & (r.shift(1) >= sell_level)
        if ma_period:
            cross_up &= closes > ma_series(closes, ma_period, self.cfg.ma_type)
        if vol_mult:
            avg_vol = df["volume"].shift(1).rolling(self.cfg.vol_lookback).mean()
            cross_up &= df["volume"] >= avg_vol * vol_mult
        if htf_ma:
            cross_up &= htf_trend_ok(df, HTF_RULES[self.cfg.htf_timeframe],
                                     htf_ma, self.bar_len())
        # HTF RSI confirmation is a cfg-level filter (not a per-variant param):
        # when set, it gates EVERY variant in a backtest run — variant names
        # get a suffix so backtest_results.csv rows stay unambiguous.
        if self.cfg.htf_rsi_level and self.timeframe.unit != TimeFrameUnit.Day:
            cross_up &= htf_rsi_ok(df, HTF_RULES[self.cfg.htf_timeframe],
                                   self.cfg.htf_rsi_level, self.cfg.rsi_period,
                                   self.bar_len())
        # Live only acts on a bar whose close lands inside regular NY hours
        # (run_once skips closed markets; a bar ending at 16:00 completes at
        # the bell, after the last actionable poll). Mask both directions —
        # an off-hours sell cross is held through, not fast-forwarded — while
        # the indicators above still warm up on every bar, exactly like live.
        # Approximation: fixed 9:30-16:00, ignores half-day early closes.
        if self.cfg.rth_only and self.timeframe.unit != TimeFrameUnit.Day:
            ends = (df.index + self.bar_len()).tz_convert("America/New_York")
            mins = ends.hour * 60 + ends.minute
            actionable = pd.Series((mins >= 570) & (mins < 960), index=df.index)
            cross_up &= actionable
            cross_down &= actionable
        return cross_up, cross_down

    def simulate_portfolio(
        self,
        dfs: dict[str, pd.DataFrame],
        buy_level: float,
        sell_level: float,
        ma_period: int,
        vol_mult: float = 0,
        trail_pct: float = 0,
        htf_ma: int = 0,
    ) -> dict:
        """Simulate every symbol on ONE shared account, sized like live trading.

        Entries take min(NOTIONAL_PCT% of current equity, available cash) —
        the same sizing `enter()` uses (flat NOTIONAL fallback when the pct is
        0) — so unlike `simulate()`, which puts 100% into each symbol
        independently, this reports the equity curve of the deployed
        portfolio: shared cash, compounding, and entries skipped when cash ran
        out. Exits are processed before entries on each bar; fills at bar
        close with COST_BPS friction per side; fractional shares (live sizing
        is whole-share only when a trailing stop is on).
        """
        cfg = self.cfg
        c = cfg.cost_bps / 10000
        closes = pd.DataFrame(
            {s: df["close"] for s, df in dfs.items()}).sort_index()
        syms = list(closes.columns)
        sig = {s: self.entry_exit_signals(dfs[s], buy_level, sell_level,
                                          ma_period, vol_mult, htf_ma)
               for s in syms}
        idx = closes.index
        ups = np.column_stack(
            [sig[s][0].reindex(idx, fill_value=False).to_numpy() for s in syms])
        downs = np.column_stack(
            [sig[s][1].reindex(idx, fill_value=False).to_numpy() for s in syms])
        has_bar = closes.notna().to_numpy()
        px = closes.ffill().to_numpy()  # last known price, for marking equity

        nsym = len(syms)
        qty = np.zeros(nsym)
        entry_px = np.zeros(nsym)  # cost basis incl. entry friction
        hwm = np.zeros(nsym)
        cash = cfg.start_equity
        equity_curve = np.empty(len(idx))
        invested_frac = np.empty(len(idx))
        trade_returns: list[float] = []
        trades_per_sym = {s: 0 for s in syms}
        for i in range(len(idx)):
            exited = np.zeros(nsym, dtype=bool)
            # Exits first: same-bar sales free cash for entries elsewhere.
            for j in range(nsym):
                if qty[j] and has_bar[i, j]:
                    p = px[i, j]
                    hwm[j] = max(hwm[j], p)
                    if downs[i, j] or (
                            trail_pct and p <= hwm[j] * (1 - trail_pct / 100)):
                        cash += qty[j] * p * (1 - c)
                        trade_returns.append(p * (1 - c) / entry_px[j] - 1)
                        trades_per_sym[syms[j]] += 1
                        qty[j] = 0.0
                        exited[j] = True
            equity = cash + float(np.nansum(qty * px[i]))
            for j in range(nsym):
                # A symbol that exited this bar can't re-enter on the same bar
                # (mirrors simulate(); live can't either — one pass per bar).
                if qty[j] or not ups[i, j] or exited[j] or cash <= 1e-9:
                    continue
                notional = (min(equity * cfg.notional_pct / 100, cash)
                            if cfg.notional_pct else min(cfg.notional, cash))
                p = px[i, j]
                qty[j] = notional / (p * (1 + c))
                entry_px[j] = p * (1 + c)
                hwm[j] = p
                cash -= notional
            pos_val = float(np.nansum(qty * px[i]))
            equity_curve[i] = cash + pos_val
            invested_frac[i] = pos_val / equity_curve[i]

        eq = pd.Series(equity_curve, index=idx)
        span_days = max((idx[-1] - idx[0]).days, 1)
        wins = sum(1 for r in trade_returns if r > 0)
        return {
            "equity": eq,
            "final_equity": float(eq.iloc[-1]),
            "return_pct": (float(eq.iloc[-1]) / cfg.start_equity - 1) * 100,
            "cagr_pct": ((float(eq.iloc[-1]) / cfg.start_equity)
                         ** (365.25 / span_days) - 1) * 100,
            "max_dd_pct": float((eq / eq.cummax() - 1).min() * 100),
            "exposure_pct": float(np.mean(invested_frac) * 100),
            "trades": len(trade_returns),
            "win_rate": (wins / len(trade_returns) * 100) if trade_returns else 0.0,
            "trades_per_sym": trades_per_sym,
            "open_positions": [syms[j] for j in range(nsym) if qty[j]],
        }

    def simulate(
        self,
        df: pd.DataFrame,
        buy_level: float,
        sell_level: float,
        ma_period: int,
        vol_mult: float = 0,
        trail_pct: float = 0,
        htf_ma: int = 0,
        eval_from: int = 0,
        eval_to: int | None = None,
    ) -> tuple[int, float, float]:
        """Simulate one variant; returns (trades, win rate %, total return %).

        In the market from the bar after RSI crosses above buy_level until the
        bar after RSI crosses below sell_level OR the close drops trail_pct%
        below the trade's high-water mark (signals act on the next bar).
        eval_from/eval_to restrict *scoring* to a bar range while indicators
        still warm up on the full history (used for walk-forward splits).
        COST_BPS friction is charged per trade round trip; a trade left open
        at the window edge is charged the full round trip anyway.
        """
        closes = df["close"]
        cross_up, cross_down = self.entry_exit_signals(
            df, buy_level, sell_level, ma_period, vol_mult, htf_ma)
        # The trailing stop is path-dependent (high-water mark), so walk the
        # bars to build the in-market series.
        c = closes.to_numpy()
        up = cross_up.to_numpy()
        down = cross_down.to_numpy()
        in_arr = np.zeros(len(c), dtype=bool)
        in_pos, hwm = False, 0.0
        for i in range(len(c)):
            if in_pos:
                in_arr[i] = True
                hwm = max(hwm, c[i])
                if down[i] or (trail_pct and c[i] <= hwm * (1 - trail_pct / 100)):
                    in_pos = False
            if not in_pos and up[i] and not in_arr[i]:
                in_pos, hwm = True, c[i]
        in_market = pd.Series(in_arr, index=closes.index)
        bar_returns = closes.pct_change().fillna(0)
        if eval_from or eval_to is not None:
            in_market = in_market.iloc[eval_from:eval_to]
            bar_returns = bar_returns.iloc[eval_from:eval_to]
        # Group consecutive in-market bars into trades for the win rate.
        grp = (in_market != in_market.shift()).cumsum()
        trade_returns = (1 + bar_returns[in_market]).groupby(grp[in_market]).prod() - 1
        trades = len(trade_returns)
        # Friction: buy at close*(1+c), sell at close*(1-c) — one round-trip
        # factor per trade. c=0 gives cost_f=1.0 and reproduces the
        # frictionless legacy numbers bit-for-bit.
        c = self.cfg.cost_bps / 10000
        cost_f = (1 - c) / (1 + c) if c else 1.0
        trade_returns = (1 + trade_returns) * cost_f - 1
        win_rate = float((trade_returns > 0).mean() * 100) if trades else 0.0
        total = ((1 + bar_returns * in_market).prod() * cost_f ** trades - 1) * 100
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
        tp = self.cfg.trail_percent or 8
        # HTF trend confirmation only applies when resampling *up* from an
        # intraday trading timeframe.
        htf_ok = self.timeframe.unit != TimeFrameUnit.Day
        variants = [
            # The resolved config (shell env > .env > profile > defaults), so
            # any candidate profile can be A/B'd against the standard variants
            # by overriding knobs per-invocation.
            ("active settings", self.cfg.rsi_buy, self.cfg.rsi_sell,
             self.cfg.trend_ma, self.cfg.vol_mult, self.cfg.trail_percent,
             self.cfg.htf_ma if htf_ok else 0),
            (f"RSI {m:g} cross", m, m, 0, 0, 0, 0),
            ("Band 55/45", 55, 45, 0, 0, 0, 0),
            (f"RSI {m:g} + MA200", m, m, 200, 0, 0, 0),
            (f"RSI {m:g} + vol x{vm:g}", m, m, 0, vm, 0, 0),
            (f"Band + vol x{vm:g}", 55, 45, 0, vm, 0, 0),
            (f"Band + vol + MA200", 55, 45, 200, vm, 0, 0),
            (f"RSI {m:g} + trail {tp:g}%", m, m, 0, 0, tp, 0),
            (f"Band + trail {tp:g}%", 55, 45, 0, 0, tp, 0),
        ]
        if htf_ok:
            hm = self.cfg.htf_ma or 50
            ht = HTF_RULES[self.cfg.htf_timeframe]
            variants += [
                (f"Band + {ht} MA{hm}", 55, 45, 0, 0, 0, hm),
                (f"Band + MA200 + {ht} MA{hm}", 55, 45, 200, 0, 0, hm),
                (f"Band+vol+MA200+{ht} MA{hm}", 55, 45, 200, vm, 0, hm),
            ]
        print(f"\nBacktest: RSI({self.cfg.rsi_period}), timeframe={self.cfg.timeframe}, "
              f"last {self.cfg.backtest_days} days\n(MA200 = 200 bars of this timeframe; "
              f"vol = signal-bar volume vs {self.cfg.vol_lookback}-bar average"
              + (f"; {ht} MA{hm} = close above MA{hm} on {self.cfg.htf_timeframe} "
                 f"bars resampled from this timeframe)" if htf_ok else ")"))
        print(f"'active settings' = resolved config (env > .env > profile): "
              f"band {self.cfg.rsi_buy:g}/{self.cfg.rsi_sell:g}, "
              f"MA {self.cfg.trend_ma} ({self.cfg.ma_type}), "
              f"vol x{self.cfg.vol_mult:g}, "
              f"trail {self.cfg.trail_percent:g}%"
              + (f", {self.cfg.htf_timeframe} MA{self.cfg.htf_ma}"
                 if htf_ok and self.cfg.htf_ma else ""))
        # These two knobs are cfg-global (not per-variant), so when set they
        # gate every row below; tag the names so the CSV log stays unambiguous.
        suffix = ""
        if self.cfg.ma_type != "sma":
            suffix += f" [{self.cfg.ma_type}]"
        if htf_ok and not self.cfg.rth_only:
            print("GLOBAL: RTH_ONLY=false — acting on extended-hours bars "
                  "live trading never sees (pre-2026-07-13 behavior)")
            suffix += " [all-hours]"
        if htf_ok and self.cfg.htf_rsi_level:
            print(f"GLOBAL FILTER on all variants: {self.cfg.htf_timeframe} "
                  f"RSI > {self.cfg.htf_rsi_level:g}")
            suffix += f" [+{ht} RSI>{self.cfg.htf_rsi_level:g}]"
        results = []
        run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
            header = f"  {'variant':<26}{'trades':>7}{'win %':>8}{'return':>9}"
            print(header)
            print("  " + "-" * (len(header) - 2))
            for name, buy, sell, ma, vol, trail, htf in variants:
                name += suffix
                trades, win_rate, total = self.simulate(df, buy, sell, ma, vol,
                                                        trail, htf)
                print(f"  {name:<26}{trades:>7}{win_rate:>7.1f}%{total:>+8.1f}%")
                results.append({
                    "run_ts": run_ts, "timeframe": self.cfg.timeframe,
                    "days": self.cfg.backtest_days, "rsi_period": self.cfg.rsi_period,
                    "symbol": symbol, "variant": name, "rsi_buy": buy,
                    "rsi_sell": sell, "trend_ma": ma, "vol_mult": vol,
                    "trail_pct": trail, "htf_ma": htf,
                    "htf_timeframe": self.cfg.htf_timeframe if htf else "",
                    "trades": trades, "win_rate": round(win_rate, 2),
                    "return_pct": round(total, 2), "buyhold_pct": round(hold, 2),
                })
        self._log_backtest_results(results)
        print(f"\nNote: fills at signal-bar close; friction COST_BPS="
              f"{self.cfg.cost_bps:g} bps/side; bars adjustment="
              f"'{self.cfg.bar_adjustment}'"
              + (f"; signals restricted to 9:30-16:00 ET (RTH_ONLY=true)"
                 if htf_ok and self.cfg.rth_only else "")
              + ". Each symbol simulated in "
              f"isolation, 100% invested — see `portfolio` for shared-account "
              f"sizing.\n")

    def portfolio(self) -> None:
        """Backtest the resolved config as one shared account (live sizing)."""
        cfg = self.cfg
        log.info(
            "Fetching %d days of %s bars for %s — this can take a minute...",
            cfg.backtest_days, cfg.timeframe, ",".join(cfg.symbols),
        )
        bars = self.fetch_bars(cfg.symbols, days=cfg.backtest_days)
        dfs: dict[str, pd.DataFrame] = {}
        for symbol in cfg.symbols:
            symbol = symbol.strip().upper()
            try:
                dfs[symbol] = bars.xs(symbol, level="symbol")
            except KeyError:
                print(f"{symbol}: no data, excluded")
        if not dfs:
            sys.exit("No data for any symbol")
        htf_ok = self.timeframe.unit != TimeFrameUnit.Day
        res = self.simulate_portfolio(
            dfs, cfg.rsi_buy, cfg.rsi_sell, cfg.trend_ma, cfg.vol_mult,
            cfg.trail_percent, cfg.htf_ma if htf_ok else 0)

        # Benchmark: equal-weight buy & hold from the first bar where every
        # symbol trades, entry friction charged, marked to market at the end.
        c = cfg.cost_bps / 10000
        closes = pd.DataFrame(
            {s: df["close"] for s, df in dfs.items()}).sort_index().ffill().dropna()
        shares = (cfg.start_equity / len(closes.columns)) / (closes.iloc[0] * (1 + c))
        bh = (closes * shares).sum(axis=1)
        bh_ret = (bh.iloc[-1] / cfg.start_equity - 1) * 100
        bh_dd = float((bh / bh.cummax() - 1).min() * 100)

        sizing = (f"{cfg.notional_pct:g}% of equity"
                  if cfg.notional_pct else f"flat ${cfg.notional:g}")
        print(f"\nPortfolio backtest: {cfg.timeframe}, last {cfg.backtest_days} "
              f"days, {', '.join(dfs)}\nstart ${cfg.start_equity:g}, entries "
              f"{sizing} (cash-capped), COST_BPS={cfg.cost_bps:g}/side, "
              f"adjustment='{cfg.bar_adjustment}'")
        print(f"band {cfg.rsi_buy:g}/{cfg.rsi_sell:g}, MA {cfg.trend_ma} "
              f"({cfg.ma_type}), vol x{cfg.vol_mult:g}, trail {cfg.trail_percent:g}%"
              + (f", {cfg.htf_timeframe} MA{cfg.htf_ma}"
                 if htf_ok and cfg.htf_ma else "")
              + (f", {cfg.htf_timeframe} RSI>{cfg.htf_rsi_level:g}"
                 if htf_ok and cfg.htf_rsi_level else ""))
        print(f"\n  {'':<14}{'return':>9}{'CAGR':>8}{'max DD':>8}")
        print(f"  {'strategy':<14}{res['return_pct']:>+8.1f}%"
              f"{res['cagr_pct']:>+7.1f}%{res['max_dd_pct']:>7.1f}%")
        print(f"  {'buy & hold':<14}{bh_ret:>+8.1f}%"
              f"{((bh.iloc[-1] / cfg.start_equity) ** (365.25 / max((bh.index[-1] - bh.index[0]).days, 1)) - 1) * 100:>+7.1f}%"
              f"{bh_dd:>7.1f}%")
        per_sym = ", ".join(f"{s} {n}" for s, n in res["trades_per_sym"].items())
        print(f"\n  final equity ${res['final_equity']:,.0f}, avg exposure "
              f"{res['exposure_pct']:.0f}%, {res['trades']} closed trades "
              f"(win rate {res['win_rate']:.1f}%): {per_sym}")
        if res["open_positions"]:
            print(f"  still open at window end (marked to market): "
                  f"{', '.join(res['open_positions'])}")
        run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._log_backtest_results([{
            "run_ts": run_ts, "timeframe": cfg.timeframe,
            "days": cfg.backtest_days, "rsi_period": cfg.rsi_period,
            "symbol": "PORTFOLIO",
            "variant": (f"portfolio {sizing}, {cfg.cost_bps:g}bps"
                        + (f" [{cfg.ma_type}]" if cfg.ma_type != "sma" else "")
                        + (f" [+{cfg.htf_timeframe} RSI>{cfg.htf_rsi_level:g}]"
                           if htf_ok and cfg.htf_rsi_level else "")),
            "rsi_buy": cfg.rsi_buy, "rsi_sell": cfg.rsi_sell,
            "trend_ma": cfg.trend_ma, "vol_mult": cfg.vol_mult,
            "trail_pct": cfg.trail_percent,
            "htf_ma": cfg.htf_ma if htf_ok else 0,
            "htf_timeframe": cfg.htf_timeframe if htf_ok and cfg.htf_ma else "",
            "trades": res["trades"], "win_rate": round(res["win_rate"], 2),
            "return_pct": round(res["return_pct"], 2),
            "buyhold_pct": round(bh_ret, 2),
        }])
        print(f"  (max drawdown strategy {res['max_dd_pct']:.1f}% vs buy & "
              f"hold {bh_dd:.1f}% — the number the per-symbol backtest "
              f"can't see)\n")

    # -- replay: live period vs journal ---------------------------------------

    def _actionable_times(self, index: pd.DatetimeIndex) -> pd.DatetimeIndex:
        """When live could first act on each bar: intraday bars at their
        close, daily bars at the ~15:50 ET evaluation (10 min before the
        bell, where the daily loop wakes up)."""
        if self.timeframe.unit == TimeFrameUnit.Day:
            ny = index.tz_convert("America/New_York")
            return (ny.normalize()
                    + pd.Timedelta(hours=15, minutes=50)).tz_convert("UTC")
        return index + self.bar_len()

    def _bar_for_ts(self, index: pd.DatetimeIndex, ts: pd.Timestamp):
        """The bar a journal row acted on: the last completed bar at the
        journal timestamp (daily: the bar of that ET trading day, which live
        evaluates just before its own close)."""
        if self.timeframe.unit == TimeFrameUnit.Day:
            d = ts.tz_convert("America/New_York").date()
            hits = index[index.tz_convert("America/New_York").date == d]
            return hits[0] if len(hits) else None
        pos = (index + self.bar_len()).searchsorted(ts, side="right") - 1
        return index[pos] if pos >= 0 else None

    def _sim_trade_events(self, df: pd.DataFrame,
                          since: pd.Timestamp) -> tuple[list[tuple], pd.Series]:
        """Replay the live decision sequence on one symbol: the same signal
        source and position state machine as `simulate()`, but starting FLAT
        at `since` (crosses from before the bot existed never fired live)
        and returning the trade events instead of aggregate returns.

        Returns ([(bar_ts, side, close, rsi)], rsi_series)."""
        htf_ok = self.timeframe.unit != TimeFrameUnit.Day
        cross_up, cross_down = self.entry_exit_signals(
            df, self.cfg.rsi_buy, self.cfg.rsi_sell, self.cfg.trend_ma,
            self.cfg.vol_mult, self.cfg.htf_ma if htf_ok else 0)
        r = rsi(df["close"], self.cfg.rsi_period)
        acts = self._actionable_times(df.index)
        now = pd.Timestamp.now(tz="UTC")
        trail = self.cfg.trail_percent
        events: list[tuple] = []
        in_pos, hwm = False, 0.0
        for i in range(len(df)):
            # Bars live never got to act on (before go-live, or a daily bar
            # whose evaluation window hasn't arrived yet) don't move state.
            if not (since <= acts[i] <= now):
                continue
            c = float(df["close"].iloc[i])
            exited = False
            if in_pos:
                hwm = max(hwm, c)
                if cross_down.iloc[i] or (trail and c <= hwm * (1 - trail / 100)):
                    events.append((df.index[i], "sell", c, float(r.iloc[i])))
                    in_pos, exited = False, True
            if not in_pos and cross_up.iloc[i] and not exited:
                events.append((df.index[i], "buy", c, float(r.iloc[i])))
                in_pos, hwm = True, c
        return events, r

    def replay(self, since_arg: str | None = None) -> int:
        """Re-simulate the live period with the resolved config and diff the
        simulated trade list against the journal (TRADES_DB).

        The production version of the repo's 'identical rows' verification
        habit: any SIM-only or LIVE-only event means live behavior and
        backtest assumptions have diverged — a strategy/data bug caught
        within days instead of surfacing in the P&L months later (the
        extended-hours gap would have shown up on day one). Returns the
        mismatch count; main() maps it to the exit code so the weekly
        systemd run turns red on divergence.

        Window start: `since` argument, else REPLAY_SINCE (set it to the
        instance's go-live moment), else the first journal row. The sim
        starts FLAT at that moment, like the freshly reset paper account.

        Honest disagreements this will surface (explain, don't paper over):
        daily bars are evaluated live ~10 min before the close on a
        near-final bar; BAR_ADJUSTMENT=all re-adjusts history after every
        dividend, so bars fetched today differ slightly from what live saw
        before an ex-div date; server-side trailing stops fill at real
        intraday prices, not bar closes.
        """
        cfg = self.cfg
        daily = self.timeframe.unit == TimeFrameUnit.Day
        db_path = os.environ.get("TRADES_DB") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "trades.db")
        rows: list[tuple] = []
        if os.path.exists(db_path):
            # Read-only connection: never contends with the live bot's writes
            # (the rw journal the constructor opened is left untouched).
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT ts, symbol, side, price, rsi, timeframe, rsi_buy,"
                " rsi_sell, trend_ma, vol_mult, htf_ma, ma_type, htf_rsi"
                " FROM trades ORDER BY id").fetchall()
            conn.close()
        since_s = (since_arg or os.environ.get("REPLAY_SINCE")
                   or (rows[0][0] if rows else None))
        if not since_s:
            sys.exit("replay: journal is empty and no window start given — "
                     "set REPLAY_SINCE to the bot's go-live time (so a bot "
                     "that WRONGLY never trades still gets checked) or pass "
                     "a date: rsi_midline_bot.py replay 2026-07-12")
        since = _utc_ts(since_s)
        window_days = max(int((pd.Timestamp.now(tz="UTC") - since).days) + 2, 3)
        symbols = [s.strip().upper() for s in cfg.symbols]
        days = self._live_fetch_days() + window_days
        log.info("Replay since %s: fetching %d days of %s bars for %s...",
                 since, days, cfg.timeframe, ",".join(symbols))
        bars = self.fetch_bars(symbols, days=days)

        htf_ok = not daily
        print(f"\nReplay vs journal: {cfg.timeframe} {','.join(symbols)} "
              f"since {since:%Y-%m-%d %H:%M} UTC\njournal: {db_path}")
        print(f"config: RSI({cfg.rsi_period}) band {cfg.rsi_buy:g}/{cfg.rsi_sell:g}, "
              f"MA {cfg.trend_ma} ({cfg.ma_type}), vol x{cfg.vol_mult:g}, "
              f"trail {cfg.trail_percent:g}%"
              + (f", {cfg.htf_timeframe} MA{cfg.htf_ma}"
                 if htf_ok and cfg.htf_ma else "")
              + (f", {cfg.htf_timeframe} RSI>{cfg.htf_rsi_level:g}"
                 if htf_ok and cfg.htf_rsi_level else ""))
        if cfg.trail_percent:
            print("NOTE: TRAIL_PERCENT set — server-side stop fills happen at "
                  "intraday prices; expect bar-level mismatches on stopped "
                  "exits (the sim exits on bar closes).")

        # Journal rows carry the settings each trade ran with: rows that
        # disagree with the replay config mean the config changed mid-window
        # (or the replay is running the wrong env) — mismatches below would
        # be expected, not bugs.
        live_rows = [r_ for r_ in rows if _utc_ts(r_[0]) >= since]
        current = {"timeframe": cfg.timeframe, "rsi_buy": cfg.rsi_buy,
                   "rsi_sell": cfg.rsi_sell, "trend_ma": cfg.trend_ma,
                   "vol_mult": cfg.vol_mult, "htf_ma": cfg.htf_ma,
                   "ma_type": cfg.ma_type, "htf_rsi": cfg.htf_rsi_level}
        drift: dict[tuple, int] = {}
        for r_ in live_rows:
            row_cfg = dict(zip(current, r_[5:13]))
            diffs = tuple(f"{k}={row_cfg[k]!r} (replaying {current[k]!r})"
                          for k in current
                          if row_cfg[k] is not None and row_cfg[k] != current[k])
            if diffs:
                drift[diffs] = drift.get(diffs, 0) + 1
        for diffs, n in drift.items():
            print(f"CONFIG DRIFT on {n} journal row(s): " + ", ".join(diffs))

        rows_by_sym: dict[str, list[tuple]] = {}
        for r_ in live_rows:
            rows_by_sym.setdefault(r_[1], []).append(r_)
        matched = mismatches = 0
        # Journal rows for symbols the replay config doesn't trade can't be
        # simulated at all — count them as mismatches, don't drop them.
        for sym in sorted(set(rows_by_sym) - set(symbols)):
            n = len(rows_by_sym[sym])
            print(f"\n{sym}: {n} journal row(s) but symbol not in SYMBOLS — "
                  f"can't replay (config drift)")
            mismatches += n

        fmt = "%Y-%m-%d" if daily else "%Y-%m-%d %H:%M"
        for symbol in symbols:
            df = self.closed_bars(symbol, bars)
            if df is None or df.empty:
                print(f"\n{symbol}: no bar data")
                continue
            events, r_series = self._sim_trade_events(df, since)
            sim_map: dict[tuple, list[tuple]] = {}
            for ts, side, close, rv in events:
                sim_map.setdefault((ts, side), []).append((close, rv))
            live_map: dict[tuple, list[tuple]] = {}
            unmapped: list[tuple] = []
            for r_ in rows_by_sym.get(symbol, []):
                bar = self._bar_for_ts(df.index, _utc_ts(r_[0]))
                if bar is None:
                    unmapped.append(r_)
                else:
                    live_map.setdefault((bar, r_[2]), []).append(r_)
            keys = sorted(set(sim_map) | set(live_map))
            if not keys and not unmapped:
                print(f"\n{symbol}: no trades (sim or journal)")
                continue
            print(f"\n{symbol}")
            print(f"  {'signal bar (ET)':<18}{'side':<6}{'status':<11}"
                  f"{'px sim/live':>18}{'rsi sim/live':>15}")
            for bar_ts, side in keys:
                sims = sim_map.get((bar_ts, side), [])
                lives = live_map.get((bar_ts, side), [])
                label = bar_ts.tz_convert("America/New_York").strftime(fmt)
                for i in range(max(len(sims), len(lives))):
                    s = sims[i] if i < len(sims) else None
                    l = lives[i] if i < len(lives) else None
                    if s and l:
                        matched += 1
                        status = "OK"
                        px = (f"{s[0]:.2f}/{l[3]:.2f}"
                              if l[3] is not None else f"{s[0]:.2f}/?")
                        rs = (f"{s[1]:.1f}/{l[4]:.1f}"
                              if l[4] is not None else f"{s[1]:.1f}/?")
                        # Price drift beyond ~re-adjustment size, or RSI far
                        # from what live logged, deserves a second look even
                        # though the bars matched.
                        if (l[3] and abs(l[3] / s[0] - 1) > (0.01 if daily else 0.002)) \
                                or (l[4] is not None and abs(l[4] - s[1]) > 1.0):
                            status = "OK?"
                    elif s:
                        mismatches += 1
                        status, px, rs = "SIM-ONLY", f"{s[0]:.2f}/-", f"{s[1]:.1f}/-"
                    else:
                        mismatches += 1
                        sim_rsi = float(r_series.loc[bar_ts])
                        status = "LIVE-ONLY"
                        px = (f"-/{l[3]:.2f}" if l[3] is not None else "-/?")
                        rs = (f"{sim_rsi:.1f}/{l[4]:.1f}"
                              if l[4] is not None else f"{sim_rsi:.1f}/?")
                    print(f"  {label:<18}{side:<6}{status:<11}{px:>18}{rs:>15}")
            for r_ in unmapped:
                mismatches += 1
                print(f"  {r_[0][:16]:<18}{r_[2]:<6}{'LIVE-ONLY':<11}"
                      f"{'no bar for journal ts':>33}")

        verdict = "MISMATCH" if mismatches else "OK"
        print(f"\nREPLAY {verdict}: {matched} matched, {mismatches} "
              f"mismatched event(s) since {since:%Y-%m-%d}.")
        if mismatches:
            print("The FIRST mismatch is the root-cause candidate: position "
                  "state diverges there, so later rows can be knock-on "
                  "effects. SIM-ONLY = live missed a trade the strategy "
                  "called for (downtime? veto? sizing skip?); LIVE-ONLY = "
                  "live traded when the re-simulation wouldn't (data "
                  "revision? borderline RSI? config drift above?).")
        print("(bars re-adjust after each dividend, so pre-ex-div prices can "
              "drift ~the dividend size; daily rows are evaluated live 10 min "
              "before the close on a near-final bar)")
        return mismatches

    def _log_backtest_results(self, rows: list[dict]) -> None:
        """Append every variant result to backtest_results.csv for comparison
        across runs — the experiment log that survives the terminal scrollback."""
        if not rows:
            return
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "backtest_results.csv")
        new_file = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            if new_file:
                writer.writeheader()
            writer.writerows(rows)
        print(f"\nLogged {len(rows)} variant results to backtest_results.csv")

    # -- tuning ---------------------------------------------------------------

    GRID_BANDS = [(50, 50), (55, 45), (60, 40)]
    GRID_MAS = [0, 50, 200]
    GRID_VOLS = [0, 1.5, 2.0]
    GRID_TRAILS = [0, 5, 10]

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

        def avg_scores(buy: float, sell: float, ma: int, vol: float,
                       trail: float) -> dict:
            train, test, train_trades, test_trades = [], [], 0, 0
            for sym, df in dfs.items():
                sp = splits[sym]
                n_tr, _, r_tr = self.simulate(df, buy, sell, ma, vol, trail, eval_to=sp)
                n_te, _, r_te = self.simulate(df, buy, sell, ma, vol, trail, eval_from=sp)
                train.append(r_tr); test.append(r_te)
                train_trades += n_tr; test_trades += n_te
            return {"buy": buy, "sell": sell, "ma": ma, "vol": vol, "trail": trail,
                    "train": sum(train) / len(train), "test": sum(test) / len(test),
                    "train_trades": train_trades, "test_trades": test_trades}

        results = [avg_scores(buy, sell, ma, vol, trail)
                   for buy, sell in self.GRID_BANDS
                   for ma in self.GRID_MAS
                   for vol in self.GRID_VOLS
                   for trail in self.GRID_TRAILS]
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
        header = (f"  {'buy/sell':<10}{'MA':>5}{'vol':>6}{'trail':>7}"
                  f"{'train %':>10}{'test %':>9}{'test trades':>13}")
        print("  top 5 by TRAIN return — test column is out-of-sample:")
        print(header)
        print("  " + "-" * (len(header) - 2))
        for r in active[:5]:
            trail_s = f"{r['trail']:g}%" if r["trail"] else "off"
            print(f"  {r['buy']:g}/{r['sell']:<7g}{r['ma']:>5}{r['vol']:>6g}"
                  f"{trail_s:>7}"
                  f"{r['train']:>+9.1f}%{r['test']:>+8.1f}%{r['test_trades']:>13}")
        print(f"\n  buy & hold on test window: {hold_test:+.1f}%")

        # Gate: current profile settings evaluated on the same test window.
        current = self._current_profile_combo()
        cur = avg_scores(*current)
        print(f"  current profile ({current[0]:g}/{current[1]:g}, MA {current[2]}, "
              f"vol x{current[3]:g}, trail {current[4]:g}%) on test window: {cur['test']:+.1f}%")

        if winner["test"] < winner["train"] / 3:
            print("\n  WARNING: winner's out-of-sample return is far below its "
                  "training return — likely overfit to the training window.")
        if (winner["buy"], winner["sell"], winner["ma"], winner["vol"],
                winner["trail"]) == current:
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

    def show_trades(self, limit: int = 20) -> None:
        rows = self.journal.recent(limit)
        if not rows:
            print("No trades logged yet.")
            return
        header = (f"{'time (UTC)':<21}{'symbol':<7}{'side':<6}{'price':>9}"
                  f"{'amount':>12}{'RSI':>6}{'rvol':>6}{'tf':>7}{'paper':>7}")
        print(header)
        print("-" * len(header))
        for row in rows:
            r = dict(zip(TradeLog.COLUMNS, row))
            amount = f"${r['notional']:,.0f}" if r["side"] == "buy" \
                else f"{r['qty']:.4f} sh"
            print(f"{r['ts'][:19]:<21}{r['symbol']:<7}{r['side'].upper():<6}"
                  f"{r['price']:>9.2f}{amount:>12}{r['rsi']:>6.1f}"
                  f"{format(r['rvol'], '.2f') if r['rvol'] is not None else 'n/a':>6}"
                  f"{r['timeframe']:>7}{'yes' if r['paper'] else 'NO':>7}")

    def _profiles_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")

    def _current_profile_combo(self) -> tuple[float, float, int, float, float]:
        """Current profile's (buy, sell, ma, vol, trail), falling back to plain midline."""
        try:
            with open(self._profiles_path()) as f:
                s = json.load(f)[self.cfg.timeframe]["settings"]
            return (float(s.get("RSI_BUY_LEVEL", self.cfg.midline)),
                    float(s.get("RSI_SELL_LEVEL", self.cfg.midline)),
                    int(s.get("TREND_MA_PERIOD", 0)),
                    float(s.get("VOLUME_MULT", 0)),
                    float(s.get("TRAIL_PERCENT", 0)))
        except (OSError, KeyError, ValueError):
            return (self.cfg.midline, self.cfg.midline, 0, 0, 0)

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
            "TRAIL_PERCENT": f"{winner['trail']:g}",
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
                f"Grid: bands {self.GRID_BANDS}, MA {self.GRID_MAS}, "
                f"vol {self.GRID_VOLS}, trail% {self.GRID_TRAILS}."
            ),
            "settings": settings,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")


# ---------------------------------------------------------------------------

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode not in ("run", "loop", "backtest", "portfolio", "tune", "trades",
                    "pnl", "replay"):
        print(__doc__)
        sys.exit(1)
    load_dotenv()
    if mode == "pnl":  # journal analysis only — needs no API keys
        show_pnl(sys.argv[2:] or None)
        return
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
    elif mode == "trades":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        bot.show_trades(limit)
    elif mode == "portfolio":
        bot.portfolio()
    elif mode == "replay":
        # Non-zero exit on divergence so the weekly systemd run shows failed.
        sys.exit(1 if bot.replay(sys.argv[2] if len(sys.argv) > 2 else None)
                 else 0)
    else:
        bot.backtest()


if __name__ == "__main__":
    main()
