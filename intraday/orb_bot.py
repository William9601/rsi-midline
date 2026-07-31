"""Opening Range Breakout (ORB) trading bot for Alpaca.

The second intraday experiment (after the mean-reversion bot came up empty).
Where mean reversion fades extremes and momentum-on-a-continuous-cross whipsaws,
ORB is *time-anchored* momentum: it fires at most once per day off a fixed
reference range, so it sidesteps the all-day whipsaw that sinks the other two.

Strategy (long-only, intraday, flat overnight):
    - Each session, define the OPENING RANGE from the first OR_MINUTES of
      regular trading hours (its high and low).
    - After the range closes, BUY when a bar closes above the opening-range
      high (a breakout), at most once per session.
    - Exit at a STOP (opening-range low by default, or a % / range-multiple
      stop), an optional profit TARGET (a multiple of the range), OR the
      session close (EXIT_AT_CLOSE — no overnight risk).
    - Optional regime gate (REGIME_MA): only take breakouts while the daily
      trend is up.

Signals are evaluated on completed bars only and restricted to the bars live
could actually act on. Reuses the shared intraday primitives (one RTH-actionable
definition across all sub-daily bots) and rsi_midline_bot's pure helpers.

Fills are modeled at the signal bar's CLOSE (entry when a bar closes above the
range; stop/target evaluated on closes) to match the repo's established
close-based simulation semantics — the same convention rsi_midline_bot uses for
its trailing stop. Real intrabar stop fills differ; treat backtest stops as
optimistic on gap-through bars.

Usage:
    python orb_bot.py run        # evaluate signals once and trade
    python orb_bot.py loop       # run continuously on an interval
    python orb_bot.py backtest   # backtest variants on history
    python orb_bot.py portfolio  # one shared account, live sizing
    python orb_bot.py trades     # show the trade journal (SQLite)
    python orb_bot.py pnl [db..] # round-trip P&L from the journal(s)
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

# This module lives in intraday/; add the repo root to the path so the shared
# rsi_midline_bot helpers still import when it's run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rsi_midline_bot import TIMEFRAMES, htf_trend_ok, load_dotenv, show_pnl
from intraday_common import (RTH_CLOSE_MIN, RTH_OPEN_MIN, rth_actionable,
                             session_ids, session_last_actionable)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("orb")


def apply_profile(timeframe: str) -> None:
    """Fill in tuned per-timeframe defaults from orb_profiles.json (this bot's
    own profiles; env/.env still win over them)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "orb_profiles.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        profile = json.load(f).get(timeframe)
    if not profile:
        log.warning("No profile for timeframe %s in orb_profiles.json", timeframe)
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
    # Opening range length, minutes from the 9:30 ET open (must be >= one bar).
    or_minutes: int = field(default_factory=lambda: int(os.environ.get("OR_MINUTES", "15")))
    # Stop placement: 'or_low' (opening-range low), 'pct' (STOP_PCT below entry),
    # or 'range' (entry - STOP_R * range height).
    stop_mode: str = field(default_factory=lambda: os.environ.get("STOP_MODE", "or_low"))
    stop_pct: float = field(default_factory=lambda: float(os.environ.get("STOP_PCT", "0")))
    stop_r: float = field(default_factory=lambda: float(os.environ.get("STOP_R", "1.0")))
    # Profit target as a multiple of the range height (0 = ride to the close).
    target_r: float = field(default_factory=lambda: float(os.environ.get("TARGET_R", "0")))
    # Alternative fixed-% profit target (0 = off). target_r takes precedence.
    target_pct: float = field(default_factory=lambda: float(os.environ.get("TARGET_PCT", "0")))
    # Don't enter a breakout after this ET minute-of-day (0 = no cutoff).
    entry_cutoff_min: int = field(
        default_factory=lambda: int(os.environ.get("ENTRY_CUTOFF_MIN", "0")))
    # At most one breakout trade per session (classic ORB).
    one_trade_per_day: bool = field(
        default_factory=lambda: os.environ.get("ONE_TRADE_PER_DAY", "true").lower() != "false")
    # Regime gate: only take breakouts when the daily close is above this daily
    # MA (0 = off). Aligned no-look-ahead.
    regime_ma: int = field(default_factory=lambda: int(os.environ.get("REGIME_MA", "0")))
    # Force flat near the session close (no overnight risk).
    exit_at_close: bool = field(
        default_factory=lambda: os.environ.get("EXIT_AT_CLOSE", "true").lower() != "false")
    # -- sizing / friction / data (same names/semantics as the other bots) --
    notional: float = field(default_factory=lambda: float(os.environ.get("NOTIONAL", "1000")))
    notional_pct: float = field(default_factory=lambda: float(os.environ.get("NOTIONAL_PCT", "0")))
    poll_seconds: int = field(default_factory=lambda: int(os.environ.get("POLL_SECONDS", "60")))
    backtest_days: int = field(default_factory=lambda: int(os.environ.get("BACKTEST_DAYS", "365")))
    cost_bps: float = field(default_factory=lambda: float(os.environ.get("COST_BPS", "0")))
    bar_adjustment: str = field(default_factory=lambda: os.environ.get("BAR_ADJUSTMENT", "all"))
    data_feed: str = field(default_factory=lambda: os.environ.get("DATA_FEED", ""))
    rth_only: bool = field(
        default_factory=lambda: os.environ.get("RTH_ONLY", "true").lower() != "false")
    start_equity: float = field(
        default_factory=lambda: float(os.environ.get("START_EQUITY", "2000")))


