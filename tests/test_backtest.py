"""Backtester tests, centred on the intrabar SL-vs-TP problem (§13) and on
proving that the entry happens on the bar AFTER the signal."""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading_bot.backtest import (Backtester, M1Execution, M5Execution, Trade,
                                  _OpenPosition)
from trading_bot.config import Direction, SameBarPolicy
from trading_bot.data import build_aligned
from trading_bot.metrics import breakeven_win_rate, summarise
from trading_bot.risk import OrderPlan
from tests.fixtures import long_setup_bars, m1_frame, m5_frame, test_config

TS = pd.Timestamp("2024-01-01 00:00", tz="UTC")


def _position(entry=1.1000, sl=1.0990, tp=1.1020, direction=Direction.LONG,
              entry_index=1) -> _OpenPosition:
    t = Trade(trade_id=1, symbol="EURUSD", direction=direction.value,
              signal_time=TS, entry_time=TS, entry=entry, sl=sl, tp=tp,
              sl_distance=abs(entry - sl), sl_pips=abs(entry - sl) / 0.0001,
              risk_amount=50.0, volume=0.5)
    return _OpenPosition(trade=t, plan=OrderPlan(True), entry_index=entry_index,
                         mfe_price=entry, mae_price=entry)


# --------------------------------------------------------------------------- #
# Intrabar resolution
# --------------------------------------------------------------------------- #
def test_m5_same_bar_policies_disagree_by_construction():
    cfg = test_config()
    ex = M5Execution(cfg)
    o, h, l = 1.1000, 1.1025, 1.0985            # bar contains BOTH levels
    pos = _position()
    assert ex._check(pos, o, h, l, SameBarPolicy.SL_FIRST)[1] == "sl_pessimistic"
    assert ex._check(_position(), o, h, l, SameBarPolicy.TP_FIRST)[1] == "tp_optimistic"
    assert ex._check(_position(), o, h, l, SameBarPolicy.PROPORTIONAL)[1] == "ambiguous_bar"
    assert ex.ambiguous_bars == 3                # the counter notices every time


def test_m1_resolves_an_ambiguous_m5_bar_correctly():
    """One M5 bar whose range spans SL and TP; the minute data shows the stop
    was hit first, so the M1 model must return SL."""
    cfg = test_config()
    m5 = m5_frame([(1.1000, 1.1010, 1.0995, 1.1005),
                   (1.1000, 1.1025, 1.0985, 1.1020)])
    minutes = [(1.1000, 1.1002, 1.0999, 1.1001)] * 5 + [
        (1.1000, 1.1001, 1.0984, 1.0986),        # minute 6: stop hit
        (1.0986, 1.1026, 1.0985, 1.1024),        # minute 7: target would be hit
        (1.1024, 1.1025, 1.1020, 1.1022),
        (1.1022, 1.1023, 1.1018, 1.1020),
        (1.1020, 1.1021, 1.1017, 1.1019)]
    m1 = m1_frame(minutes)
    aligned = build_aligned(m5, m1)
    pos = _position(entry_index=1)
    pos.m1_cursor = int(aligned.m1_start[1])
    res = M1Execution(cfg).progress(pos, 1, aligned)
    assert res is not None
    _, price, reason = res
    assert reason == "sl"
    assert abs(price - 1.0990) < 1e-9


def test_m1_and_m5_agree_when_only_one_level_is_touched():
    cfg = test_config()
    m5 = m5_frame([(1.1000, 1.1010, 1.0995, 1.1005),
                   (1.1000, 1.1025, 1.0998, 1.1020)])
    m1 = m1_frame([(1.1000, 1.1005, 1.0999, 1.1004)] * 5
                  + [(1.1000, 1.1026, 1.0998, 1.1024)] * 5)
    aligned = build_aligned(m5, m1)
    p1, p5 = _position(entry_index=1), _position(entry_index=1)
    p1.m1_cursor = int(aligned.m1_start[1])
    r1 = M1Execution(cfg).progress(p1, 1, aligned)
    r5 = M5Execution(cfg).progress(p5, 1, aligned)
    assert r1[2] == r5[2] == "tp"
    assert abs(r1[1] - r5[1]) < 1e-9


def test_gap_through_the_stop_fills_at_the_open_not_the_level():
    cfg = test_config()
    ex = M5Execution(cfg)
    pos = _position()
    price, reason = ex._check(pos, 1.0980, 1.0982, 1.0975, SameBarPolicy.SL_FIRST)
    assert reason == "sl"
    assert abs(price - 1.0980) < 1e-9            # gapped open, worse than the stop


def test_short_exit_pays_the_spread_on_the_buy_back():
    cfg = test_config()
    cfg.costs.spread_pips = 1.0
    ex = M5Execution(cfg)
    pos = _position(entry=1.1000, sl=1.1010, tp=1.0980, direction=Direction.SHORT)
    # bid high 1.10095 -> ask high 1.10105 -> stop IS hit even though bid never was
    res = ex._check(pos, 1.1000, 1.10095, 1.0999, SameBarPolicy.SL_FIRST)
    assert res is not None and res[1] == "sl"


