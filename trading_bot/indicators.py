"""Indicators and pattern primitives.

All functions are pure and vectorised where it matters.  The engulfing test is
deliberately written as a scalar function over two candles so that it can be
unit-tested exhaustively against hand-built edge cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from .config import EngulfConfig

EPS = 1e-12


# --------------------------------------------------------------------------- #
# ATR
# --------------------------------------------------------------------------- #
def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR.  Shifted usage is the caller's responsibility: ``atr[i]``
    includes bar ``i`` and is therefore known only at the close of bar ``i``.
    """
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


# --------------------------------------------------------------------------- #
# Swing pivots (confirmed, i.e. usable only `right` bars later)
# --------------------------------------------------------------------------- #
def swing_flags(df: pd.DataFrame, left: int = 2, right: int = 2
                ) -> Tuple[np.ndarray, np.ndarray]:
    """Return boolean arrays (is_swing_high, is_swing_low) positioned at the
    pivot bar.  A pivot at index p is only CONFIRMED at index p + right;
    callers must respect that delay.
    """
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    n = len(df)
    sh = np.zeros(n, dtype=bool)
    sl = np.zeros(n, dtype=bool)
    for p in range(left, n - right):
        wl_h, wr_h = h[p - left:p], h[p + 1:p + right + 1]
        wl_l, wr_l = l[p - left:p], l[p + 1:p + right + 1]
        if h[p] > wl_h.max(initial=-np.inf) and h[p] >= wr_h.max(initial=-np.inf):
            sh[p] = True
        if l[p] < wl_l.min(initial=np.inf) and l[p] <= wr_l.min(initial=np.inf):
            sl[p] = True
    return sh, sl


# --------------------------------------------------------------------------- #
# Engulfing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_top(self) -> float:
        return max(self.open, self.close)

    @property
    def body_bottom(self) -> float:
        return min(self.open, self.close)


@dataclass(frozen=True)
class EngulfResult:
    ok: bool
    reason: str = ""


def is_engulfing(prev: Candle, cur: Candle, bullish: bool,
                 cfg: EngulfConfig, pip: float) -> EngulfResult:
    """Objective engulfing test.

    Baseline (cfg defaults):
      bullish: prev bearish, cur bullish, cur body fully engulfs prev body, i.e.
               cur.open <= prev.close  and  cur.close >= prev.open
      bearish: mirror image.

    Ties are resolved with >= / <= : an exact equality between the two bodies
    still counts as engulfing.  This matters because FX opens frequently equal
    the previous close, so a strict '>' would silently discard most signals.
    Doji candles (body == 0) are excluded by ``min_body_pips``, otherwise every
    flat bar 'engulfs' its neighbour.
    """
    if cfg.require_prev_opposite:
        if bullish and not prev.bearish:
            return EngulfResult(False, "prev_not_bearish")
        if not bullish and not prev.bullish:
            return EngulfResult(False, "prev_not_bullish")

    if bullish and not cur.bullish:
        return EngulfResult(False, "cur_not_bullish")
    if not bullish and not cur.bearish:
        return EngulfResult(False, "cur_not_bearish")

    if cfg.require_body_engulf:
        if cur.body_bottom > prev.body_bottom + EPS or cur.body_top < prev.body_top - EPS:
            return EngulfResult(False, "body_not_engulfed")

    if cfg.engulf_use_range:
        if cur.high < prev.high - EPS or cur.low > prev.low + EPS:
            return EngulfResult(False, "range_not_engulfed")

    if cfg.min_body_ratio > 0 and cur.body < cfg.min_body_ratio * prev.body - EPS:
        return EngulfResult(False, "body_ratio")

    if cfg.min_range_ratio > 0 and cur.range < cfg.min_range_ratio * prev.range - EPS:
        return EngulfResult(False, "range_ratio")

    if cfg.min_body_pips > 0 and cur.body < cfg.min_body_pips * pip - EPS:
        return EngulfResult(False, "body_too_small")

    if cfg.max_body_pips > 0 and cur.body > cfg.max_body_pips * pip + EPS:
        return EngulfResult(False, "body_too_large")

    if cfg.close_position_pct > 0 and cur.range > EPS:
        pos = (cur.close - cur.low) / cur.range
        if bullish and pos < 1.0 - cfg.close_position_pct:
            return EngulfResult(False, "close_not_near_high")
        if not bullish and pos > cfg.close_position_pct:
            return EngulfResult(False, "close_not_near_low")

    return EngulfResult(True)


def engulf_columns(df: pd.DataFrame, cfg: EngulfConfig, pip: float
                   ) -> pd.DataFrame:
    """Vectorised convenience wrapper used by research notebooks/plots."""
    out = pd.DataFrame(index=df.index)
    prev = df.shift(1)
    bull, bear = [], []
    for i in range(len(df)):
        if i == 0 or np.isnan(prev["open"].iloc[i]):
            bull.append(False)
            bear.append(False)
            continue
        p = Candle(prev["open"].iloc[i], prev["high"].iloc[i],
                   prev["low"].iloc[i], prev["close"].iloc[i])
        c = Candle(df["open"].iloc[i], df["high"].iloc[i],
                   df["low"].iloc[i], df["close"].iloc[i])
        bull.append(is_engulfing(p, c, True, cfg, pip).ok)
        bear.append(is_engulfing(p, c, False, cfg, pip).ok)
    out["bull_engulf"] = bull
    out["bear_engulf"] = bear
    return out
