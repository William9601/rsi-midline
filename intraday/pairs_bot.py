"""Intraday cross-sectional / market-neutral pairs-reversion research engine.

The fourth intraday strategy explored for this repo, and the first that is
*relative* rather than *absolute*. The RSI-trend, session-VWAP mean-reversion
(mean_reversion_bot.py) and Opening-Range-Breakout (orb_bot.py) bots all bet on
one instrument's own price and all failed on liquid ETFs — SPY/QQQ/IWM crash
together, so a single-name long-only bet is really one market bet. This bot bets
on the *spread* between two correlated legs instead, which can be closer to
market-neutral:

Strategy (market-neutral, intraday, flat overnight):
    - For a pair (A, B) form the log-spread  s = logA - beta*logB, where beta is
      a ROLLING OLS hedge ratio (Cov(logA,logB)/Var(logB) over the trailing
      BETA_LOOKBACK bars). z-score s over a rolling Z_LOOKBACK window.
    - z <= -ENTRY_Z  (A cheap vs B): LONG the spread  -> long A, short B.
    - z >= +ENTRY_Z  (A rich  vs B): SHORT the spread -> short A, long B.
    - Exit when the spread reverts (|z| <= EXIT_Z), or blows out (|z| >= STOP_Z),
      or the session ends (EXIT_AT_CLOSE — no overnight risk).

This is RESEARCH-ONLY. Per the project's non-negotiable norm ("validate OOS at
COST_BPS=5 BEFORE any deploy scaffolding") the two-sided *edge* is fully
backtestable by simulating both legs; live shorting infrastructure
(submit_order short legs, margin/borrow checks, per-leg journaling, run/loop,
replay) is deliberately NOT built until the walk-forward gate proves an edge.
`run`/`loop` are stubbed so nothing can short before that.

Imports the battle-tested pure helpers (load_dotenv, TIMEFRAMES, show_pnl) from
rsi_midline_bot and the one live-actionable RTH definition from intraday_common,
so this bot can never drift from live or from its siblings.

Usage:
    python pairs_bot.py backtest   # per-pair walk-forward variant table
    python pairs_bot.py portfolio  # one shared account, two-sided MTM, DD/exposure
    python pairs_bot.py run/loop    # DEFERRED — refuses (no validated edge yet)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
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

# Reuse the RSI bot's proven, import-safe pure helpers (none touch the network).
# This module lives in intraday/; add the repo root to the path so the shared
# rsi_midline_bot helpers still import when it's run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rsi_midline_bot import TIMEFRAMES, load_dotenv, show_pnl
# Shared intraday primitives — one live-actionable definition for every
# sub-daily bot, so they can never drift from each other or from live.
from intraday_common import (RTH_CLOSE_MIN, RTH_OPEN_MIN, rth_actionable,
                             session_last_actionable)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pairs")


def parse_pairs(spec: str) -> list[tuple[str, str]]:
    """'QQQ/SPY,IWM/SPY' -> [('QQQ','SPY'), ('IWM','SPY')]."""
    pairs = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        a, _, b = tok.partition("/")
        a, b = a.strip().upper(), b.strip().upper()
        if not a or not b:
            raise ValueError(f"Bad pair spec '{tok}'; expected 'A/B'")
        pairs.append((a, b))
    if not pairs:
        raise ValueError("PAIRS is empty")
    return pairs


def apply_profile(timeframe: str) -> None:
    """Fill tuned per-timeframe defaults from pairs_profiles.json (if present).
    Env/.env always win; a profile only supplies still-unset knobs. Mirrors the
    RSI bot's precedence but reads this bot's own profiles file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "pairs_profiles.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        profile = json.load(f).get(timeframe)
    if not profile:
        log.warning("No profile for timeframe %s in pairs_profiles.json", timeframe)
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
        default_factory=lambda: os.environ.get("ALPACA_PAPER", "true").lower() != "false")
    pairs: list = field(
        default_factory=lambda: parse_pairs(os.environ.get("PAIRS", "QQQ/SPY,IWM/SPY")))
    timeframe: str = field(default_factory=lambda: os.environ.get("TIMEFRAME", "5Min"))
    # -- strategy knobs -----------------------------------------------------
    # Rolling OLS window (bars) for the hedge ratio beta = Cov(logA,logB)/Var(logB).
    beta_lookback: int = field(
        default_factory=lambda: int(os.environ.get("BETA_LOOKBACK", "60")))
    # Rolling window (bars) for the spread's mean/std -> z-score.
    z_lookback: int = field(default_factory=lambda: int(os.environ.get("Z_LOOKBACK", "30")))
    # Enter when |z| >= ENTRY_Z (long the cheap leg / short the rich leg).
    entry_z: float = field(default_factory=lambda: float(os.environ.get("ENTRY_Z", "2.0")))
    # Exit (reversion target) when |z| <= EXIT_Z (0 = all the way to the mean).
    exit_z: float = field(default_factory=lambda: float(os.environ.get("EXIT_Z", "0.5")))
    # Stop: exit if the spread blows out to |z| >= STOP_Z (0 = off).
    stop_z: float = field(default_factory=lambda: float(os.environ.get("STOP_Z", "4.0")))
    # beta=1 static hedge (log-ratio spread) instead of the rolling OLS beta.
    static_beta: bool = field(
        default_factory=lambda: os.environ.get("STATIC_BETA", "false").lower() == "true")
    # Force flat near the session close (no overnight gap risk on the spread).
    exit_at_close: bool = field(
        default_factory=lambda: os.environ.get("EXIT_AT_CLOSE", "true").lower() != "false")
    # -- sizing / friction / data (same names/semantics as the siblings) -----
    # Per-pair GROSS exposure (|long$|+|short$|) as % of equity in the portfolio sim.
    gross_pct: float = field(default_factory=lambda: float(os.environ.get("GROSS_PCT", "25")))
    # Cap on total gross across all pairs, % of equity (margin guard).
    max_gross_pct: float = field(
        default_factory=lambda: float(os.environ.get("MAX_GROSS_PCT", "100")))
    backtest_days: int = field(default_factory=lambda: int(os.environ.get("BACKTEST_DAYS", "365")))
    cost_bps: float = field(default_factory=lambda: float(os.environ.get("COST_BPS", "0")))
    bar_adjustment: str = field(default_factory=lambda: os.environ.get("BAR_ADJUSTMENT", "all"))
    data_feed: str = field(default_factory=lambda: os.environ.get("DATA_FEED", ""))
    # Restrict entries/exits to bars live could act on. "false" only for experiments.
    rth_only: bool = field(
        default_factory=lambda: os.environ.get("RTH_ONLY", "true").lower() != "false")
    start_equity: float = field(
        default_factory=lambda: float(os.environ.get("START_EQUITY", "2000")))

    @property
    def symbols(self) -> list:
        """Union of all legs, order-preserving — what we fetch."""
        seen, out = set(), []
        for a, b in self.pairs:
            for s in (a, b):
                if s not in seen:
                    seen.add(s)
                    out.append(s)
        return out


