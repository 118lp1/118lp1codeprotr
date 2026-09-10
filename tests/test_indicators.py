"""Engulfing / indicator tests, including the tie and doji edge cases that
decide whether a rule fires at all on real FX data."""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading_bot.config import EngulfConfig
from trading_bot.indicators import Candle, atr, is_engulfing, swing_flags

PIP = 0.0001
CFG = EngulfConfig(min_body_pips=0.5, min_body_ratio=1.0)


def test_bullish_engulfing_basic():
    prev = Candle(1.1010, 1.1012, 1.1004, 1.1005)      # bearish
    cur = Candle(1.1004, 1.1016, 1.1003, 1.1015)       # bullish, engulfs body
    assert is_engulfing(prev, cur, True, CFG, PIP).ok


def test_bearish_engulfing_basic():
    prev = Candle(1.1005, 1.1012, 1.1004, 1.1010)      # bullish
    cur = Candle(1.1011, 1.1013, 1.1000, 1.1002)       # bearish, engulfs body
    assert is_engulfing(prev, cur, False, CFG, PIP).ok


def test_direction_must_match():
    prev = Candle(1.1010, 1.1012, 1.1004, 1.1005)
    cur = Candle(1.1004, 1.1016, 1.1003, 1.1015)
    assert not is_engulfing(prev, cur, False, CFG, PIP).ok


def test_prev_must_be_opposite():
    prev = Candle(1.1000, 1.1012, 1.0999, 1.1005)      # bullish
    cur = Candle(1.0999, 1.1016, 1.0998, 1.1015)       # bullish
    res = is_engulfing(prev, cur, True, CFG, PIP)
    assert not res.ok and res.reason == "prev_not_bearish"


def test_partial_engulf_rejected():
    prev = Candle(1.1010, 1.1012, 1.1004, 1.1005)
    cur = Candle(1.1006, 1.1016, 1.1005, 1.1015)       # open above prev close
    res = is_engulfing(prev, cur, True, CFG, PIP)
    assert not res.ok and res.reason == "body_not_engulfed"


def test_exact_tie_counts_as_engulfing():
    """FX opens routinely equal the previous close.  A strict '>' would throw
    away most real signals, so equality must pass."""
    prev = Candle(1.1010, 1.1012, 1.1004, 1.1005)
    cur = Candle(1.1005, 1.1016, 1.1004, 1.1010)       # open == prev.close exactly
    assert is_engulfing(prev, cur, True, CFG, PIP).ok


def test_doji_rejected_by_min_body():
    prev = Candle(1.10100, 1.10105, 1.10098, 1.10099)  # 0.1 pip body
    cur = Candle(1.10098, 1.10106, 1.10097, 1.10101)   # 0.2 pip body
    res = is_engulfing(prev, cur, True, CFG, PIP)
    assert not res.ok and res.reason == "body_too_small"


def test_body_ratio_filter():
    cfg = EngulfConfig(min_body_pips=0.1, min_body_ratio=3.0)
    prev = Candle(1.1010, 1.1012, 1.1004, 1.1005)      # 5 pip body
    cur = Candle(1.1005, 1.1016, 1.1004, 1.1011)       # 6 pip body < 3x
    res = is_engulfing(prev, cur, True, cfg, PIP)
    assert not res.ok and res.reason == "body_ratio"


def test_close_position_filter():
    cfg = EngulfConfig(min_body_pips=0.1, close_position_pct=0.25)
    prev = Candle(1.1010, 1.1012, 1.1004, 1.1005)
    cur = Candle(1.1004, 1.1030, 1.1003, 1.1011)       # closes mid-range
    res = is_engulfing(prev, cur, True, cfg, PIP)
    assert not res.ok and res.reason == "close_not_near_high"


def test_range_engulf_option():
    cfg = EngulfConfig(min_body_pips=0.1, engulf_use_range=True)
    prev = Candle(1.1010, 1.1020, 1.1000, 1.1005)      # wide range
    cur = Candle(1.1004, 1.1016, 1.1003, 1.1015)       # body engulfs, range does not
    assert not is_engulfing(prev, cur, True, cfg, PIP).ok
    assert is_engulfing(prev, cur, True, EngulfConfig(min_body_pips=0.1), PIP).ok


def test_atr_is_causal_and_positive():
    df = pd.DataFrame({"open": [1.1, 1.101, 1.102, 1.103, 1.104],
                       "high": [1.1005, 1.1015, 1.1025, 1.1035, 1.1045],
                       "low": [1.0995, 1.1005, 1.1015, 1.1025, 1.1035],
                       "close": [1.101, 1.102, 1.103, 1.104, 1.105]})
    a = atr(df, 3)
    assert np.isnan(a.iloc[0]) and np.isnan(a.iloc[1])
    assert a.iloc[2] > 0
    # truncating the future must not change a past value
    assert abs(atr(df.iloc[:4], 3).iloc[2] - a.iloc[2]) < 1e-15


def test_swing_pivot_position():
    highs = [1.0, 1.1, 1.5, 1.2, 1.05, 1.0]
    df = pd.DataFrame({"high": highs, "low": [h - 0.5 for h in highs]})
    sh, sl = swing_flags(df, 2, 2)
    assert sh[2] and not sh[1] and not sh[3]
