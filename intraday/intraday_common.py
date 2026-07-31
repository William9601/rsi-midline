"""Shared intraday primitives for the sub-daily bots (mean_reversion_bot,
orb_bot).

The one thing these bots MUST agree on is which bars live can actually act on
— the RTH-actionable mask and the session's last actionable bar (where a
flat-at-close exit is booked). Keeping that logic in one place means the two
strategies can never drift apart from live, the same reason rsi_midline_bot
keeps its RTH mask in a single `entry_exit_signals`.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

# ET regular-trading-hours boundaries, minutes-from-midnight.
RTH_OPEN_MIN = 9 * 60 + 30   # 570  (09:30)
RTH_CLOSE_MIN = 16 * 60      # 960  (16:00)


def minute_of_end(index: pd.DatetimeIndex, bar_len: timedelta) -> np.ndarray:
    """ET minute-of-day at which each bar *ends* (bar closes at index+bar_len)."""
    ends = (index + bar_len).tz_convert("America/New_York")
    return np.asarray(ends.hour * 60 + ends.minute)


def rth_actionable(index: pd.DatetimeIndex, bar_len: timedelta,
                   rth_only: bool = True) -> pd.Series:
    """Bars live could act on: bar close inside 9:30-16:00 ET, PLUS the last
    bar to complete at-or-before the open (the first post-open poll still sees
    it as the newest completed bar). Identical to rsi_midline_bot's RTH mask.
    rth_only=False marks every bar actionable (extended-hours experiments only).
    """
    if not rth_only:
        return pd.Series(True, index=index)
    ends = (index + bar_len).tz_convert("America/New_York")
    nxt_ends = ends[1:].append(ends[-1:] + bar_len)
    mins = ends.hour * 60 + ends.minute
    nxt_mins = nxt_ends.hour * 60 + nxt_ends.minute
    return pd.Series(
        (mins < RTH_CLOSE_MIN)
        & ((nxt_mins > RTH_OPEN_MIN) | (nxt_ends.date != ends.date)),
        index=index)


def session_last_actionable(index: pd.DatetimeIndex, bar_len: timedelta,
                            actionable: pd.Series) -> pd.Series:
    """True on the last actionable bar of each ET session — where a
    flat-at-close exit is booked (the last bar live can still act on before
    the bell)."""
    ends = (index + bar_len).tz_convert("America/New_York")
    dates = np.asarray(ends.date)
    act = actionable.to_numpy()
    force = np.zeros(len(index), dtype=bool)
    seen: set = set()
    for i in range(len(index) - 1, -1, -1):
        if act[i] and dates[i] not in seen:
            force[i] = True
            seen.add(dates[i])
    return pd.Series(force, index=index)


def session_ids(index: pd.DatetimeIndex, bar_len: timedelta) -> np.ndarray:
    """Integer session id per bar (ET calendar day of the bar's close), so a
    numpy state loop can detect session boundaries cheaply."""
    ends = (index + bar_len).tz_convert("America/New_York")
    dates = np.asarray(ends.normalize().view("int64"))
    # Map distinct day values to 0..k preserving order.
    _, ids = np.unique(dates, return_inverse=True)
    return ids