# ---------------------------------------------------------------------------
# Opening-range levels
# ---------------------------------------------------------------------------

def opening_range(df: pd.DataFrame, bar_len: timedelta, or_minutes: int
                  ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Per-bar opening-range high/low and an `after_or` mask.

    The opening range is the high/low over the RTH bars that fall within the
    first `or_minutes` of the session (bars ending in (9:30, 9:30+or_minutes]).
    or_high/or_low are broadcast to every bar of the session, but only USED on
    bars after the range has fully closed (`after_or`), so acting on them never
    looks ahead: by then the range is complete and lies entirely in the past.
    """
    ends = (df.index + bar_len).tz_convert("America/New_York")
    mins = np.asarray(ends.hour * 60 + ends.minute)
    session = pd.Series(np.asarray(ends.normalize().view("int64")), index=df.index)
    in_or = (mins > RTH_OPEN_MIN) & (mins <= RTH_OPEN_MIN + or_minutes)
    after_or = pd.Series((mins > RTH_OPEN_MIN + or_minutes) & (mins <= RTH_CLOSE_MIN),
                         index=df.index)
    hi = df["high"].where(pd.Series(in_or, index=df.index))
    lo = df["low"].where(pd.Series(in_or, index=df.index))
    or_high = hi.groupby(session).transform("max")
    or_low = lo.groupby(session).transform("min")
    return or_high, or_low, after_or


# ---------------------------------------------------------------------------
# Trade journal (ORB context)
# ---------------------------------------------------------------------------

class TradeLog:
    COLUMNS = ("ts", "symbol", "side", "price", "notional", "qty", "order_id",
               "or_high", "or_low", "stop", "target", "timeframe", "or_minutes",
               "reason", "paper")

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL,
                notional REAL,
                qty REAL,
                order_id TEXT,
                or_high REAL, or_low REAL, stop REAL, target REAL,
                timeframe TEXT, or_minutes INTEGER,
                reason TEXT,               -- exit reason: stop/target/close
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
    return os.environ.get("ORB_TRADES_DB") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "orb_trades.db")


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class OrbBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.trading = TradingClient(cfg.api_key, cfg.secret_key, paper=cfg.paper)
        self.data = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
        if cfg.timeframe not in TIMEFRAMES:
            raise ValueError(f"TIMEFRAME must be one of {list(TIMEFRAMES)}")
        if TIMEFRAMES[cfg.timeframe].unit == TimeFrameUnit.Day:
            raise ValueError("orb_bot is intraday-only; TIMEFRAME=1Day has no "
                             "opening range. Use 1Min/5Min/15Min/1Hour.")
        if cfg.stop_mode not in ("or_low", "pct", "range"):
            raise ValueError("STOP_MODE must be or_low, pct, or range")
        if cfg.bar_adjustment not in [a.value for a in Adjustment]:
            raise ValueError(
                f"BAR_ADJUSTMENT must be one of {[a.value for a in Adjustment]}")
        if cfg.data_feed and cfg.data_feed not in [f.value for f in DataFeed]:
            raise ValueError(
                f"DATA_FEED must be one of {[f.value for f in DataFeed]} or unset")
        self.timeframe = TIMEFRAMES[cfg.timeframe]
        bar_min = self._bar_minutes()
        if cfg.or_minutes < bar_min:
            raise ValueError(f"OR_MINUTES ({cfg.or_minutes}) must be >= one bar "
                             f"({bar_min} min) or the range is empty")
        self.journal = TradeLog(_default_db_path())

    # -- data ---------------------------------------------------------------

    def fetch_bars(self, symbols: list[str], days: int) -> pd.DataFrame:
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=self.timeframe,
            start=datetime.now(timezone.utc) - timedelta(days=days),
            adjustment=Adjustment(self.cfg.bar_adjustment),
            feed=DataFeed(self.cfg.data_feed) if self.cfg.data_feed else None,
        )
        return self.data.get_stock_bars(req).df

    def _bar_minutes(self) -> int:
        if self.timeframe.unit == TimeFrameUnit.Minute:
            return self.timeframe.amount_value
        if self.timeframe.unit == TimeFrameUnit.Hour:
            return self.timeframe.amount_value * 60
        return self.timeframe.amount_value * 60 * 24

    def bar_len(self) -> timedelta:
        return timedelta(minutes=self._bar_minutes())

    def closed_bars(self, symbol: str, bars: pd.DataFrame) -> pd.DataFrame | None:
        try:
            df = bars.xs(symbol, level="symbol")
        except KeyError:
            return None
        last_start = df.index[-1]
        if datetime.now(timezone.utc) < last_start + self.bar_len():
            df = df.iloc[:-1]
        return df

    # -- signal source (single truth for both simulators AND live) ----------

    def signals(self, df: pd.DataFrame) -> dict:
        """Per-bar arrays every consumer needs, all no-look-ahead:
        or_high/or_low, after_or (range closed), actionable (live can act),
        force_exit (session's last actionable bar), regime_ok, session ids."""
        cfg = self.cfg
        bl = self.bar_len()
        or_high, or_low, after_or = opening_range(df, bl, cfg.or_minutes)
        actionable = rth_actionable(df.index, bl, cfg.rth_only)
        force_exit = (session_last_actionable(df.index, bl, actionable)
                      if cfg.exit_at_close
                      else pd.Series(False, index=df.index))
        if cfg.regime_ma:
            regime_ok = htf_trend_ok(df, "1D", cfg.regime_ma, bl)
        else:
            regime_ok = pd.Series(True, index=df.index)
        # Optional late-breakout cutoff.
        ends = (df.index + bl).tz_convert("America/New_York")
        mins = np.asarray(ends.hour * 60 + ends.minute)
        if cfg.entry_cutoff_min:
            not_late = pd.Series(mins <= cfg.entry_cutoff_min, index=df.index)
        else:
            not_late = pd.Series(True, index=df.index)
        return {
            "or_high": or_high.to_numpy(), "or_low": or_low.to_numpy(),
            "after_or": after_or.to_numpy(),
            "actionable": actionable.to_numpy(),
            "force_exit": force_exit.to_numpy(),
            "regime_ok": regime_ok.reindex(df.index, fill_value=False).to_numpy(),
            "not_late": not_late.to_numpy(),
            "session": session_ids(df.index, bl),
            "index": df.index,
        }

    def _stop_target(self, entry: float, or_high: float, or_low: float
                     ) -> tuple[float, float | None]:
        """Stop price and (optional) target price for a fresh entry."""
        cfg = self.cfg
        rng = or_high - or_low
        if cfg.stop_mode == "or_low":
            stop = or_low
        elif cfg.stop_mode == "pct":
            stop = entry * (1 - cfg.stop_pct / 100)
        else:  # range
            stop = entry - cfg.stop_r * rng
        target = None
        if cfg.target_r:
            target = entry + cfg.target_r * rng
        elif cfg.target_pct:
            target = entry * (1 + cfg.target_pct / 100)
        return stop, target

    def _walk_positions(self, close: np.ndarray, sig: dict) -> np.ndarray:
        """Build the in-market boolean array from ORB signals for one symbol.

        Same close-based convention as rsi_midline_bot.simulate(): entered at
        the breakout bar's close (in_market marks the bar AFTER entry), exited
        at the stop/target/close bar's close. At most one entry per session
        when ONE_TRADE_PER_DAY.
        """
        cfg = self.cfg
        or_high, or_low = sig["or_high"], sig["or_low"]
        after_or, actionable = sig["after_or"], sig["actionable"]
        force, regime_ok, not_late = sig["force_exit"], sig["regime_ok"], sig["not_late"]
        session = sig["session"]
        n = len(close)
        in_arr = np.zeros(n, dtype=bool)
        in_pos = False
        cur_sess, traded = -1, False
        entry_px = stop_lvl = 0.0
        target_lvl: float | None = None
        for i in range(n):
            if session[i] != cur_sess:
                cur_sess, traded = session[i], False
            if in_pos:
                in_arr[i] = True
                do_exit = (force[i] or close[i] <= stop_lvl
                           or (target_lvl is not None and close[i] >= target_lvl))
                if do_exit:
                    in_pos = False
            if (not in_pos and not (traded and cfg.one_trade_per_day)
                    and after_or[i] and actionable[i] and not force[i]
                    and regime_ok[i] and not_late[i]
                    and not np.isnan(or_high[i]) and close[i] > or_high[i]):
                in_pos, traded = True, True
                entry_px = close[i]
                stop_lvl, target_lvl = self._stop_target(entry_px, or_high[i], or_low[i])
        return in_arr

    # -- backtest simulators ------------------------------------------------

    def simulate(self, df: pd.DataFrame, eval_from: int = 0,
                 eval_to: int | None = None) -> tuple[int, float, float]:
        """Simulate one config; returns (trades, win rate %, total return %).
        eval_from/eval_to restrict scoring while indicators warm on full
        history. COST_BPS friction per round trip; fills at signal-bar close."""
        closes = df["close"]
        in_arr = self._walk_positions(closes.to_numpy(), self.signals(df))
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

    def simulate_portfolio(self, dfs: dict[str, pd.DataFrame]) -> dict:
        """Every symbol on ONE shared account, live sizing (NOTIONAL_PCT of
        equity capped by cash, fractional shares, exits before entries each
        bar). Same signal source as simulate()."""
        cfg = self.cfg
        c = cfg.cost_bps / 10000
        closes = pd.DataFrame(
            {s: df["close"] for s, df in dfs.items()}).sort_index()
        syms = list(closes.columns)
        sig = {s: self.signals(dfs[s]) for s in syms}
        idx = closes.index

        def col(key):
            return np.column_stack([
                pd.Series(sig[s][key], index=sig[s]["index"]).reindex(
                    idx).to_numpy() for s in syms])

        or_high = col("or_high")
        or_low = col("or_low")
        after_or = col("after_or").astype(bool)
        actionable = np.nan_to_num(col("actionable"), nan=0).astype(bool)
        force = np.nan_to_num(col("force_exit"), nan=0).astype(bool)
        regime_ok = np.nan_to_num(col("regime_ok"), nan=0).astype(bool)
        not_late = np.nan_to_num(col("not_late"), nan=0).astype(bool)
        session = col("session")
        has_bar = closes.notna().to_numpy()
        px = closes.ffill().to_numpy()

        nsym = len(syms)
        qty = np.zeros(nsym)
        entry_basis = np.zeros(nsym)
        stop_lvl = np.zeros(nsym)
        target_lvl: list[float | None] = [None] * nsym
        cur_sess = np.full(nsym, -1.0)
        traded = np.zeros(nsym, dtype=bool)
        cash = cfg.start_equity
        equity_curve = np.empty(len(idx))
        invested_frac = np.empty(len(idx))
        trade_returns: list[float] = []
        trades_per_sym = {s: 0 for s in syms}
        for i in range(len(idx)):
            exited = np.zeros(nsym, dtype=bool)
            for j in range(nsym):
                if not has_bar[i, j]:
                    continue
                if session[i, j] != cur_sess[j]:
                    cur_sess[j], traded[j] = session[i, j], False
                if qty[j]:
                    p = px[i, j]
                    do_exit = (force[i, j] or p <= stop_lvl[j]
                               or (target_lvl[j] is not None and p >= target_lvl[j]))
                    if do_exit:
                        cash += qty[j] * p * (1 - c)
                        trade_returns.append(p * (1 - c) / entry_basis[j] - 1)
                        trades_per_sym[syms[j]] += 1
                        qty[j] = 0.0
                        exited[j] = True
            equity = cash + float(np.nansum(qty * px[i]))
            for j in range(nsym):
                if (qty[j] or exited[j] or not has_bar[i, j] or cash <= 1e-9
                        or (traded[j] and cfg.one_trade_per_day)
                        or not (after_or[i, j] and actionable[i, j]
                                and not force[i, j] and regime_ok[i, j]
                                and not_late[i, j])
                        or np.isnan(or_high[i, j]) or px[i, j] <= or_high[i, j]):
                    continue
                notional = (min(equity * cfg.notional_pct / 100, cash)
                            if cfg.notional_pct else min(cfg.notional, cash))
                p = px[i, j]
                qty[j] = notional / (p * (1 + c))
                entry_basis[j] = p * (1 + c)
                traded[j] = True
                stop_lvl[j], target_lvl[j] = self._stop_target(
                    p, or_high[i, j], or_low[i, j])
                cash -= notional
            pos_val = float(np.nansum(qty * px[i]))
            equity_curve[i] = cash + pos_val
            invested_frac[i] = pos_val / equity_curve[i]

        eq = pd.Series(equity_curve, index=idx)
        span_days = max((idx[-1] - idx[0]).days, 1)
        wins = sum(1 for r in trade_returns if r > 0)
        return {
            "equity": eq, "final_equity": float(eq.iloc[-1]),
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
            or_high=ctx.get("or_high"), or_low=ctx.get("or_low"),
            stop=ctx.get("stop"), target=ctx.get("target"),
            timeframe=self.cfg.timeframe, or_minutes=self.cfg.or_minutes,
            reason=ctx.get("reason"), paper=int(self.cfg.paper), **extra)

    def enter(self, symbol: str, ctx: dict) -> None:
        if self.position_qty(symbol) > 0:
            log.info("%s: breakout but already long, skipping", symbol)
            return
        notional = self.position_notional()
        if notional < 1:
            log.warning("%s: buy skipped — size $%.2f below $1 minimum", symbol, notional)
            return
        order = self.trading.submit_order(MarketOrderRequest(
            symbol=symbol, notional=notional, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY))
        self._log_trade(symbol, "buy", ctx, notional=notional, order_id=str(order.id))
        log.info("%s: BUY $%.2f breakout > ORH %.2f (order %s)",
                 symbol, notional, ctx.get("or_high", float("nan")), order.id)

    def exit(self, symbol: str, ctx: dict) -> None:
        qty = self.position_qty(symbol)
        if qty <= 0:
            log.info("%s: exit signal but no position, skipping", symbol)
            return
        order = self.trading.close_position(symbol)
        self._log_trade(symbol, "sell", ctx, qty=qty, order_id=str(order.id))
        log.info("%s: SELL — closed (%.4f sh, %s)", symbol, qty,
                 ctx.get("reason", "signal"))

    # -- live passes --------------------------------------------------------

    def _live_fetch_days(self) -> int:
        days = 20
        if self.cfg.regime_ma:
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
                log.exception("%s: pass failed; continuing", symbol)

    def _eval_symbol(self, symbol: str, bars: pd.DataFrame) -> None:
        df = self.closed_bars(symbol, bars)
        if df is None or len(df) < 3:
            log.warning("%s: not enough data, skipping", symbol)
            return
        # Re-derive today's state with the same state machine the sim uses, so
        # live and backtest make the identical decision on the latest bar.
        sig = self.signals(df)
        in_arr = self._walk_positions(df["close"].to_numpy(), sig)
        held_live = self.position_qty(symbol) > 0
        want_in = bool(in_arr[-1])
        price = float(df["close"].iloc[-1])
        oh = sig["or_high"][-1]
        ol = sig["or_low"][-1]
        ctx = {"price": price,
               "or_high": None if np.isnan(oh) else float(oh),
               "or_low": None if np.isnan(ol) else float(ol)}
        log.info("%s: close=%.2f ORH=%s ORL=%s want_in=%s held=%s", symbol, price,
                 f"{oh:.2f}" if not np.isnan(oh) else "n/a",
                 f"{ol:.2f}" if not np.isnan(ol) else "n/a", want_in, held_live)
        if want_in and not held_live:
            if ctx["or_high"] is not None:
                stop, target = self._stop_target(price, oh, ol)
                ctx["stop"], ctx["target"] = stop, target
            self.enter(symbol, ctx)
        elif held_live and not want_in:
            ctx["reason"] = ("close" if sig["force_exit"][-1] else "stop/target")
            self.exit(symbol, ctx)

    def run_loop(self) -> None:
        log.info("Starting loop: symbols=%s timeframe=%s OR=%dmin stop=%s "
                 "target_r=%g exit_at_close=%s paper=%s",
                 self.cfg.symbols, self.cfg.timeframe, self.cfg.or_minutes,
                 self.cfg.stop_mode, self.cfg.target_r, self.cfg.exit_at_close,
                 self.cfg.paper)
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
        if not rows:
            return
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "orb_backtest_results.csv")
        new_file = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            if new_file:
                writer.writeheader()
            writer.writerows(rows)
        print(f"\nLogged {len(rows)} variant results to orb_backtest_results.csv")

    def _variants(self) -> list[tuple]:
        """(name, env-overrides dict) for the standard backtest grid. Each is
        applied on top of a fresh Config so 'active settings' A/Bs cleanly."""
        return [
            ("active settings", {}),
            ("OR15 stop=ORlow ride", {"OR_MINUTES": "15", "STOP_MODE": "or_low",
                                      "TARGET_R": "0"}),
            ("OR30 stop=ORlow ride", {"OR_MINUTES": "30", "STOP_MODE": "or_low",
                                      "TARGET_R": "0"}),
            ("OR15 stop=ORlow tgt2R", {"OR_MINUTES": "15", "STOP_MODE": "or_low",
                                       "TARGET_R": "2"}),
            ("OR15 stop=0.5R ride", {"OR_MINUTES": "15", "STOP_MODE": "range",
                                     "STOP_R": "0.5", "TARGET_R": "0"}),
            ("OR5 stop=ORlow ride", {"OR_MINUTES": "5", "STOP_MODE": "or_low",
                                     "TARGET_R": "0"}),
            ("OR15 ORlow +regimeMA50", {"OR_MINUTES": "15", "STOP_MODE": "or_low",
                                        "TARGET_R": "0", "REGIME_MA": "50"}),
        ]

    def _variant_bot(self, overrides: dict) -> "OrbBot":
        """A sibling bot with the same creds/timeframe/data but a variant's
        strategy knobs applied via a temporary env overlay."""
        keys = ("OR_MINUTES", "STOP_MODE", "STOP_PCT", "STOP_R", "TARGET_R",
                "TARGET_PCT", "ENTRY_CUTOFF_MIN", "ONE_TRADE_PER_DAY",
                "REGIME_MA")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            for k in keys:
                os.environ.pop(k, None)
            os.environ.update({k: str(v) for k, v in overrides.items()})
            # Fresh Config() so it re-reads the overlaid env (reusing self.cfg
            # would keep the base config's already-resolved strategy knobs).
            return OrbBot(Config())
        finally:
            for k in keys:
                if saved[k] is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = saved[k]

    def backtest(self) -> None:
        cfg = self.cfg
        log.info("Fetching %d days of %s bars for %s — this can take a minute...",
                 cfg.backtest_days, cfg.timeframe, ",".join(cfg.symbols))
        bars = self.fetch_bars(cfg.symbols, days=cfg.backtest_days)
        log.info("Fetched %d bars", len(bars))
        variants = self._variants()
        # Build each variant's bot ONCE (reuses this bot's clients).
        vbots = [(name, self if not ov else self._variant_bot(ov))
                 for name, ov in variants]
        split_frac = float(os.environ.get("SPLIT_FRAC", "0.7"))

        print(f"\nBacktest: Opening Range Breakout, timeframe={cfg.timeframe}, "
              f"last {cfg.backtest_days} days")
        print(f"'active settings' = resolved config: OR {cfg.or_minutes}min, "
              f"stop={cfg.stop_mode}"
              + (f"({cfg.stop_pct:g}%)" if cfg.stop_mode == "pct" else
                 f"({cfg.stop_r:g}R)" if cfg.stop_mode == "range" else "")
              + (f", target {cfg.target_r:g}R" if cfg.target_r else
                 f", target {cfg.target_pct:g}%" if cfg.target_pct else ", ride to close")
              + (f", regime MA{cfg.regime_ma}" if cfg.regime_ma else "")
              + f", 1/day={cfg.one_trade_per_day}, exit_at_close={cfg.exit_at_close}")
        suffix = ""
        if not cfg.rth_only:
            print("GLOBAL: RTH_ONLY=false — extended-hours bars live never sees")
            suffix += " [all-hours]"
        if not cfg.exit_at_close:
            suffix += " [hold-overnight]"

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
            header = (f"  {'variant':<26}{'trades':>7}{'win %':>8}{'full %':>9}"
                      f"{'train %':>9}{'test %':>9}")
            print(header)
            print("  " + "-" * (len(header) - 2))
            for name, vbot in vbots:
                nm = name + suffix
                trades, win, full = vbot.simulate(df)
                _, _, train = vbot.simulate(df, eval_to=split)
                t_tr, _, test = vbot.simulate(df, eval_from=split)
                print(f"  {nm:<26}{trades:>7}{win:>7.1f}%{full:>+8.1f}%"
                      f"{train:>+8.1f}%{test:>+8.1f}%")
                results.append({
                    "run_ts": run_ts, "timeframe": cfg.timeframe,
                    "days": cfg.backtest_days, "symbol": symbol, "variant": nm,
                    "or_minutes": vbot.cfg.or_minutes, "stop_mode": vbot.cfg.stop_mode,
                    "stop_r": vbot.cfg.stop_r, "target_r": vbot.cfg.target_r,
                    "regime_ma": vbot.cfg.regime_ma, "cost_bps": cfg.cost_bps,
                    "trades": trades, "win_rate": round(win, 2),
                    "return_pct": round(full, 2), "train_pct": round(train, 2),
                    "test_pct": round(test, 2), "test_trades": t_tr,
                    "buyhold_pct": round(hold, 2),
                })
        self._log_backtest_results(results)
        print(f"\nNote: fills at signal-bar close (stops/targets evaluated on "
              f"closes — optimistic on gap-through bars); friction COST_BPS="
              f"{cfg.cost_bps:g} bps/side; adjustment='{cfg.bar_adjustment}'; "
              f"RTH-actionable only={cfg.rth_only}. Each symbol isolated, 100% "
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
        res = self.simulate_portfolio(dfs)

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
        tpd = res["trades"] / max(span, 1)
        print(f"\nPortfolio backtest: {cfg.timeframe}, last {cfg.backtest_days} "
              f"days, {', '.join(dfs)}\nstart ${cfg.start_equity:g}, entries "
              f"{sizing} (cash-capped), COST_BPS={cfg.cost_bps:g}/side, "
              f"adjustment='{cfg.bar_adjustment}'")
        print(f"OR {cfg.or_minutes}min, stop={cfg.stop_mode}, "
              f"target_r={cfg.target_r:g}"
              + (f", regime MA{cfg.regime_ma}" if cfg.regime_ma else "")
              + f", 1/day={cfg.one_trade_per_day}, exit_at_close={cfg.exit_at_close}")
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
            "days": cfg.backtest_days, "symbol": "PORTFOLIO",
            "variant": f"portfolio {sizing}, {cfg.cost_bps:g}bps",
            "or_minutes": cfg.or_minutes, "stop_mode": cfg.stop_mode,
            "stop_r": cfg.stop_r, "target_r": cfg.target_r,
            "regime_ma": cfg.regime_ma, "cost_bps": cfg.cost_bps,
            "trades": res["trades"], "win_rate": round(res["win_rate"], 2),
            "return_pct": round(res["return_pct"], 2), "train_pct": "",
            "test_pct": "", "test_trades": "", "buyhold_pct": round(bh_ret, 2),
        }])
        print(f"  (max drawdown strategy {res['max_dd_pct']:.1f}% vs buy & hold "
              f"{bh_dd:.1f}%)\n")

    def show_trades(self, limit: int = 20) -> None:
        rows = self.journal.recent(limit)
        if not rows:
            print("No trades logged yet.")
            return
        header = (f"{'time (UTC)':<21}{'symbol':<7}{'side':<6}{'price':>9}"
                  f"{'amount':>12}{'ORH':>9}{'ORL':>9}{'reason':>10}{'paper':>7}")
        print(header)
        print("-" * len(header))
        for row in rows:
            r = dict(zip(TradeLog.COLUMNS, row))
            amount = (f"${r['notional']:,.0f}" if r["side"] == "buy"
                      else f"{r['qty']:.4f} sh")
            print(f"{r['ts'][:19]:<21}{r['symbol']:<7}{r['side'].upper():<6}"
                  f"{(r['price'] or 0):>9.2f}{amount:>12}"
                  f"{(r['or_high'] or float('nan')):>9.2f}"
                  f"{(r['or_low'] or float('nan')):>9.2f}"
                  f"{(r['reason'] or ''):>10}{'yes' if r['paper'] else 'NO':>7}")


# ---------------------------------------------------------------------------

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode not in ("run", "loop", "backtest", "portfolio", "trades", "pnl"):
        print(__doc__)
        sys.exit(1)
    load_dotenv()
    if mode == "pnl":
        show_pnl(sys.argv[2:] or [_default_db_path()])
        return
    if not os.environ.get("ALPACA_API_KEY") or not os.environ.get("ALPACA_SECRET_KEY"):
        sys.exit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY. Add them to "
                 "rsi-midline-bot/.env (see .env.example) or export them.")
    apply_profile(os.environ.get("TIMEFRAME", "5Min"))
    cfg = Config()
    bot = OrbBot(cfg)
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