def test_stop_slippage_worsens_the_fill():
    cfg = test_config()
    cfg.costs.slippage_stop_pips = 0.5
    ex = M5Execution(cfg)
    price, reason = ex._check(_position(), 1.1000, 1.1005, 1.0985, SameBarPolicy.SL_FIRST)
    assert reason == "sl"
    assert abs(price - (1.0990 - 0.00005)) < 1e-9


# --------------------------------------------------------------------------- #
# Full loop
# --------------------------------------------------------------------------- #
def _bars_with_outcome(hit: str):
    bars = long_setup_bars()
    if hit == "tp":                                   # entry 1.1010 -> TP 1.1032
        bars += [(1.10180, 1.10340, 1.10170, 1.10330)] + [(1.10330, 1.10340, 1.10320, 1.10330)] * 2
    else:                                             # SL 1.0999
        bars += [(1.10180, 1.10190, 1.09980, 1.09990)] + [(1.09990, 1.10000, 1.09980, 1.09990)] * 2
    return bars


def test_entry_happens_on_the_bar_after_the_signal():
    cfg = test_config()
    aligned = build_aligned(m5_frame(_bars_with_outcome("tp")))
    res = Backtester(cfg, log_events=False).run(aligned)
    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert pd.Timestamp(t["entry_time"]) == aligned.m5["timestamp"].iloc[31]
    assert abs(t["entry"] - aligned.m5["open"].iloc[31]) < 1e-9
    assert pd.Timestamp(t["signal_time"]) == aligned.m5["bar_end"].iloc[30]


def test_take_profit_outcome_is_two_r():
    cfg = test_config()
    aligned = build_aligned(m5_frame(_bars_with_outcome("tp")))
    t = Backtester(cfg, log_events=False).run(aligned).trades.iloc[0]
    assert t["exit_reason"] == "tp"
    assert abs(t["r_multiple"] - 2.0) < 0.05
    assert t["pnl"] > 0


def test_stop_loss_outcome_is_minus_one_r():
    cfg = test_config()
    aligned = build_aligned(m5_frame(_bars_with_outcome("sl")))
    t = Backtester(cfg, log_events=False).run(aligned).trades.iloc[0]
    assert t["exit_reason"] == "sl"
    assert abs(t["r_multiple"] + 1.0) < 0.05


def test_costs_reduce_the_realised_r():
    base = test_config()
    costly = test_config()
    costly.costs.spread_pips = 1.5
    costly.costs.slippage_entry_pips = 0.5
    aligned = build_aligned(m5_frame(_bars_with_outcome("tp")))
    a = Backtester(base, log_events=False).run(aligned).trades.iloc[0]
    b = Backtester(costly, log_events=False).run(aligned).trades.iloc[0]
    assert b["entry"] > a["entry"]
    assert b["pnl"] < a["pnl"]


def test_position_is_sized_from_equity_and_stop():
    cfg = test_config()
    aligned = build_aligned(m5_frame(_bars_with_outcome("sl")))
    t = Backtester(cfg, log_events=False).run(aligned).trades.iloc[0]
    expected = 10_000 * cfg.risk.risk_per_trade / (t["sl_distance"] * 100_000)
    assert abs(t["volume"] - np.floor(expected * 100) / 100) < 1e-9
    assert abs(t["risk_amount"] - 50.0) < 5.0


def test_trade_record_carries_the_full_provenance():
    cfg = test_config()
    aligned = build_aligned(m5_frame(_bars_with_outcome("tp")))
    t = Backtester(cfg, log_events=False).run(aligned).trades.iloc[0]
    for field in ("poi_id", "poi_type", "poi_high", "poi_low", "poi_time",
                  "retest_time", "engulf_time", "signal_time", "entry_time",
                  "exit_time", "mfe_r", "mae_r", "exit_reason", "sl_pips"):
        assert field in t.index
    assert t["poi_high"] > t["poi_low"]
    assert pd.Timestamp(t["poi_time"]) < pd.Timestamp(t["retest_time"])
    assert pd.Timestamp(t["retest_time"]) <= pd.Timestamp(t["engulf_time"])


def test_no_trade_when_the_stop_would_exceed_the_cap():
    cfg = test_config()
    cfg.trade.max_sl_pips = 5.0                       # fixture needs ~11 pips
    aligned = build_aligned(m5_frame(_bars_with_outcome("tp")))
    res = Backtester(cfg, log_events=True).run(aligned)
    assert len(res.trades) == 0
    assert res.rejections.get("sl_exceeds_max", 0) == 1


# --------------------------------------------------------------------------- #
def test_breakeven_win_rate_arithmetic():
    assert abs(breakeven_win_rate(2.0, 0.0) - 1 / 3) < 1e-12
    # a 1-pip cost on a 10-pip stop is 0.1R and lifts the required win rate
    assert abs(breakeven_win_rate(2.0, 0.1) - 1.1 / 3) < 1e-12


def test_summary_reports_uncertainty_not_just_point_estimates():
    cfg = test_config()
    aligned = build_aligned(m5_frame(_bars_with_outcome("tp")))
    res = Backtester(cfg, log_events=False).run(aligned)
    s = summarise(res.trades, cfg, res.equity)
    for key in ("expectancy_r", "p_value", "r_ci_low", "r_ci_high",
                "n_for_significance", "max_drawdown_pct"):
        assert key in s
