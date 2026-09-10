"""Small, pure helpers.  Everything here is deterministic and unit-testable."""

from __future__ import annotations

import math
from datetime import datetime, time
from typing import Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from .config import SessionConfig, SymbolSpec


# --------------------------------------------------------------------------- #
# Price / pip conversion
# --------------------------------------------------------------------------- #
def pip_size(spec: SymbolSpec) -> float:
    return spec.resolved_pip_size()


def price_to_pips(price_delta: float, spec: SymbolSpec) -> float:
    return price_delta / pip_size(spec)


def pips_to_price(pips: float, spec: SymbolSpec) -> float:
    return pips * pip_size(spec)


def round_to_tick(price: float, spec: SymbolSpec) -> float:
    """Round a price to the instrument's tick grid, then to its digits."""
    if spec.tick_size <= 0:
        return round(price, spec.digits)
    return round(round(price / spec.tick_size) * spec.tick_size, spec.digits)


def round_volume(volume: float, spec: SymbolSpec) -> float:
    """Floor to the volume step (never round *up*: that would over-risk)."""
    if spec.volume_step <= 0:
        return volume
    steps = math.floor(volume / spec.volume_step + 1e-9)
    vol = steps * spec.volume_step
    # volume_step is typically 0.01 -> guard against binary float dust
    decimals = max(0, -int(math.floor(math.log10(spec.volume_step))))
    return round(vol, decimals)


def money_per_price_unit(spec: SymbolSpec) -> float:
    """Account-currency P/L for a 1.0-lot position per 1.0 unit of price move."""
    if spec.tick_size <= 0:
        raise ValueError("tick_size must be > 0")
    return spec.tick_value / spec.tick_size


# --------------------------------------------------------------------------- #
# Sessions / calendar
# --------------------------------------------------------------------------- #
def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def in_session(ts: pd.Timestamp, cfg: SessionConfig) -> bool:
    """True if ``ts`` (bar OPEN time, tz-aware or naive UTC) is tradable."""
    if not cfg.enabled:
        return True
    if ts.weekday() not in cfg.weekdays:
        return False
    t = ts.time()
    for start_s, end_s in cfg.windows:
        start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
        blocked_open = cfg.block_minutes_after_open
        blocked_close = cfg.block_minutes_before_close
        s_minutes = start.hour * 60 + start.minute + blocked_open
        e_minutes = end.hour * 60 + end.minute - blocked_close
        cur = t.hour * 60 + t.minute
        if s_minutes <= e_minutes:
            if s_minutes <= cur < e_minutes:
                return True
        else:  # window wraps midnight
            if cur >= s_minutes or cur < e_minutes:
                return True
    return False


def trading_day(ts: pd.Timestamp) -> str:
    """Day key used for per-day risk counters (UTC calendar day)."""
    return ts.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b not in (0, 0.0) else default


def consecutive_runs(flags: Sequence[bool]) -> Tuple[int, int]:
    """Return (max run of True, max run of False)."""
    best_t = best_f = cur_t = cur_f = 0
    for f in flags:
        if f:
            cur_t += 1
            cur_f = 0
        else:
            cur_f += 1
            cur_t = 0
        best_t = max(best_t, cur_t)
        best_f = max(best_f, cur_f)
    return best_t, best_f


def fmt_ts(ts: Optional[pd.Timestamp]) -> str:
    return "" if ts is None or pd.isna(ts) else pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M")