# ---------------------------------------------------------------------------
# Spread indicators (rolling-OLS-beta hedge + z-score of the spread)
# ---------------------------------------------------------------------------

def pair_spread_z(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    bar_len: timedelta,
    beta_lookback: int,
    z_lookback: int,
    static_beta: bool = False,
) -> tuple[pd.Index, pd.Series, pd.Series, pd.Series, pd.Series]:
    """Align two legs and return (common_index, beta, z, retA, retB).

    spread = logA - beta*logB, beta a rolling OLS slope over the trailing
    BETA_LOOKBACK RTH bars (Cov(logA,logB)/Var(logB) — the intercept/mean is
    absorbed by the z-score's rolling mean). z = (spread - rollmean) / rollstd
    over Z_LOOKBACK RTH bars. Both beta and z are computed on the RTH bar subset
    (contiguous within a session; spreads are not daily-reset like a VWAP) and
    reindexed to the full common index — NaN off-hours and during warmup, so no
    entry/exit comparison fires there. No look-ahead: every value at bar t uses
    only bars up to and including t. retA/retB are simple bar returns on the full
    common index (used for the two-sided P&L; positions never span the overnight
    gap so gap returns are never accrued).
    """
    idx = df_a.index.intersection(df_b.index)
    close_a = df_a["close"].reindex(idx)
    close_b = df_b["close"].reindex(idx)
    ret_a = close_a.pct_change().fillna(0.0)
    ret_b = close_b.pct_change().fillna(0.0)

    ends_ny = (idx + bar_len).tz_convert("America/New_York")
    mins = ends_ny.hour * 60 + ends_ny.minute
    in_rth = (mins > RTH_OPEN_MIN) & (mins <= RTH_CLOSE_MIN)
    if not in_rth.any():
        nan = pd.Series(np.nan, index=idx)
        return idx, nan, nan.copy(), ret_a, ret_b

    rth_idx = idx[in_rth]
    log_a = np.log(close_a.reindex(rth_idx))
    log_b = np.log(close_b.reindex(rth_idx))

    if static_beta:
        beta_rth = pd.Series(1.0, index=rth_idx)
    else:
        w = beta_lookback
        mp = max(3, w // 2)
        cov = log_a.rolling(w, min_periods=mp).cov(log_b)
        var = log_b.rolling(w, min_periods=mp).var()
        beta_rth = cov / var.replace(0, np.nan)

    spread_rth = log_a - beta_rth * log_b
    zmp = max(3, z_lookback // 2)
    mean = spread_rth.rolling(z_lookback, min_periods=zmp).mean()
    std = spread_rth.rolling(z_lookback, min_periods=zmp).std()
    z_rth = (spread_rth - mean) / std.replace(0, np.nan)

    beta = beta_rth.reindex(idx)
    z = z_rth.reindex(idx)
    return idx, beta, z, ret_a, ret_b


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class PairsBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.trading = TradingClient(cfg.api_key, cfg.secret_key, paper=cfg.paper)
        self.data = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
        if cfg.timeframe not in TIMEFRAMES:
            raise ValueError(f"TIMEFRAME must be one of {list(TIMEFRAMES)}")
        if TIMEFRAMES[cfg.timeframe].unit == TimeFrameUnit.Day:
            raise ValueError("pairs_bot is intraday-only (spread z-score needs "
                             "intraday bars). Use 1Min/5Min/15Min/1Hour.")
        if cfg.bar_adjustment not in [a.value for a in Adjustment]:
            raise ValueError(
                f"BAR_ADJUSTMENT must be one of {[a.value for a in Adjustment]}")
        if cfg.data_feed and cfg.data_feed not in [f.value for f in DataFeed]:
            raise ValueError(
                f"DATA_FEED must be one of {[f.value for f in DataFeed]} or unset")
        self.timeframe = TIMEFRAMES[cfg.timeframe]

    # -- data ---------------------------------------------------------------

    def fetch_bars(self, symbols: list, days: int) -> pd.DataFrame:
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

    # -- signal source (single truth for both simulators) -------------------

    def _actionable(self, index: pd.DatetimeIndex) -> pd.Series:
        return rth_actionable(index, self.bar_len(), self.cfg.rth_only)

    def entry_exit_signals(
        self,
        df_a: pd.DataFrame,
        df_b: pd.DataFrame,
        entry_z: float,
        exit_z: float,
        stop_z: float,
        z_lookback: int,
        static_beta: bool,
    ) -> tuple:
        """Boolean/float Series on the common index for one pair — the single
        source of signal truth for both simulators.

        Returns (enter_long, enter_short, exit_level, force_exit, actionable,
        beta, ret_a, ret_b).
        enter_long  — z <= -entry_z  (long A / short B), on a live-actionable bar.
        enter_short — z >= +entry_z  (short A / long B), on a live-actionable bar.
        exit_level  — reversion (|z| <= exit_z) or blow-out stop (|z| >= stop_z).
        force_exit  — session-close flat (last actionable bar) when EXIT_AT_CLOSE.
        """
        cfg = self.cfg
        idx, beta, z, ret_a, ret_b = pair_spread_z(
            df_a, df_b, self.bar_len(), cfg.beta_lookback, z_lookback, static_beta)
        actionable = self._actionable(idx)

        enter_long = (z <= -entry_z) & actionable
        enter_short = (z >= entry_z) & actionable
        absz = z.abs()
        exit_level = (absz <= exit_z)
        if stop_z:
            exit_level = exit_level | (absz >= stop_z)
        exit_level &= actionable

        if cfg.exit_at_close:
            force_exit = session_last_actionable(idx, self.bar_len(), actionable)
        else:
            force_exit = pd.Series(False, index=idx)
        # Don't open on the very bar we'd be forced to close.
        enter_long &= ~force_exit
        enter_short &= ~force_exit
        return (enter_long.fillna(False), enter_short.fillna(False),
                exit_level.fillna(False), force_exit, actionable,
                beta, ret_a, ret_b)

    # -- position state machine (3-state: flat / long-spread / short-spread) -

    def _walk_positions(
        self,
        ret_a: np.ndarray,
        ret_b: np.ndarray,
        beta: np.ndarray,
        enter_long: np.ndarray,
        enter_short: np.ndarray,
        exit_level: np.ndarray,
        force: np.ndarray,
    ) -> tuple:
        """Build (in_market, bar_pnl, sign) arrays from the per-bar signals.

        Same convention as the siblings: entered at the signal bar's close
        (in_market marks the bar AFTER entry), exited at the exit signal bar's
        close. bar_pnl[i] is the position's P&L as a fraction of GROSS exposure
        for bar i's returns, held beta-neutral at the entry beta:
            long spread  (+1): (retA - beta*retB) / (1+|beta|)
            short spread (-1): -that
        No position is ever held across the overnight gap (force_exit closes it),
        so gap returns are never accrued.
        """
        n = len(ret_a)
        in_arr = np.zeros(n, dtype=bool)
        bar_pnl = np.zeros(n)
        sign_arr = np.zeros(n, dtype=np.int8)
        pos = 0            # 0 flat, +1 long spread, -1 short spread
        entry_beta = 0.0
        for i in range(n):
            if pos != 0:
                in_arr[i] = True
                sign_arr[i] = pos
                gross_norm = 1.0 + abs(entry_beta)
                bar_pnl[i] = pos * (ret_a[i] - entry_beta * ret_b[i]) / gross_norm
                if exit_level[i] or force[i]:
                    pos = 0
            if pos == 0 and not in_arr[i] and not force[i]:
                if enter_long[i]:
                    pos, entry_beta = 1, beta[i]
                elif enter_short[i]:
                    pos, entry_beta = -1, beta[i]
        return in_arr, bar_pnl, sign_arr

    # -- backtest simulators ------------------------------------------------

    def simulate(
        self,
        df_a: pd.DataFrame,
        df_b: pd.DataFrame,
        entry_z: float,
        exit_z: float,
        stop_z: float,
        z_lookback: int,
        static_beta: bool,
        eval_from: int = 0,
        eval_to: int | None = None,
    ) -> tuple[int, float, float]:
        """Simulate one pair variant; returns (trades, win rate %, total return
        % on gross). eval_from/eval_to restrict *scoring* to a bar range while
        beta/z warm on full history (walk-forward). Friction: a pairs round trip
        is 4 fills (enter/exit x 2 legs) = 2*COST_BPS of gross (each fill is on a
        partial notional, so per unit of gross it's the same 2*bps as a long-only
        round trip — but the spread P&L it must overcome is far smaller, which is
        why pairs are friction-bound). Fills at the signal bar's close."""
        el, es, xl, fe, _act, beta, ret_a, ret_b = self.entry_exit_signals(
            df_a, df_b, entry_z, exit_z, stop_z, z_lookback, static_beta)
        in_arr, bar_pnl, _sign = self._walk_positions(
            ret_a.to_numpy(), ret_b.to_numpy(), beta.to_numpy(),
            el.to_numpy(), es.to_numpy(), xl.to_numpy(), fe.to_numpy())
        in_market = pd.Series(in_arr, index=ret_a.index)
        pnl = pd.Series(bar_pnl, index=ret_a.index)
        if eval_from or eval_to is not None:
            in_market = in_market.iloc[eval_from:eval_to]
            pnl = pnl.iloc[eval_from:eval_to]
        grp = (in_market != in_market.shift()).cumsum()
        trade_returns = (1 + pnl[in_market]).groupby(grp[in_market]).prod() - 1
        trades = len(trade_returns)
        c = self.cfg.cost_bps / 10000
        cost_f = 1 - 2 * c          # 2*bps of gross per round trip
        trade_returns = (1 + trade_returns) * cost_f - 1
        win_rate = float((trade_returns > 0).mean() * 100) if trades else 0.0
        total = ((1 + pnl * in_market).prod() * cost_f ** trades - 1) * 100
        return trades, win_rate, total

    def entry_betas(
        self,
        df_a: pd.DataFrame,
        df_b: pd.DataFrame,
        z_lookback: int,
        static_beta: bool,
        eval_from: int = 0,
        eval_to: int | None = None,
    ) -> np.ndarray:
        """Hedge ratios captured at each entry within [eval_from, eval_to) — for
        the beta-stability guard (a ratio that drifts train->test is an overfit
        tell)."""
        cfg = self.cfg
        el, es, xl, fe, _act, beta, ret_a, ret_b = self.entry_exit_signals(
            df_a, df_b, cfg.entry_z, cfg.exit_z, cfg.stop_z, z_lookback, static_beta)
        betas = beta.to_numpy()
        el_a, es_a = el.to_numpy(), es.to_numpy()
        fe_a, xl_a = fe.to_numpy(), xl.to_numpy()
        n = len(betas)
        hi = eval_to if eval_to is not None else n
        out, pos, exited = [], 0, False  # mirror _walk_positions' same-bar block
        for i in range(n):
            exited = False
            if pos != 0 and (xl_a[i] or fe_a[i]):
                pos, exited = 0, True
            if pos == 0 and not exited and not fe_a[i] and (el_a[i] or es_a[i]):
                if eval_from <= i < hi and not np.isnan(betas[i]):
                    out.append(betas[i])
                pos = 1 if el_a[i] else -1
        return np.asarray(out)

    def simulate_portfolio(
        self,
        dfs: dict,
        entry_z: float,
        exit_z: float,
        stop_z: float,
        z_lookback: int,
        static_beta: bool,
    ) -> dict:
        """Simulate every pair on ONE shared account with real two-sided
        mark-to-market: long legs are positive qty (cash -=), short legs negative
        (cash += proceeds), equity = cash + Σ qty*price. Each pair position uses
        GROSS_PCT% of equity split beta-neutral; total gross capped at
        MAX_GROSS_PCT. Exits before entries each bar, flat at close. Same signal
        source as simulate(). dfs maps a pair-key 'A/B' to (df_a, df_b)."""
        cfg = self.cfg
        c = cfg.cost_bps / 10000
        keys = list(dfs.keys())
        sig = {}
        # Union of all bar timestamps across every leg, sorted.
        all_idx = None
        for k in keys:
            df_a, df_b = dfs[k]
            s = self.entry_exit_signals(df_a, df_b, entry_z, exit_z, stop_z,
                                        z_lookback, static_beta)
            sig[k] = s
            idx = s[6].index  # ret_a index == common index for the pair
            all_idx = idx if all_idx is None else all_idx.union(idx)
        idx = all_idx.sort_values()

        # Reindex each pair's signals + leg prices onto the shared clock.
        el = {k: sig[k][0].reindex(idx, fill_value=False).to_numpy() for k in keys}
        es = {k: sig[k][1].reindex(idx, fill_value=False).to_numpy() for k in keys}
        xl = {k: sig[k][2].reindex(idx, fill_value=False).to_numpy() for k in keys}
        fe = {k: sig[k][3].reindex(idx, fill_value=False).to_numpy() for k in keys}
        beta = {k: sig[k][5].reindex(idx).to_numpy() for k in keys}
        px_a, px_b, has = {}, {}, {}
        for k in keys:
            df_a, df_b = dfs[k]
            pa = df_a["close"].reindex(idx)
            pb = df_b["close"].reindex(idx)
            has[k] = (pa.notna() & pb.notna()).to_numpy()
            px_a[k] = pa.ffill().to_numpy()
            px_b[k] = pb.ffill().to_numpy()

        cash = cfg.start_equity
        pos = {k: 0 for k in keys}          # -1/0/+1 spread sign
        qa = {k: 0.0 for k in keys}         # signed shares leg A
        qb = {k: 0.0 for k in keys}         # signed shares leg B
        gross_at_entry = {k: 0.0 for k in keys}
        cash_flow_entry = {k: 0.0 for k in keys}   # net cash change when opened
        equity_curve = np.empty(len(idx))
        gross_frac = np.empty(len(idx))
        trade_returns: list = []
        trades_per_pair = {k: 0 for k in keys}

        def leg_value(k, i):
            return qa[k] * px_a[k][i] + qb[k] * px_b[k][i]

        for i in range(len(idx)):
            # --- exits first ---
            for k in keys:
                if pos[k] and has[k][i] and (xl[k][i] or fe[k][i]):
                    proceeds = qa[k] * px_a[k][i] + qb[k] * px_b[k][i]
                    fee = c * (abs(qa[k]) * px_a[k][i] + abs(qb[k]) * px_b[k][i])
                    exit_flow = proceeds - fee
                    cash += exit_flow
                    # Realized dollar P&L = entry cash flow + exit cash flow
                    # (position is flat after a round trip). Return on gross.
                    pnl = cash_flow_entry[k] + exit_flow
                    g0 = gross_at_entry[k]
                    trade_returns.append(pnl / g0 if g0 else 0.0)
                    trades_per_pair[k] += 1
                    qa[k] = qb[k] = 0.0
                    pos[k] = 0
            # mark equity after exits
            equity = cash
            for k in keys:
                if pos[k]:
                    equity += leg_value(k, i)
            # --- entries ---
            cur_gross = sum(abs(qa[k]) * px_a[k][i] + abs(qb[k]) * px_b[k][i]
                            for k in keys if pos[k])
            for k in keys:
                if pos[k] or not has[k][i] or not (el[k][i] or es[k][i]):
                    continue
                b = beta[k][i]
                if np.isnan(b):
                    continue
                target = equity * cfg.gross_pct / 100
                room = equity * cfg.max_gross_pct / 100 - cur_gross
                gross = min(target, room)
                if gross <= 1e-9:
                    continue
                pa_i, pb_i = px_a[k][i], px_b[k][i]
                la = gross / (1 + abs(b))          # long-leg $
                sb = abs(b) * gross / (1 + abs(b)) # short-leg $
                s = 1 if el[k][i] else -1          # +1 long spread, -1 short
                # long spread: +A, -B ; short spread: -A, +B
                qa[k] = (la / pa_i) * s
                qb[k] = (sb / pb_i) * (-s)
                fee = c * (abs(qa[k]) * pa_i + abs(qb[k]) * pb_i)
                entry_flow = -(qa[k] * pa_i + qb[k] * pb_i) - fee  # buy costs, short adds
                cash += entry_flow
                pos[k] = s
                cur_gross += gross
                # remember entry accounting for the round-trip return
                gross_at_entry[k] = gross
                cash_flow_entry[k] = entry_flow
            # mark equity + gross exposure
            eq = cash + sum(leg_value(k, i) for k in keys if pos[k])
            g = sum(abs(qa[k]) * px_a[k][i] + abs(qb[k]) * px_b[k][i]
                    for k in keys if pos[k])
            equity_curve[i] = eq
            gross_frac[i] = g / eq if eq else 0.0

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
            "exposure_pct": float(np.mean(gross_frac) * 100),
            "trades": len(trade_returns),
            "win_rate": (wins / len(trade_returns) * 100) if trade_returns else 0.0,
            "trades_per_pair": trades_per_pair,
            "open_positions": [k for k in keys if pos[k]],
        }

    # -- live passes (DEFERRED) ---------------------------------------------

    def run_once(self) -> None:
        raise SystemExit(
            "pairs_bot live trading is DEFERRED. This bot shorts, and per the "
            "project norm no deploy scaffolding is built until the walk-forward "
            "gate proves an OOS edge at COST_BPS=5. Use `backtest`/`portfolio`.")

    def run_loop(self) -> None:
        self.run_once()

    # -- backtest / portfolio reporting -------------------------------------

    def _log_backtest_results(self, rows: list) -> None:
        if not rows:
            return
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "pairs_backtest_results.csv")
        new_file = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            if new_file:
                writer.writeheader()
            writer.writerows(rows)
        print(f"\nLogged {len(rows)} variant results to pairs_backtest_results.csv")

    def _variants(self) -> list:
        """(name, entry_z, exit_z, stop_z, z_lookback, static_beta). First row
        is the resolved config so a candidate A/Bs against the standards."""
        cfg = self.cfg
        zl = cfg.z_lookback
        return [
            ("active settings", cfg.entry_z, cfg.exit_z, cfg.stop_z, zl, cfg.static_beta),
            ("z2.0 exit0.5", 2.0, 0.5, 0.0, zl, False),
            ("z2.0 exit0.0", 2.0, 0.0, 0.0, zl, False),
            ("z2.5 exit0.5", 2.5, 0.5, 0.0, zl, False),
            ("z1.5 exit0.5", 1.5, 0.5, 0.0, zl, False),
            ("z2.0 exit0.5 stop4", 2.0, 0.5, 4.0, zl, False),
            ("z2.0 exit0.5 zl20", 2.0, 0.5, 0.0, 20, False),
            ("z2.0 exit0.5 static", 2.0, 0.5, 0.0, zl, True),
        ]

    def _suffix(self) -> str:
        tag = ""
        if not self.cfg.rth_only:
            tag += " [all-hours]"
        if not self.cfg.exit_at_close:
            tag += " [hold-overnight]"
        return tag

    def backtest(self) -> None:
        cfg = self.cfg
        log.info("Fetching %d days of %s bars for %s — this can take a minute...",
                 cfg.backtest_days, cfg.timeframe, ",".join(cfg.symbols))
        bars = self.fetch_bars(cfg.symbols, days=cfg.backtest_days)
        log.info("Fetched %d bars", len(bars))
        variants = self._variants()
        split_frac = float(os.environ.get("SPLIT_FRAC", "0.7"))
        suffix = self._suffix()

        print(f"\nBacktest: market-neutral pairs reversion, timeframe="
              f"{cfg.timeframe}, last {cfg.backtest_days} days")
        print(f"'active settings' = resolved config: entry_z {cfg.entry_z:g}, "
              f"exit_z {cfg.exit_z:g}, stop_z {cfg.stop_z:g}, beta_lookback "
              f"{cfg.beta_lookback}, z_lookback {cfg.z_lookback}, "
              f"{'STATIC beta=1' if cfg.static_beta else 'rolling OLS beta'}, "
              f"exit_at_close={cfg.exit_at_close}")
        if not cfg.rth_only:
            print("GLOBAL: RTH_ONLY=false — acting on extended-hours bars live "
                  "never sees")

        results = []
        run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        leg_dfs: dict = {}
        for s in cfg.symbols:
            try:
                leg_dfs[s] = bars.xs(s, level="symbol")
            except KeyError:
                leg_dfs[s] = None

        for a, b in cfg.pairs:
            key = f"{a}/{b}"
            if leg_dfs.get(a) is None or leg_dfs.get(b) is None:
                print(f"\n{key}: no data for a leg")
                continue
            df_a, df_b = leg_dfs[a], leg_dfs[b]
            common = df_a.index.intersection(df_b.index)
            split = int(len(common) * split_frac)
            print(f"\n{key} — {len(common)} shared bars  "
                  f"(train/test split at bar {split})")
            header = (f"  {'variant':<24}{'trades':>7}{'win %':>8}{'full %':>9}"
                      f"{'train %':>9}{'test %':>9}")
            print(header)
            print("  " + "-" * (len(header) - 2))
            for name, ez, xz, sz, zl, sb in variants:
                name += suffix
                trades, win, full = self.simulate(df_a, df_b, ez, xz, sz, zl, sb)
                _, _, train = self.simulate(df_a, df_b, ez, xz, sz, zl, sb, eval_to=split)
                t_tr, _, test = self.simulate(df_a, df_b, ez, xz, sz, zl, sb,
                                              eval_from=split)
                print(f"  {name:<24}{trades:>7}{win:>7.1f}%{full:>+8.2f}%"
                      f"{train:>+8.2f}%{test:>+8.2f}%")
                results.append({
                    "run_ts": run_ts, "timeframe": cfg.timeframe,
                    "days": cfg.backtest_days, "pair": key, "variant": name,
                    "entry_z": ez, "exit_z": xz, "stop_z": sz,
                    "beta_lookback": cfg.beta_lookback, "z_lookback": zl,
                    "static_beta": sb, "cost_bps": cfg.cost_bps,
                    "trades": trades, "win_rate": round(win, 2),
                    "return_pct": round(full, 2), "train_pct": round(train, 2),
                    "test_pct": round(test, 2), "test_trades": t_tr,
                })
            # Beta-stability guard: entry-beta mean±std, train vs test.
            if not cfg.static_beta:
                b_tr = self.entry_betas(df_a, df_b, cfg.z_lookback, False, eval_to=split)
                b_te = self.entry_betas(df_a, df_b, cfg.z_lookback, False, eval_from=split)
                def fmt(x):
                    return (f"{x.mean():.2f}±{x.std():.2f} (n={len(x)})"
                            if len(x) else "n=0")
                print(f"  beta @ entry (rolling OLS): train {fmt(b_tr)}  "
                      f"test {fmt(b_te)}")
        self._log_backtest_results(results)
        print(f"\nNote: return is on GROSS exposure (|long$|+|short$|); friction "
              f"COST_BPS={cfg.cost_bps:g} bps/side x4 fills = {2*cfg.cost_bps:g}bps "
              f"of gross per round trip; fills at signal-bar close (optimistic on "
              f"gap-through bars); adjustment='{cfg.bar_adjustment}'; live-actionable "
              f"RTH only={cfg.rth_only}. Each pair isolated — see `portfolio` for "
              f"the shared-account two-sided book.\n")

    def portfolio(self) -> None:
        cfg = self.cfg
        log.info("Fetching %d days of %s bars for %s — this can take a minute...",
                 cfg.backtest_days, cfg.timeframe, ",".join(cfg.symbols))
        bars = self.fetch_bars(cfg.symbols, days=cfg.backtest_days)
        dfs: dict = {}
        for a, b in cfg.pairs:
            try:
                dfs[f"{a}/{b}"] = (bars.xs(a, level="symbol"),
                                   bars.xs(b, level="symbol"))
            except KeyError:
                print(f"{a}/{b}: no data, excluded")
        if not dfs:
            sys.exit("No data for any pair")
        res = self.simulate_portfolio(dfs, cfg.entry_z, cfg.exit_z, cfg.stop_z,
                                      cfg.z_lookback, cfg.static_beta)
        span = max((res["equity"].index[-1] - res["equity"].index[0]).days, 1)
        span_years = span / 365.25
        tpd = res["trades"] / max(span, 1)
        print(f"\nPortfolio backtest (market-neutral, two-sided MTM): "
              f"{cfg.timeframe}, last {cfg.backtest_days} days, {', '.join(dfs)}")
        print(f"start ${cfg.start_equity:g}, per-pair gross {cfg.gross_pct:g}% of "
              f"equity (cap {cfg.max_gross_pct:g}% total), COST_BPS={cfg.cost_bps:g}"
              f"/side, adjustment='{cfg.bar_adjustment}'")
        print(f"entry_z {cfg.entry_z:g}, exit_z {cfg.exit_z:g}, stop_z {cfg.stop_z:g}, "
              f"beta_lookback {cfg.beta_lookback}, z_lookback {cfg.z_lookback}, "
              f"{'STATIC beta=1' if cfg.static_beta else 'rolling OLS beta'}, "
              f"exit_at_close={cfg.exit_at_close}")
        print(f"\n  {'':<14}{'return':>9}{'CAGR':>8}{'max DD':>8}")
        print(f"  {'strategy':<14}{res['return_pct']:>+8.1f}%"
              f"{res['cagr_pct']:>+7.1f}%{res['max_dd_pct']:>7.1f}%")
        print("  (market-neutral book — benchmark is cash/0%, not buy & hold)")
        per_pair = ", ".join(f"{k} {n}" for k, n in res["trades_per_pair"].items())
        print(f"\n  final equity ${res['final_equity']:,.0f}, avg gross exposure "
              f"{res['exposure_pct']:.0f}%, {res['trades']} closed trades "
              f"(win rate {res['win_rate']:.1f}%, ~{tpd:.1f}/day): {per_pair}")
        if res["open_positions"]:
            print(f"  still open at window end: {', '.join(res['open_positions'])}")
        run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._log_backtest_results([{
            "run_ts": run_ts, "timeframe": cfg.timeframe,
            "days": cfg.backtest_days, "pair": "PORTFOLIO",
            "variant": f"portfolio {cfg.gross_pct:g}%gross, {cfg.cost_bps:g}bps"
                       f"{self._suffix()}",
            "entry_z": cfg.entry_z, "exit_z": cfg.exit_z, "stop_z": cfg.stop_z,
            "beta_lookback": cfg.beta_lookback, "z_lookback": cfg.z_lookback,
            "static_beta": cfg.static_beta, "cost_bps": cfg.cost_bps,
            "trades": res["trades"], "win_rate": round(res["win_rate"], 2),
            "return_pct": round(res["return_pct"], 2), "train_pct": "",
            "test_pct": "", "test_trades": "",
        }])
        print(f"  (max drawdown {res['max_dd_pct']:.1f}% over {span_years:.1f}y)\n")


# ---------------------------------------------------------------------------

def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"
    if mode not in ("run", "loop", "backtest", "portfolio", "pnl"):
        print(__doc__)
        sys.exit(1)
    load_dotenv()
    if not os.environ.get("ALPACA_API_KEY") or not os.environ.get("ALPACA_SECRET_KEY"):
        sys.exit(
            "Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.\n"
            "Add them to rsi-midline-bot/.env (see .env.example) or export them.")
    apply_profile(os.environ.get("TIMEFRAME", "5Min"))
    cfg = Config()
    bot = PairsBot(cfg)
    if mode == "backtest":
        bot.backtest()
    elif mode == "portfolio":
        bot.portfolio()
    elif mode in ("run", "loop"):
        bot.run_once()
    else:
        sys.exit("pnl: no live journal yet (live trading deferred).")


if __name__ == "__main__":
    main()
