"""Shared fixtures.

Every fixture builds price data *explicitly*, bar by bar.  Random data is
useless for testing logic: you cannot assert on what you did not construct.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

import pandas as pd

from trading_bot.config import (BacktestConfig, Config, CostConfig, EngulfConfig,
                                POIConfig, RetestConfig, RiskConfig, SessionConfig,
                                SymbolSpec, TradeConfig)

START = pd.Timestamp("2024-01-01 00:00", tz="UTC")


def m5_frame(bars: Sequence[Tuple[float, float, float, float]],
             start: pd.Timestamp = START) -> pd.DataFrame:
    """Build an M5 frame from explicit (open, high, low, close) tuples."""
    idx = pd.date_range(start, periods=len(bars), freq="5min")
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"])
    df.insert(0, "timestamp", idx)
    df["volume"] = 100.0
    return df


def m1_frame(bars: Sequence[Tuple[float, float, float, float]],
             start: pd.Timestamp = START) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(bars), freq="1min")
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"])
    df.insert(0, "timestamp", idx)
    df["volume"] = 20.0
    return df


def m15_frame(bars: Sequence[Tuple[float, float, float, float]],
              start: pd.Timestamp = START) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(bars), freq="15min")
    df = pd.DataFrame(bars, columns=["open", "high", "low", "close"])
    df.insert(0, "timestamp", idx)
    df["volume"] = 300.0
    df["bar_end"] = df["timestamp"] + pd.Timedelta(minutes=15)
    return df


def test_config(**over) -> Config:
    """A small, deterministic config suited to hand-built fixtures."""
    cfg = Config()
    cfg.symbol_spec = SymbolSpec()          # 5-digit EURUSD, pip = 0.0001
    cfg.poi = POIConfig(atr_period=3, max_leg_bars=5, displacement_atr_mult=1.0,
                        max_zone_width_pips=18.0, min_zone_width_pips=0.5)
    cfg.retest = RetestConfig(min_departure_pips=8.0, min_m5_bars_outside=3,
                              max_m5_bars_to_retest=200, touch_tolerance_pips=0.5)
    cfg.engulf = EngulfConfig(max_m5_bars_after_retest=6, min_body_pips=0.5,
                              zone_proximity_pips=2.0)
    cfg.trade = TradeConfig(sl_buffer_pips=1.0, max_sl_pips=20.0, min_sl_pips=1.0)
    cfg.session = SessionConfig(enabled=False)
    cfg.risk = RiskConfig(max_trades_per_day=99, max_consecutive_losses=0,
                          cooldown_m5_bars_after_loss=0, max_daily_loss_pct=1.0)
    cfg.costs = CostConfig(spread_pips=0.0, slippage_entry_pips=0.0,
                           slippage_stop_pips=0.0, commission_per_lot_round_turn=0.0)
    cfg.backtest = BacktestConfig(initial_equity=10_000.0)
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


# --------------------------------------------------------------------------- #
# A complete, hand-built LONG setup:
#   M15 #6  = bearish origin candle  -> POI [1.1000, 1.1006]
#   M15 #7  = bullish displacement   -> POI confirmed
#   M15 #8  = departure (all M5 lows above the zone)
#   M15 #9  = return to the zone     -> retest on M5 #29
#   M15 #10 = bullish M5 engulfing   -> signal on M5 #30
# --------------------------------------------------------------------------- #
def long_setup_bars() -> List[Tuple[float, float, float, float]]:
    bars: List[Tuple[float, float, float, float]] = []
    # 6 warm-up M15 bars (18 M5 bars) oscillating in a 5-pip band
    for k in range(18):
        base = 1.1000 + (0.00005 if k % 2 else -0.00005)
        bars.append((base, base + 0.00025, base - 0.00025, base))
    # M15 #6 - the origin candle (bearish): O 1.1005 H 1.1006 L 1.1000 C 1.1001
    bars += [(1.10050, 1.10060, 1.10030, 1.10040),
             (1.10040, 1.10045, 1.10010, 1.10020),
             (1.10020, 1.10025, 1.10000, 1.10010)]
    # M15 #7 - displacement: closes at 1.1029, above high[o] = 1.1006
    bars += [(1.10010, 1.10120, 1.10005, 1.10110),
             (1.10110, 1.10220, 1.10105, 1.10210),
             (1.10210, 1.10300, 1.10205, 1.10290)]
    # M15 #8 - three M5 bars entirely above the zone -> departure confirmed
    bars += [(1.10290, 1.10310, 1.10250, 1.10270),
             (1.10270, 1.10290, 1.10240, 1.10260),
             (1.10260, 1.10280, 1.10230, 1.10250)]
    # M15 #9 - decline back into the zone; M5 #29 touches 1.1004
    bars += [(1.10250, 1.10255, 1.10150, 1.10160),
             (1.10160, 1.10165, 1.10080, 1.10090),
             (1.10080, 1.10085, 1.10040, 1.10050)]   # <- retest bar (bearish)
    # M15 #10 - bullish engulfing of M5 #29, then filler
    bars += [(1.10040, 1.10105, 1.10030, 1.10100),   # <- engulfing bar (#30)
             (1.10100, 1.10150, 1.10090, 1.10140),
             (1.10140, 1.10190, 1.10130, 1.10180)]
    return bars
