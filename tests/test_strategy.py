"""Strategy-engine tests: POI creation, invalidation, retest, duplicate
suppression, and an explicit look-ahead regression test."""

from __future__ import annotations

import pandas as pd

from trading_bot.config import Direction, POIType
from trading_bot.data import build_aligned
from trading_bot.logger import EventLog
from trading_bot.strategy import (CREATED, DEPARTED, EXPIRED, INVALID, RETESTED,
                                  TRIGGERED, POIDetector, StrategyEngine)
from tests.fixtures import long_setup_bars, m15_frame, m5_frame, test_config


def _run(bars, cfg=None):
    cfg = cfg or test_config()
    aligned = build_aligned(m5_frame(bars))
    eng = StrategyEngine(cfg, EventLog(enabled=True))
    eng.prepare(aligned)
    signals = []
    for i in range(len(aligned.m5)):
        signals.extend(eng.step(i))
    return eng, signals


# --------------------------------------------------------------------------- #
# POI detection
# --------------------------------------------------------------------------- #
def test_order_block_is_the_origin_candle_of_the_displacement_leg():
    cfg = test_config()
    m15 = m15_frame([
        (1.1000, 1.1006, 1.0994, 1.1000),   # 0 warm-up
        (1.1000, 1.1006, 1.0994, 1.1000),   # 1
        (1.1000, 1.1006, 1.0994, 1.1000),   # 2
        (1.1000, 1.1006, 1.0994, 1.1000),   # 3
        (1.1000, 1.1006, 1.0994, 1.1000),   # 4
        (1.1000, 1.1006, 1.0994, 1.1000),   # 5
        (1.1005, 1.1006, 1.1000, 1.1001),   # 6 <- bearish origin candle
        (1.1001, 1.1030, 1.1000, 1.1029),   # 7 <- displacement
    ])
    det = POIDetector(cfg)
    det.prepare(m15)
    assert det.detect(6, 20) == []                      # nothing confirmed yet
    pois = det.detect(7, 23)
    assert len(pois) == 1
    poi = pois[0]
    assert poi.direction is Direction.LONG
    assert poi.poi_type is POIType.ORDER_BLOCK
    assert abs(poi.lower - 1.1000) < 1e-9               # low of the origin candle
    assert abs(poi.upper - 1.1006) < 1e-9               # high of the origin candle
    assert poi.origin_time == m15["timestamp"].iloc[6]  # zone comes from bar 6 ...
    assert poi.created_time == m15["bar_end"].iloc[7]   # ... but is KNOWN at bar 7's close
    assert poi.strength > 1.0


def test_weak_displacement_creates_no_poi():
    cfg = test_config()
    cfg.poi.displacement_atr_mult = 3.0
    m15 = m15_frame([(1.1000, 1.1006, 1.0994, 1.1000)] * 6
                    + [(1.1005, 1.1006, 1.1000, 1.1001),
                       (1.1001, 1.1010, 1.1000, 1.1008)])
    det = POIDetector(cfg)
    det.prepare(m15)
    assert det.detect(7, 23) == []


def test_leg_must_close_beyond_the_origin_candle_high():
    cfg = test_config()
    m15 = m15_frame([(1.1000, 1.1006, 1.0994, 1.1000)] * 6
                    + [(1.1005, 1.1030, 1.1000, 1.1001),   # tall wick, bearish
                       (1.1001, 1.1029, 1.1000, 1.1028)])  # closes below high[o]
    det = POIDetector(cfg)
    det.prepare(m15)
    assert det.detect(7, 23) == []


def test_same_origin_candle_is_never_registered_twice():
    cfg = test_config()
    m15 = m15_frame([(1.1000, 1.1006, 1.0994, 1.1000)] * 6
                    + [(1.1005, 1.1006, 1.1000, 1.1001),
                       (1.1001, 1.1030, 1.1000, 1.1029),
                       (1.1029, 1.1045, 1.1028, 1.1044)])  # leg continues
    det = POIDetector(cfg)
    det.prepare(m15)
    assert len(det.detect(7, 23)) == 1
    assert det.detect(8, 26) == []                        # same origin -> ignored


def test_zone_wider_than_the_stop_budget_is_discarded():
    cfg = test_config()
    cfg.poi.max_zone_width_pips = 8.0
    m15 = m15_frame([(1.1000, 1.1006, 1.0994, 1.1000)] * 6
                    + [(1.1015, 1.1016, 1.0990, 1.0991),   # 26-pip zone
                       (1.0991, 1.1060, 1.0990, 1.1055)])
    det = POIDetector(cfg)
    det.prepare(m15)
    assert det.detect(7, 23) == []


