"""Intraday VWAP mean-reversion trading bot for Alpaca.

The complement to rsi_midline_bot.py. That bot is a trend-follower and
structurally loses intraday (it whipsaws when liquid ETFs oscillate instead of
trending). This bot harvests exactly that oscillation:

Strategy (long-only, intraday, flat overnight):
    - Anchor a session VWAP at each day's 9:30 ET open (resets daily).
    - Measure how far price has stretched from VWAP as a z-score of the
      close-minus-VWAP deviation.
    - BUY when price is stretched *below* VWAP (z <= -ENTRY_Z) AND short-term
      RSI(2) is oversold — a statistically extreme dip.
    - SELL (close) when price reverts to VWAP (z >= EXIT_Z), OR the dip
      extends into a stop (z <= -STOP_Z, or STOP_PCT below entry), OR the
      session is ending (EXIT_AT_CLOSE — no overnight risk).
    - Optional regime gate (REGIME_MA): only buy dips while the daily trend is
      up ("buy the dip in an uptrend").

Signals are evaluated on completed bars only and restricted to the bars live
could actually act on (regular trading hours). Imports the battle-tested pure
helpers (rsi, ma_series, load_dotenv, ...) from rsi_midline_bot so the
indicator/journal math never drifts between the two bots.

Usage:
    python mean_reversion_bot.py run        # evaluate signals once and trade
    python mean_reversion_bot.py loop       # run continuously on an interval
    python mean_reversion_bot.py backtest   # backtest variants on history
    python mean_reversion_bot.py portfolio  # one shared account, live sizing
    python mean_reversion_bot.py trades     # show the trade journal (SQLite)
    python mean_reversion_bot.py pnl [db..] # round-trip P&L from the journal(s)
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
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

# Reuse the RSI bot's proven, import-safe pure helpers (none of these touch the
# network or require API keys — Config's credential lookups are default_factory,
# evaluated only when that bot's Config() is instantiated, which we never do).
# This module lives in intraday/; add the repo root to the path so the shared
# rsi_midline_bot helpers still import when it's run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rsi_midline_bot import (TIMEFRAMES, htf_trend_ok, load_dotenv, ma_series,
                             rsi, show_pnl)
# Shared intraday primitives — one live-actionable definition for every
# sub-daily bot, so they can never drift from each other or from live.
from intraday_common import (RTH_CLOSE_MIN, RTH_OPEN_MIN, rth_actionable,
                             session_last_actionable)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("mean-reversion")


def apply_profile(timeframe: str) -> None:
    """Fill in tuned per-timeframe defaults from mr_profiles.json.

    Settings already in the environment (shell or .env) always win, so a
    profile only supplies knobs you haven't set. Mirrors the RSI bot's
    precedence, but reads this bot's own profiles file.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "mr_profiles.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        profile = json.load(f).get(timeframe)
    if not profile:
        log.warning("No profile for timeframe %s in mr_profiles.json", timeframe)
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

@dataclass
class Config:
    api_key: str = field(default_factory=lambda: os.environ["ALPACA_API_KEY"])
    secret_key: str = field(default_factory=lambda: os.environ["ALPACA_SECRET_KEY"])
    paper: bool = field(
        default_factory=lambda: os.environ.get("ALPACA_PAPER", "true").lower() != "false"
    )
    symbols: list[str] = field(
        default_factory=lambda: os.environ.get("SYMBOLS", "SPY,QQQ,IWM").split(",")
    )
    timeframe: str = field(default_factory=lambda: os.environ.get("TIMEFRAME", "5Min"))
    # -- strategy knobs -----------------------------------------------------
    # RSI period for the oversold confirmation (Connors-style short RSI).
    rsi_period: int = field(default_factory=lambda: int(os.environ.get("RSI2_PERIOD", "2")))
    # Buy only when RSI(rsi_period) <= this (oversold). 100 = off.
    rsi_oversold: float = field(
        default_factory=lambda: float(os.environ.get("RSI_OVERSOLD", "10")))
    # Enter when the close is at least ENTRY_Z std below session VWAP.
    entry_z: float = field(default_factory=lambda: float(os.environ.get("ENTRY_Z", "2.0")))
    # Exit (reversion target) when the close climbs back to ENTRY relative to
    # VWAP: z >= EXIT_Z (0 = all the way back to VWAP).
    exit_z: float = field(default_factory=lambda: float(os.environ.get("EXIT_Z", "0")))
    # Stop: exit if the dip extends to z <= -STOP_Z (0 = off).
    stop_z: float = field(default_factory=lambda: float(os.environ.get("STOP_Z", "0")))
    # Stop: exit if price falls STOP_PCT% below the entry price (0 = off).
    stop_pct: float = field(default_factory=lambda: float(os.environ.get("STOP_PCT", "0")))
    # Bars used for the rolling std of the VWAP deviation (the z denominator).
    std_lookback: int = field(
        default_factory=lambda: int(os.environ.get("VWAP_STD_LOOKBACK", "20")))
    # Regime gate: only buy dips when the daily close is above this daily MA
    # (0 = off, take dips regardless of trend). Aligned no-look-ahead.
    regime_ma: int = field(default_factory=lambda: int(os.environ.get("REGIME_MA", "0")))
    # Force flat near the session close (no overnight risk). The defining
    # intraday feature — for a mean-reverter, flat-at-close is correct.
    exit_at_close: bool = field(
        default_factory=lambda: os.environ.get("EXIT_AT_CLOSE", "true").lower() != "false")
    # -- sizing / friction / data (same names/semantics as the RSI bot) -----
    notional: float = field(default_factory=lambda: float(os.environ.get("NOTIONAL", "1000")))
    notional_pct: float = field(default_factory=lambda: float(os.environ.get("NOTIONAL_PCT", "0")))
    poll_seconds: int = field(default_factory=lambda: int(os.environ.get("POLL_SECONDS", "60")))
    backtest_days: int = field(default_factory=lambda: int(os.environ.get("BACKTEST_DAYS", "365")))
    cost_bps: float = field(default_factory=lambda: float(os.environ.get("COST_BPS", "0")))
    bar_adjustment: str = field(
        default_factory=lambda: os.environ.get("BAR_ADJUSTMENT", "all"))
    data_feed: str = field(default_factory=lambda: os.environ.get("DATA_FEED", ""))
    # Restrict entries/exits to bars live could act on (bar close inside RTH,
    # plus the last pre-open bar). "false" only for feed/hours experiments.
    rth_only: bool = field(
        default_factory=lambda: os.environ.get("RTH_ONLY", "true").lower() != "false")
    start_equity: float = field(
        default_factory=lambda: float(os.environ.get("START_EQUITY", "2000")))


