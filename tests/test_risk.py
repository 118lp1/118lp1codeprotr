"""Risk-layer tests.

Position sizing is the place where a silent bug costs real money without ever
producing an error message, so the arithmetic is pinned down instrument by
instrument.
"""

from __future__ import annotations

import pandas as pd

from trading_bot.config import Config, Direction, SymbolSpec
from trading_bot.risk import (RiskManager, build_order, position_size,
                              stop_loss_price, take_profit_price)
from trading_bot.strategy import POI, Signal
from trading_bot.config import POIType
from trading_bot.utils import money_per_price_unit, pip_size, round_volume
from tests.fixtures import test_config

TS = pd.Timestamp("2024-01-02 10:00", tz="UTC")


def _signal(lower: float, upper: float, direction=Direction.LONG) -> Signal:
    poi = POI(poi_id="POI1", direction=direction, poi_type=POIType.ORDER_BLOCK,
              upper=upper, lower=lower, origin_time=TS, created_time=TS)
    return Signal(signal_time=TS, signal_index=10, direction=direction, poi=poi,
                  engulf_open=1.1000, engulf_high=1.1010, engulf_low=1.0995,
                  engulf_close=1.1008, engulf_time=TS)


# --------------------------------------------------------------------------- #
def test_pip_size_fx_5_digit():
    assert abs(pip_size(SymbolSpec(digits=5, point=0.00001)) - 0.0001) < 1e-12


def test_pip_size_jpy_3_digit():
    assert abs(pip_size(SymbolSpec(digits=3, point=0.001)) - 0.01) < 1e-12


def test_pip_size_explicit_override_for_non_fx():
    gold = SymbolSpec(name="XAUUSD", digits=2, point=0.01, pip_size=0.1)
    assert abs(pip_size(gold) - 0.1) < 1e-12


def test_pip_size_4_digit_is_not_fractional():
    assert abs(pip_size(SymbolSpec(digits=4, point=0.0001)) - 0.0001) < 1e-12


def test_volume_rounding_never_rounds_up():
    spec = SymbolSpec(volume_step=0.01)
    assert round_volume(0.4699, spec) == 0.46
    assert round_volume(0.999, spec) == 0.99


# --------------------------------------------------------------------------- #
def test_stop_loss_sits_beyond_the_poi_with_buffer():
    cfg = test_config()
    cfg.trade.sl_buffer_pips = 1.0
    assert abs(stop_loss_price(1.1006, 1.1000, Direction.LONG, cfg) - 1.0999) < 1e-9
    assert abs(stop_loss_price(1.1006, 1.1000, Direction.SHORT, cfg) - 1.1007) < 1e-9


def test_take_profit_is_exactly_two_r():
    cfg = test_config()
    tp = take_profit_price(1.1000, 1.0990, Direction.LONG, cfg)
    assert abs(tp - 1.1020) < 1e-9
    tp_s = take_profit_price(1.1000, 1.1010, Direction.SHORT, cfg)
    assert abs(tp_s - 1.0980) < 1e-9


def test_tp_uses_the_actual_fill_not_the_signal_price():
    """Spread and slippage move the entry, so the 1:2 must be measured from the
    fill.  Otherwise the advertised RR is not the RR that is traded."""
    cfg = test_config()
    cfg.costs.spread_pips = 2.0
    # This test is about TP arithmetic, not about trade selection.  A 2-pip
    # spread trips the cost-drag floor, which would reject the order before the
    # arithmetic is ever exercised, so switch that gate off here.
    cfg.trade.min_stop_spread_mult = 0.0
    plan = build_order(_signal(1.1000, 1.1006), next_open=1.1010, equity=10_000, cfg=cfg)
    assert plan.ok
    assert abs(plan.entry - 1.1012) < 1e-9                       # 1.1010 + 2 pips
    assert abs((plan.tp - plan.entry) - 2 * (plan.entry - plan.sl)) < 1e-9