def test_fvg_poi_alternative():
    cfg = test_config()
    cfg.poi.poi_type = POIType.FVG
    m15 = m15_frame([(1.1000, 1.1006, 1.0994, 1.1000)] * 6
                    + [(1.1000, 1.1010, 1.0999, 1.1009),
                       (1.1009, 1.1030, 1.1008, 1.1029),
                       (1.1029, 1.1040, 1.1020, 1.1035)])  # low 1.1020 > high[j-2] 1.1010
    det = POIDetector(cfg)
    det.prepare(m15)
    pois = det.detect(8, 26)
    assert len(pois) == 1 and pois[0].direction is Direction.LONG
    assert abs(pois[0].lower - 1.1010) < 1e-9 and abs(pois[0].upper - 1.1020) < 1e-9


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def test_full_long_setup_produces_exactly_one_signal():
    eng, signals = _run(long_setup_bars())
    assert len(signals) == 1
    s = signals[0]
    assert s.direction is Direction.LONG
    assert abs(s.poi.lower - 1.1000) < 1e-9 and abs(s.poi.upper - 1.1006) < 1e-9
    assert s.signal_index == 30
    assert s.poi.retest_time is not None
    assert s.poi.retest_time < s.signal_time or s.poi.retest_index == 29


def test_retest_precedes_the_engulfing_bar():
    _, signals = _run(long_setup_bars())
    s = signals[0]
    assert s.poi.retest_index <= s.signal_index
    assert s.poi.departed_index < s.poi.retest_index


def test_no_departure_means_no_signal():
    cfg = test_config()
    cfg.retest.min_departure_pips = 60.0            # unreachable in the fixture
    _, signals = _run(long_setup_bars(), cfg)
    assert signals == []


def test_close_beyond_the_distal_boundary_invalidates_the_poi():
    bars = long_setup_bars()
    # M5 #26 closes 3 pips below the zone low (1.1000)
    bars[26] = (1.10260, 1.10280, 1.09960, 1.09970)
    eng, signals = _run(bars)
    assert signals == []
    # invalidated POIs are pruned from the live list, so check the event stream
    reasons = [e.get("reason") for e in eng.events.events if e["event"] == "poi_dead"]
    assert "close_below_zone" in reasons


def test_engulfing_outside_the_confirmation_window_is_ignored():
    cfg = test_config()
    cfg.engulf.max_m5_bars_after_retest = 0         # only the retest bar itself
    _, signals = _run(long_setup_bars(), cfg)
    assert signals == []


def test_engulfing_away_from_the_zone_is_rejected():
    cfg = test_config()
    cfg.engulf.zone_proximity_pips = 0.0
    bars = long_setup_bars()
    # lift the engulfing bar so it no longer touches the zone at all
    bars[30] = (1.10100, 1.10165, 1.10095, 1.10160)
    bars[29] = (1.10140, 1.10145, 1.10050, 1.10105)
    _, signals = _run(bars, cfg)
    assert signals == []


def test_poi_is_consumed_after_triggering():
    """Duplicate-signal prevention: one POI can produce at most one entry."""
    bars = long_setup_bars()
    # append a second, identical engulfing sequence right after the first
    bars += [(1.10180, 1.10185, 1.10040, 1.10050),
             (1.10040, 1.10110, 1.10030, 1.10105),
             (1.10105, 1.10150, 1.10100, 1.10140)]
    eng, signals = _run(bars)
    assert len(signals) == 1
    # a triggered POI is consumed: it leaves the active list and can never
    # produce a second entry, even though a second valid engulfing follows
    assert all(p.poi_id != signals[0].poi.poi_id for p in eng.pois)
    engulfs = [e for e in eng.events.events if e["event"] == "engulf"]
    assert len(engulfs) == 1


def test_poi_expires_after_max_age():
    cfg = test_config()
    cfg.poi.expiry_m15_bars = 1                      # 3 M5 bars
    _, signals = _run(long_setup_bars(), cfg)
    assert signals == []


def test_short_setup_is_the_mirror_image():
    """Same fixture reflected around 1.1000 must give a SHORT signal."""
    bars = [(2.2000 - o, 2.2000 - l, 2.2000 - h, 2.2000 - c)
            for (o, h, l, c) in long_setup_bars()]
    _, signals = _run(bars)
    assert len(signals) == 1
    assert signals[0].direction is Direction.SHORT


# --------------------------------------------------------------------------- #
# Look-ahead regression
# --------------------------------------------------------------------------- #
def test_signal_does_not_depend_on_future_bars():
    """Truncating the series immediately after the signal must not change it.

    This is the test that catches accidental use of a not-yet-closed M15 bar or
    an off-by-one in the alignment map.
    """
    bars = long_setup_bars()
    _, full = _run(bars)
    assert len(full) == 1
    cut = full[0].signal_index + 1
    _, truncated = _run(bars[:cut])
    assert len(truncated) == 1
    assert truncated[0].signal_index == full[0].signal_index
    assert truncated[0].poi.lower == full[0].poi.lower
    assert truncated[0].poi.upper == full[0].poi.upper


def test_poi_never_created_before_its_confirming_bar_closes():
    eng, _ = _run(long_setup_bars())
    m15 = eng.aligned.m15
    for poi in eng.pois:
        assert poi.created_time in set(m15["bar_end"])
        assert poi.origin_time < poi.created_time