# ---------------------------------------------------------------------------
# Indicators (session VWAP + deviation z-score)
# ---------------------------------------------------------------------------

def session_vwap_z(df: pd.DataFrame, bar_len: timedelta,
                   std_lookback: int) -> tuple[pd.Series, pd.Series]:
    """Session-anchored VWAP and the z-score of (close - VWAP).

    VWAP accumulates only over regular-hours bars (those ending in
    (9:30, 16:00] ET), resetting each ET trading day — the standard anchored
    VWAP. z = deviation / rolling std(deviation, std_lookback) over the RTH
    bars. Both are NaN outside RTH and during warmup; a NaN z never satisfies
    an entry/exit comparison, so off-hours bars are inert. No look-ahead: every
    value at bar t uses only bars up to and including t.
    """
    ends_ny = (df.index + bar_len).tz_convert("America/New_York")
    mins = ends_ny.hour * 60 + ends_ny.minute
    in_rth = (mins > RTH_OPEN_MIN) & (mins <= RTH_CLOSE_MIN)
    if not in_rth.any():
        nan = pd.Series(np.nan, index=df.index)
        return nan, nan.copy()

    rth = df[in_rth]
    ends_rth = ends_ny[in_rth]
    session = pd.Index(ends_rth.normalize())  # ET calendar day = VWAP anchor
    tp = (rth["high"] + rth["low"] + rth["close"]) / 3
    vol = rth["volume"]
    cum_vol = vol.groupby(session).cumsum()
    cum_pv = (tp * vol).groupby(session).cumsum()
    vwap_rth = cum_pv / cum_vol.replace(0, np.nan)
    dev_rth = rth["close"] - vwap_rth
    # Rolling std of the deviation. min_periods keeps early-session bars from
    # being all-NaN once a handful of bars exist; the window spans RTH bars
    # (contiguous in this subset), which is fine — near the open dev ~ 0 so z
    # is small and no entry fires anyway.
    std_rth = dev_rth.rolling(std_lookback,
                              min_periods=max(2, std_lookback // 2)).std()
    z_rth = dev_rth / std_rth.replace(0, np.nan)

    vwap = vwap_rth.reindex(df.index)
    z = z_rth.reindex(df.index)
    return vwap, z


# ---------------------------------------------------------------------------
# Trade journal (its own schema — mean-reversion context, not RSI-band context)
# ---------------------------------------------------------------------------

class TradeLog:
    """SQLite journal of every order with the mean-reversion signal context."""

    COLUMNS = ("ts", "symbol", "side", "price", "notional", "qty", "order_id",
               "vwap", "zscore", "rsi2", "timeframe", "entry_z", "exit_z",
               "stop_z", "reason", "paper")

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
                vwap REAL,                 -- session VWAP on the signal bar
                zscore REAL,               -- deviation z-score on the signal bar
                rsi2 REAL,                 -- short RSI on the signal bar
                timeframe TEXT,
                entry_z REAL, exit_z REAL, stop_z REAL,
                reason TEXT,               -- exit reason: revert/stop/close
                paper INTEGER
            )""")
        self.conn.commit()

    def record(self, **f) -> None:
        f["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            f"INSERT INTO trades ({','.join(self.COLUMNS)}) "
            f"VALUES ({','.join('?' * len(self.COLUMNS))})",
            tuple(f.get(c) for c in self.COLUMNS),
        )
        self.conn.commit()

    def recent(self, limit: int = 20) -> list[tuple]:
        return self.conn.execute(
            f"SELECT {','.join(self.COLUMNS)} FROM trades "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()[::-1]


def _default_db_path() -> str:
    return os.environ.get("MR_TRADES_DB") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "mr_trades.db")


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class MeanReversionBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.trading = TradingClient(cfg.api_key, cfg.secret_key, paper=cfg.paper)
        self.data = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
        if cfg.timeframe not in TIMEFRAMES:
            raise ValueError(f"TIMEFRAME must be one of {list(TIMEFRAMES)}")
        if TIMEFRAMES[cfg.timeframe].unit == TimeFrameUnit.Day:
            raise ValueError("mean_reversion_bot is intraday-only; TIMEFRAME=1Day "
                             "has no session VWAP. Use 1Min/5Min/15Min/1Hour.")
        if cfg.bar_adjustment not in [a.value for a in Adjustment]:
            raise ValueError(
                f"BAR_ADJUSTMENT must be one of {[a.value for a in Adjustment]}")
        if cfg.data_feed and cfg.data_feed not in [f.value for f in DataFeed]:
            raise ValueError(
                f"DATA_FEED must be one of {[f.value for f in DataFeed]} or unset")
        self.timeframe = TIMEFRAMES[cfg.timeframe]
        self.journal = TradeLog(_default_db_path())

    # -- data ---------------------------------------------------------------

    def fetch_bars(self, symbols: list[str], days: int) -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=self.timeframe,
            start=datetime.now(timezone.utc) - timedelta(days=days),
            adjustment=Adjustment(self.cfg.bar_adjustment),
            # None lets the server pick the subscription default (SIP on Algo
            # Trader Plus); DATA_FEED=iex reproduces free-tier data.
            feed=DataFeed(self.cfg.data_feed) if self.cfg.data_feed else None,
        )
        return self.data.get_stock_bars(req).df

    def bar_len(self) -> timedelta:
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
        last_start = df.index[-1]
        if datetime.now(timezone.utc) < last_start + self.bar_len():
            df = df.iloc[:-1]
        return df

    # -- signal source (single truth for both simulators AND live) ----------

    def _actionable(self, index: pd.DatetimeIndex) -> pd.Series:
        return rth_actionable(index, self.bar_len(), self.cfg.rth_only)

    def _session_last_actionable(self, index: pd.DatetimeIndex,
                                 actionable: pd.Series) -> pd.Series:
        return session_last_actionable(index, self.bar_len(), actionable)

    def entry_exit_signals(
        self,
        df: pd.DataFrame,
        entry_z: float,
        exit_z: float,
        stop_z: float,
        rsi_oversold: float,
    ) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        """Boolean (enter, exit_level, force_exit, actionable) Series for one
        symbol. Single source of signal truth for both simulators and live.

        enter       — statistically extreme dip below VWAP with RSI oversold,
                      on a live-actionable bar (never on a forced-exit bar).
        exit_level  — reversion target hit (z >= exit_z) or z-stop (z <= -stop_z).
        force_exit  — the session-close flat (last actionable bar of the day)
                      when EXIT_AT_CLOSE is on.
        actionable  — the live-actionable RTH mask (exposed for the state loop).
        The STOP_PCT stop is path-dependent on the entry price, so it lives in
        the position state loop, not here.
        """
        cfg = self.cfg
        vwap, z = session_vwap_z(df, self.bar_len(), cfg.std_lookback)
        r = rsi(df["close"], cfg.rsi_period)
        actionable = self._actionable(df.index)

        enter = (z <= -entry_z) & (r <= rsi_oversold) & actionable
        if cfg.regime_ma:
            # Daily-trend gate, resampled up and aligned no-look-ahead (reused
            # from the RSI bot's HTF machinery, rule "1D").
            enter &= htf_trend_ok(df, "1D", cfg.regime_ma, self.bar_len())

        exit_level = (z >= exit_z)
        if stop_z:
            exit_level = exit_level | (z <= -stop_z)
        exit_level &= actionable

        if cfg.exit_at_close:
            force_exit = self._session_last_actionable(df.index, actionable)
        else:
            force_exit = pd.Series(False, index=df.index)
        # Don't open a position on the very bar we'd be forced to close.
        enter &= ~force_exit
        return (enter.fillna(False), exit_level.fillna(False),
                force_exit, actionable)

    def _indicators(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
        """(vwap, z, rsi2) — for live journaling / logging."""
        vwap, z = session_vwap_z(df, self.bar_len(), self.cfg.std_lookback)
        return vwap, z, rsi(df["close"], self.cfg.rsi_period)

    # -- position state machine (shared logic, mirrors simulate()) ----------

    def _walk_positions(self, close: np.ndarray, enter: np.ndarray,
                        exit_level: np.ndarray, force: np.ndarray) -> np.ndarray:
        """Build the in-market boolean array from the per-bar signals, applying
        the path-dependent STOP_PCT. Same convention as rsi_midline_bot's
        simulate(): entered at the signal bar's close (in_market marks the bar
        AFTER entry), exited at the exit signal bar's close."""
        stop_pct = self.cfg.stop_pct
        n = len(close)
        in_arr = np.zeros(n, dtype=bool)
        in_pos, entry_px = False, 0.0
        for i in range(n):
            if in_pos:
                in_arr[i] = True
                do_exit = exit_level[i] or force[i]
                if not do_exit and stop_pct and close[i] <= entry_px * (1 - stop_pct / 100):
                    do_exit = True
                if do_exit:
                    in_pos = False
            if not in_pos and enter[i] and not in_arr[i]:
                in_pos, entry_px = True, close[i]
        return in_arr

    # -- backtest simulators ------------------------------------------------

    def simulate(
        self,
        df: pd.DataFrame,
        entry_z: float,
        exit_z: float,
        stop_z: float,
        rsi_oversold: float,
        eval_from: int = 0,
        eval_to: int | None = None,
    ) -> tuple[int, float, float]:
        """Simulate one variant; returns (trades, win rate %, total return %).

        eval_from/eval_to restrict *scoring* to a bar range while indicators
        warm on the full history (walk-forward splits). COST_BPS friction is
        charged per round trip. Fills at the signal bar's close.
        """
        closes = df["close"]
        enter, exit_level, force, _ = self.entry_exit_signals(
            df, entry_z, exit_z, stop_z, rsi_oversold)
        in_arr = self._walk_positions(
            closes.to_numpy(), enter.to_numpy(), exit_level.to_numpy(),
            force.to_numpy())
        in_market = pd.Series(in_arr, index=closes.index)
        bar_returns = closes.pct_change().fillna(0)
        if eval_from or eval_to is not None:
            in_market = in_market.iloc[eval_from:eval_to]
            bar_returns = bar_returns.iloc[eval_from:eval_to]
        grp = (in_market != in_market.shift()).cumsum()
        trade_returns = (1 + bar_returns[in_market]).groupby(grp[in_market]).prod() - 1
        trades = len(trade_returns)
        c = self.cfg.cost_bps / 10000
        cost_f = (1 - c) / (1 + c) if c else 1.0
        trade_returns = (1 + trade_returns) * cost_f - 1
        win_rate = float((trade_returns > 0).mean() * 100) if trades else 0.0
        total = ((1 + bar_returns * in_market).prod() * cost_f ** trades - 1) * 100
        return trades, win_rate, total

    def simulate_portfolio(
        self,
        dfs: dict[str, pd.DataFrame],
        entry_z: float,
        exit_z: float,
        stop_z: float,
        rsi_oversold: float,
    ) -> dict:
        """Simulate every symbol on ONE shared account, sized like live trading
        (min(NOTIONAL_PCT% of equity, cash), fractional shares, exits before
        entries each bar). Reports equity curve, max drawdown, exposure. Same
        signal source as simulate()."""
        cfg = self.cfg
        c = cfg.cost_bps / 10000
        stop_pct = cfg.stop_pct
        closes = pd.DataFrame(
            {s: df["close"] for s, df in dfs.items()}).sort_index()
        syms = list(closes.columns)
        sig = {s: self.entry_exit_signals(dfs[s], entry_z, exit_z, stop_z,
                                          rsi_oversold) for s in syms}
        idx = closes.index
        enters = np.column_stack(
            [sig[s][0].reindex(idx, fill_value=False).to_numpy() for s in syms])
        exits = np.column_stack(
            [sig[s][1].reindex(idx, fill_value=False).to_numpy() for s in syms])
        forces = np.column_stack(
            [sig[s][2].reindex(idx, fill_value=False).to_numpy() for s in syms])
        has_bar = closes.notna().to_numpy()
        px = closes.ffill().to_numpy()

        nsym = len(syms)
        qty = np.zeros(nsym)
        entry_basis = np.zeros(nsym)   # cost basis incl. entry friction
        entry_raw = np.zeros(nsym)     # raw entry price, for the STOP_PCT stop
        cash = cfg.start_equity
        equity_curve = np.empty(len(idx))
        invested_frac = np.empty(len(idx))
        trade_returns: list[float] = []
        trades_per_sym = {s: 0 for s in syms}
        for i in range(len(idx)):
            exited = np.zeros(nsym, dtype=bool)
            for j in range(nsym):
                if qty[j] and has_bar[i, j]:
                    p = px[i, j]
                    do_exit = exits[i, j] or forces[i, j] or (
                        stop_pct and p <= entry_raw[j] * (1 - stop_pct / 100))
                    if do_exit:
                        cash += qty[j] * p * (1 - c)
                        trade_returns.append(p * (1 - c) / entry_basis[j] - 1)
                        trades_per_sym[syms[j]] += 1
                        qty[j] = 0.0
                        exited[j] = True
            equity = cash + float(np.nansum(qty * px[i]))
            for j in range(nsym):
                if qty[j] or not enters[i, j] or exited[j] or cash <= 1e-9:
                    continue
                notional = (min(equity * cfg.notional_pct / 100, cash)
                            if cfg.notional_pct else min(cfg.notional, cash))
                p = px[i, j]
                qty[j] = notional / (p * (1 + c))
                entry_basis[j] = p * (1 + c)
                entry_raw[j] = p
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

    # -- trading ------------------------------------------------------------

    def position_qty(self, symbol: str) -> float:
        for pos in self.trading.get_all_positions():
            if pos.symbol == symbol:
                return float(pos.qty)
        return 0.0

    def position_notional(self) -> float:
        """Dollars for a new position: NOTIONAL_PCT% of current equity capped
        by cash so entries never lean on margin, else flat NOTIONAL."""
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

    def _log_trade(self, symbol: str, side: str, ctx: dict, **extra) -> None:
        self.journal.record(
            symbol=symbol, side=side, price=ctx.get("price"),
            vwap=ctx.get("vwap"), zscore=ctx.get("z"), rsi2=ctx.get("rsi2"),
            timeframe=self.cfg.timeframe, entry_z=self.cfg.entry_z,
            exit_z=self.cfg.exit_z, stop_z=self.cfg.stop_z,
            reason=ctx.get("reason"), paper=int(self.cfg.paper), **extra)

    def enter(self, symbol: str, ctx: dict) -> None:
        if self.position_qty(symbol) > 0:
            log.info("%s: buy signal but already long, skipping", symbol)
            return
        notional = self.position_notional()
        if notional < 1:
            log.warning("%s: buy skipped — position size $%.2f below $1 minimum",
                        symbol, notional)
            return
        order = self.trading.submit_order(MarketOrderRequest(
            symbol=symbol, notional=notional, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY))
        self._log_trade(symbol, "buy", ctx, notional=notional,
                        order_id=str(order.id))
        log.info("%s: BUY $%.2f submitted (z=%.2f rsi=%.1f, order %s)",
                 symbol, notional, ctx.get("z", float("nan")),
                 ctx.get("rsi2", float("nan")), order.id)

    def exit(self, symbol: str, ctx: dict) -> None:
        qty = self.position_qty(symbol)
        if qty <= 0:
            log.info("%s: sell signal but no position, skipping", symbol)
            return
        order = self.trading.close_position(symbol)
        self._log_trade(symbol, "sell", ctx, qty=qty, order_id=str(order.id))
        log.info("%s: SELL — position closed (%.4f shares, %s)",
                 symbol, qty, ctx.get("reason", "signal"))

    # -- live passes --------------------------------------------------------

    def _live_fetch_days(self) -> int:
        """History depth a live pass fetches: enough to warm the RSI, the z-std
        rolling window (both intraday), and — if the regime gate is on — the
        daily MA (needs many calendar days of intraday bars)."""
        days = 20
        if self.cfg.regime_ma:
            # A daily MA needs ~regime_ma trading days of intraday history.
            days = max(days, int(self.cfg.regime_ma * 1.6) + 10)
        return days

    def run_once(self) -> None:
        clock = self.trading.get_clock()
        if not clock.is_open:
            log.info("Market closed (next open %s), skipping pass", clock.next_open)
            return
        bars = self.fetch_bars(self.cfg.symbols, days=self._live_fetch_days())
        for symbol in self.cfg.symbols:
            symbol = symbol.strip().upper()
            try:
                self._eval_symbol(symbol, bars)
            except Exception:
                log.exception("%s: pass failed; continuing with next symbol",
                              symbol)

    def _eval_symbol(self, symbol: str, bars: pd.DataFrame) -> None:
        df = self.closed_bars(symbol, bars)
        if df is None or len(df) < max(self.cfg.rsi_period, self.cfg.std_lookback) + 2:
            log.warning("%s: not enough data, skipping", symbol)
            return
        enter, exit_level, force, actionable = self.entry_exit_signals(
            df, self.cfg.entry_z, self.cfg.exit_z, self.cfg.stop_z,
            self.cfg.rsi_oversold)
        vwap, z, r = self._indicators(df)
        held = self.position_qty(symbol) > 0
        zc = float(z.iloc[-1]) if not pd.isna(z.iloc[-1]) else float("nan")
        ctx = {"price": float(df["close"].iloc[-1]),
               "vwap": float(vwap.iloc[-1]) if not pd.isna(vwap.iloc[-1]) else None,
               "z": zc, "rsi2": float(r.iloc[-1]) if not pd.isna(r.iloc[-1]) else None}
        log.info("%s: close=%.2f vwap=%s z=%.2f rsi=%s held=%s",
                 symbol, ctx["price"],
                 f"{ctx['vwap']:.2f}" if ctx["vwap"] else "n/a", zc,
                 f"{ctx['rsi2']:.1f}" if ctx["rsi2"] is not None else "n/a", held)
        if held:
            reason = ("revert" if exit_level.iloc[-1] and z.iloc[-1] >= self.cfg.exit_z
                      else "stop" if exit_level.iloc[-1]
                      else "close" if force.iloc[-1] else None)
            stop_pct_hit = self._live_stop_pct_hit(symbol, ctx["price"])
            if exit_level.iloc[-1] or force.iloc[-1] or stop_pct_hit:
                ctx["reason"] = reason or "stop"
                self.exit(symbol, ctx)
        elif enter.iloc[-1]:
            self.enter(symbol, ctx)

    def _live_stop_pct_hit(self, symbol: str, price: float) -> bool:
        """STOP_PCT check for the live path: compare the current price to the
        position's average entry price from Alpaca (the loop's entry_px)."""
        if not self.cfg.stop_pct:
            return False
        for pos in self.trading.get_all_positions():
            if pos.symbol == symbol:
                return price <= float(pos.avg_entry_price) * (1 - self.cfg.stop_pct / 100)
        return False

    def run_loop(self) -> None:
        log.info(
            "Starting loop: symbols=%s timeframe=%s RSI(%d)<=%.0f entry_z=%.1f "
            "exit_z=%.1f stop_z=%.1f exit_at_close=%s paper=%s",
            self.cfg.symbols, self.cfg.timeframe, self.cfg.rsi_period,
            self.cfg.rsi_oversold, self.cfg.entry_z, self.cfg.exit_z,
            self.cfg.stop_z, self.cfg.exit_at_close, self.cfg.paper)
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Pass failed; retrying next cycle")
            time.sleep(self.cfg.poll_seconds)

    # -- backtest / portfolio reporting -------------------------------------

    def _log_backtest_results(self, rows: list[dict]) -> None:
        """Append every variant result to mr_backtest_results.csv — this bot's
        own append-only experiment log (separate from the RSI bot's)."""
        if not rows:
            return
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mr_backtest_results.csv")
        new_file = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            if new_file:
                writer.writeheader()
            writer.writerows(rows)
        print(f"\nLogged {len(rows)} variant results to mr_backtest_results.csv")

    def _variants(self) -> list[tuple]:
        """(name, entry_z, exit_z, stop_z, rsi_oversold). First row is the
        resolved config so any candidate profile A/Bs against the standards."""
        cfg = self.cfg
        return [
            ("active settings", cfg.entry_z, cfg.exit_z, cfg.stop_z, cfg.rsi_oversold),
            ("z2.0 -> VWAP", 2.0, 0.0, 0.0, 100),
            ("z2.0 + rsi<10", 2.0, 0.0, 0.0, 10),
            ("z2.0 + rsi<5", 2.0, 0.0, 0.0, 5),
            ("z2.5 + rsi<10", 2.5, 0.0, 0.0, 10),
            ("z1.5 + rsi<15", 1.5, 0.0, 0.0, 15),
            ("z2.0 rsi<10 stop3.5", 2.0, 0.0, 3.5, 10),
            ("z2.0 rsi<10 exit-0.5", 2.0, -0.5, 0.0, 10),
        ]

    def backtest(self) -> None:
        cfg = self.cfg
        log.info("Fetching %d days of %s bars for %s — this can take a minute...",
                 cfg.backtest_days, cfg.timeframe, ",".join(cfg.symbols))
        bars = self.fetch_bars(cfg.symbols, days=cfg.backtest_days)
        log.info("Fetched %d bars", len(bars))
        variants = self._variants()
        split_frac = float(os.environ.get("SPLIT_FRAC", "0.7"))

        print(f"\nBacktest: session-VWAP mean reversion, RSI({cfg.rsi_period}), "
              f"timeframe={cfg.timeframe}, last {cfg.backtest_days} days")
        print(f"'active settings' = resolved config (env > .env > profile): "
              f"entry_z {cfg.entry_z:g}, exit_z {cfg.exit_z:g}, stop_z {cfg.stop_z:g}"
              + (f", stop {cfg.stop_pct:g}%" if cfg.stop_pct else "")
              + f", rsi<= {cfg.rsi_oversold:g}, std {cfg.std_lookback}"
              + (f", regime MA{cfg.regime_ma}(1D)" if cfg.regime_ma else "")
              + f", exit_at_close={cfg.exit_at_close}")
        suffix = ""
        if not cfg.rth_only:
            print("GLOBAL: RTH_ONLY=false — acting on extended-hours bars live "
                  "never sees")
            suffix += " [all-hours]"
        if not cfg.exit_at_close:
            suffix += " [hold-overnight]"
        if cfg.regime_ma:
            suffix += f" [regimeMA{cfg.regime_ma}]"

        results = []
        run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for symbol in cfg.symbols:
            symbol = symbol.strip().upper()
            try:
                df = bars.xs(symbol, level="symbol")
            except KeyError:
                print(f"\n{symbol}: no data")
                continue
            closes = df["close"]
            hold = (closes.iloc[-1] / closes.iloc[0] - 1) * 100
            split = int(len(df) * split_frac)
            print(f"\n{symbol} — buy & hold: {hold:+.1f}%  "
                  f"(train/test split at bar {split}/{len(df)})")
            header = (f"  {'variant':<24}{'trades':>7}{'win %':>8}{'full %':>9}"
                      f"{'train %':>9}{'test %':>9}")
            print(header)
            print("  " + "-" * (len(header) - 2))
            for name, ez, xz, sz, ro in variants:
                name += suffix
                trades, win, full = self.simulate(df, ez, xz, sz, ro)
                _, _, train = self.simulate(df, ez, xz, sz, ro, eval_to=split)
                t_tr, _, test = self.simulate(df, ez, xz, sz, ro, eval_from=split)
                print(f"  {name:<24}{trades:>7}{win:>7.1f}%{full:>+8.1f}%"
                      f"{train:>+8.1f}%{test:>+8.1f}%")
                results.append({
                    "run_ts": run_ts, "timeframe": cfg.timeframe,
                    "days": cfg.backtest_days, "rsi_period": cfg.rsi_period,
                    "symbol": symbol, "variant": name, "entry_z": ez,
                    "exit_z": xz, "stop_z": sz, "stop_pct": cfg.stop_pct,
                    "rsi_oversold": ro, "std_lookback": cfg.std_lookback,
                    "regime_ma": cfg.regime_ma, "cost_bps": cfg.cost_bps,
                    "trades": trades, "win_rate": round(win, 2),
                    "return_pct": round(full, 2), "train_pct": round(train, 2),
                    "test_pct": round(test, 2), "test_trades": t_tr,
                    "buyhold_pct": round(hold, 2),
                })
        self._log_backtest_results(results)
        print(f"\nNote: fills at signal-bar close; friction COST_BPS="
              f"{cfg.cost_bps:g} bps/side; bars adjustment='{cfg.bar_adjustment}'; "
              f"signals restricted to live-actionable RTH bars"
              f"={cfg.rth_only}. Each symbol simulated in isolation, 100% "
              f"invested — see `portfolio` for shared-account sizing.\n")

    def portfolio(self) -> None:
        cfg = self.cfg
        log.info("Fetching %d days of %s bars for %s — this can take a minute...",
                 cfg.backtest_days, cfg.timeframe, ",".join(cfg.symbols))
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
        res = self.simulate_portfolio(
            dfs, cfg.entry_z, cfg.exit_z, cfg.stop_z, cfg.rsi_oversold)

        c = cfg.cost_bps / 10000
        closes = pd.DataFrame(
            {s: df["close"] for s, df in dfs.items()}).sort_index().ffill().dropna()
        shares = (cfg.start_equity / len(closes.columns)) / (closes.iloc[0] * (1 + c))
        bh = (closes * shares).sum(axis=1)
        bh_ret = (bh.iloc[-1] / cfg.start_equity - 1) * 100
        bh_dd = float((bh / bh.cummax() - 1).min() * 100)
        span = max((bh.index[-1] - bh.index[0]).days, 1)
        bh_cagr = ((bh.iloc[-1] / cfg.start_equity) ** (365.25 / span) - 1) * 100

        sizing = (f"{cfg.notional_pct:g}% of equity"
                  if cfg.notional_pct else f"flat ${cfg.notional:g}")
        span_years = span / 365.25
        tpd = res["trades"] / max(span, 1)  # trades/day across the whole account
        print(f"\nPortfolio backtest: {cfg.timeframe}, last {cfg.backtest_days} "
              f"days, {', '.join(dfs)}\nstart ${cfg.start_equity:g}, entries "
              f"{sizing} (cash-capped), COST_BPS={cfg.cost_bps:g}/side, "
              f"adjustment='{cfg.bar_adjustment}'")
        print(f"entry_z {cfg.entry_z:g}, exit_z {cfg.exit_z:g}, stop_z {cfg.stop_z:g}"
              + (f", stop {cfg.stop_pct:g}%" if cfg.stop_pct else "")
              + f", rsi<= {cfg.rsi_oversold:g}, std {cfg.std_lookback}"
              + (f", regime MA{cfg.regime_ma}" if cfg.regime_ma else "")
              + f", exit_at_close={cfg.exit_at_close}")
        print(f"\n  {'':<14}{'return':>9}{'CAGR':>8}{'max DD':>8}")
        print(f"  {'strategy':<14}{res['return_pct']:>+8.1f}%"
              f"{res['cagr_pct']:>+7.1f}%{res['max_dd_pct']:>7.1f}%")
        print(f"  {'buy & hold':<14}{bh_ret:>+8.1f}%{bh_cagr:>+7.1f}%{bh_dd:>7.1f}%")
        per_sym = ", ".join(f"{s} {n}" for s, n in res["trades_per_sym"].items())
        print(f"\n  final equity ${res['final_equity']:,.0f}, avg exposure "
              f"{res['exposure_pct']:.0f}%, {res['trades']} closed trades "
              f"(win rate {res['win_rate']:.1f}%, ~{tpd:.1f}/day): {per_sym}")
        if res["open_positions"]:
            print(f"  still open at window end: {', '.join(res['open_positions'])}")
        run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._log_backtest_results([{
            "run_ts": run_ts, "timeframe": cfg.timeframe,
            "days": cfg.backtest_days, "rsi_period": cfg.rsi_period,
            "symbol": "PORTFOLIO",
            "variant": f"portfolio {sizing}, {cfg.cost_bps:g}bps{suffix_tag(cfg)}",
            "entry_z": cfg.entry_z, "exit_z": cfg.exit_z, "stop_z": cfg.stop_z,
            "stop_pct": cfg.stop_pct, "rsi_oversold": cfg.rsi_oversold,
            "std_lookback": cfg.std_lookback, "regime_ma": cfg.regime_ma,
            "cost_bps": cfg.cost_bps, "trades": res["trades"],
            "win_rate": round(res["win_rate"], 2),
            "return_pct": round(res["return_pct"], 2),
            "train_pct": "", "test_pct": "", "test_trades": "",
            "buyhold_pct": round(bh_ret, 2),
        }])
        print(f"  (max drawdown strategy {res['max_dd_pct']:.1f}% vs buy & hold "
              f"{bh_dd:.1f}%, over {span_years:.1f}y)\n")

    def show_trades(self, limit: int = 20) -> None:
        rows = self.journal.recent(limit)
        if not rows:
            print("No trades logged yet.")
            return
        header = (f"{'time (UTC)':<21}{'symbol':<7}{'side':<6}{'price':>9}"
                  f"{'amount':>12}{'z':>7}{'rsi':>6}{'reason':>8}{'paper':>7}")
        print(header)
        print("-" * len(header))
        for row in rows:
            r = dict(zip(TradeLog.COLUMNS, row))
            amount = (f"${r['notional']:,.0f}" if r["side"] == "buy"
                      else f"{r['qty']:.4f} sh")
            print(f"{r['ts'][:19]:<21}{r['symbol']:<7}{r['side'].upper():<6}"
                  f"{(r['price'] or 0):>9.2f}{amount:>12}"
                  f"{(r['zscore'] if r['zscore'] is not None else float('nan')):>7.2f}"
                  f"{(r['rsi2'] if r['rsi2'] is not None else float('nan')):>6.1f}"
                  f"{(r['reason'] or ''):>8}{'yes' if r['paper'] else 'NO':>7}")