def test_stop_too_tight_vs_spread_is_rejected():
    """A stop worth only a few spreads cannot clear its own breakeven.

    At 1:2 the required win rate is (1 + c/R)/3.  With c = 1 pip and R = 4 pips
    that is 41.7 %, against roughly 29 % for a driftless process -- the trade is
    a slow certain loss, so it is declined rather than sized.
    """
    cfg = test_config()
    cfg.costs.spread_pips = 1.0
    cfg.trade.min_stop_spread_mult = 8.0
    # POI 1.1000-1.1006, entry ~1.1011 -> stop ~12 pips, floor is 8 pips: allowed
    ok = build_order(_signal(1.1000, 1.1006), next_open=1.1010, equity=10_000, cfg=cfg)
    assert ok.ok and ok.sl_pips >= 8.0

    # A narrow zone right under the entry gives a 5-pip stop: below the floor
    tight = build_order(_signal(1.1007, 1.1009), next_open=1.1010, equity=10_000, cfg=cfg)
    assert not tight.ok
    assert tight.reason == "stop_too_tight_vs_spread"
    assert abs(tight.sl_pips - 5.0) < 1e-6

    # ...and the same setup is accepted once the floor is switched off
    cfg.trade.min_stop_spread_mult = 0.0
    assert build_order(_signal(1.1007, 1.1009), next_open=1.1010, equity=10_000, cfg=cfg).ok


# --------------------------------------------------------------------------- #
def test_lot_size_basic_case():
    cfg = test_config()
    cfg.risk.risk_per_trade = 0.005
    cfg.costs.commission_per_lot_round_turn = 0.0
    lots, risk = position_size(10_000.0, 0.0010, cfg)            # 10-pip stop
    assert abs(lots - 0.5) < 1e-9                                # $50 / $100 per lot
    assert abs(risk - 50.0) < 1e-6


def test_lot_size_accounts_for_commission():
    cfg = test_config()
    cfg.costs.commission_per_lot_round_turn = 7.0
    lots, _ = position_size(10_000.0, 0.0010, cfg)
    assert abs(lots - 0.46) < 1e-9                               # 50 / 107 -> floored


def test_lot_size_scales_with_equity_and_stop():
    cfg = test_config()
    cfg.costs.commission_per_lot_round_turn = 0.0
    a, _ = position_size(10_000.0, 0.0010, cfg)
    b, _ = position_size(20_000.0, 0.0010, cfg)
    c, _ = position_size(10_000.0, 0.0020, cfg)
    assert abs(b - 2 * a) < 1e-9
    assert abs(c - a / 2) < 1e-9


def test_lot_size_generalises_to_a_different_instrument():
    """USDJPY-style: 3 digits, pip 0.01, tick value in account currency."""
    cfg = test_config()
    cfg.symbol_spec = SymbolSpec(name="USDJPY", digits=3, point=0.001,
                                 tick_size=0.001, tick_value=0.68,
                                 contract_size=100_000)
    cfg.costs.commission_per_lot_round_turn = 0.0
    sl_distance = 0.10                                            # 10 pips
    lots, risk = position_size(10_000.0, sl_distance, cfg)
    expected_loss_per_lot = sl_distance * money_per_price_unit(cfg.symbol_spec)
    assert abs(lots * expected_loss_per_lot - risk) < 1e-6
    assert abs(risk - 50.0) < 1.0                                 # within one lot step


def test_zero_or_negative_stop_returns_no_position():
    cfg = test_config()
    assert position_size(10_000.0, 0.0, cfg) == (0.0, 0.0)
    assert position_size(10_000.0, -0.001, cfg) == (0.0, 0.0)


# --------------------------------------------------------------------------- #
def test_trade_is_rejected_when_stop_exceeds_20_pips():
    """The hard rule: reject, never compress."""
    cfg = test_config()
    plan = build_order(_signal(1.0980, 1.1006), next_open=1.1010, equity=10_000, cfg=cfg)
    assert not plan.ok and plan.reason == "sl_exceeds_max"
    assert plan.sl_pips > 20.0