def suffix_tag(cfg: Config) -> str:
    tag = ""
    if not cfg.rth_only:
        tag += " [all-hours]"
    if not cfg.exit_at_close:
        tag += " [hold-overnight]"
    if cfg.regime_ma:
        tag += f" [regimeMA{cfg.regime_ma}]"
    return tag


# ---------------------------------------------------------------------------

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode not in ("run", "loop", "backtest", "portfolio", "trades", "pnl"):
        print(__doc__)
        sys.exit(1)
    load_dotenv()
    if mode == "pnl":  # journal analysis only — needs no API keys
        show_pnl(sys.argv[2:] or [_default_db_path()])
        return
    if not os.environ.get("ALPACA_API_KEY") or not os.environ.get("ALPACA_SECRET_KEY"):
        sys.exit(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.\n"
            "Add them to rsi-midline-bot/.env (see .env.example) or export them.")
    apply_profile(os.environ.get("TIMEFRAME", "5Min"))
    cfg = Config()
    bot = MeanReversionBot(cfg)
    if not cfg.paper and mode in ("run", "loop"):
        log.warning("LIVE TRADING MODE — real money at risk")
    if mode == "run":
        bot.run_once()
    elif mode == "loop":
        bot.run_loop()
    elif mode == "trades":
        bot.show_trades(int(sys.argv[2]) if len(sys.argv) > 2 else 20)
    elif mode == "portfolio":
        bot.portfolio()
    else:
        bot.backtest()


if __name__ == "__main__":
    main()