def test_stop_at_exactly_20_pips_is_accepted():
    cfg = test_config()
    cfg.trade.sl_buffer_pips = 0.0
    plan = build_order(_signal(1.0990, 1.1006), next_open=1.1010, equity=10_000, cfg=cfg)
    assert abs(plan.sl_pips - 20.0) < 1e-6
    assert plan.ok


def test_stop_below_minimum_is_rejected():
    cfg = test_config()
    cfg.trade.min_sl_pips = 5.0
    cfg.trade.sl_buffer_pips = 0.0
    plan = build_order(_signal(1.1008, 1.1009), next_open=1.1010, equity=10_000, cfg=cfg)
    assert not plan.ok and plan.reason == "sl_below_min"


def test_short_order_geometry():
    cfg = test_config()
    plan = build_order(_signal(1.1000, 1.1006, Direction.SHORT), next_open=1.0995,
                       equity=10_000, cfg=cfg)
    assert plan.ok
    assert plan.sl > plan.entry > plan.tp
    assert abs((plan.entry - plan.tp) - 2 * (plan.sl - plan.entry)) < 1e-9


def test_wrong_side_stop_is_rejected():
    cfg = test_config()
    # long signal whose POI sits ABOVE the entry price -> nonsensical stop
    plan = build_order(_signal(1.1020, 1.1030), next_open=1.1010, equity=10_000, cfg=cfg)
    assert not plan.ok and plan.reason == "sl_wrong_side"


# --------------------------------------------------------------------------- #
def test_risk_gates_block_and_release():
    cfg = test_config()
    cfg.risk.max_trades_per_day = 2
    cfg.risk.max_open_positions = 1
    rm = RiskManager(cfg)
    rm.reset(10_000)
    ok, _ = rm.can_open(TS, 0, 0.5)
    assert ok
    rm.on_open()
    ok, reason = rm.can_open(TS, 0, 0.5)
    assert not ok and reason == "max_open_positions"
    rm.on_close(-50, 5)
    rm.on_open()
    rm.on_close(-50, 10)
    ok, reason = rm.can_open(TS, 11, 0.5)
    assert not ok and reason == "max_trades_per_day"
    ok, _ = rm.can_open(TS + pd.Timedelta(days=1), 12, 0.5)   # new day resets
    assert ok


def test_spread_gate():
    cfg = test_config()
    cfg.risk.max_spread_pips = 1.5
    rm = RiskManager(cfg)
    rm.reset(10_000)
    ok, reason = rm.can_open(TS, 0, 3.0)
    assert not ok and reason == "spread_too_wide"


def test_consecutive_loss_brake_resets_next_day():
    """Regression: without a daily reset the counter can never clear, because a
    halted strategy can never produce the win that would clear it."""
    cfg = test_config()
    cfg.risk.max_consecutive_losses = 2
    rm = RiskManager(cfg)
    rm.reset(10_000)
    rm.can_open(TS, 0, 0.5)
    for k in range(2):
        rm.on_open()
        rm.on_close(-50, k)
    ok, reason = rm.can_open(TS, 5, 0.5)
    assert not ok and reason == "consecutive_losses"
    ok, _ = rm.can_open(TS + pd.Timedelta(days=1), 6, 0.5)
    assert ok


def test_daily_loss_halt():
    cfg = test_config()
    cfg.risk.max_daily_loss_pct = 0.02
    rm = RiskManager(cfg)
    rm.reset(10_000)
    rm.can_open(TS, 0, 0.5)
    rm.on_open()
    rm.on_close(-250, 1)                                   # -2.5 %
    ok, reason = rm.can_open(TS, 2, 0.5)
    assert not ok and reason == "daily_loss_halt"
