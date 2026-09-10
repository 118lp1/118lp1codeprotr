"""Data-layer tests.

The alignment test is the single most important test in this repository: if
``m15_ready`` is off by one bar, the strategy silently trades on information it
could not have had, and every downstream number becomes fiction.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pandas as pd

from trading_bot.data import (build_aligned, clean_ohlcv, data_report, load_csv,
                              resample)
from tests.fixtures import m5_frame


def _ramp(n: int):
    return [(1.1000 + i * 1e-4, 1.1000 + i * 1e-4 + 3e-4,
             1.1000 + i * 1e-4 - 3e-4, 1.1000 + i * 1e-4 + 1e-4) for i in range(n)]


def test_clean_drops_duplicates_and_sorts():
    df = m5_frame(_ramp(4))
    dup = pd.concat([df, df.iloc[[2]]], ignore_index=True).sample(frac=1, random_state=1)
    out = clean_ohlcv(dup)
    assert len(out) == 4
    assert out["timestamp"].is_monotonic_increasing


def test_clean_drops_impossible_bars():
    df = m5_frame(_ramp(4))
    df.loc[2, "high"] = df.loc[2, "low"] - 0.001      # high below low
    out = clean_ohlcv(df)
    assert len(out) == 3


def test_resample_m5_to_m15_aggregates_correctly():
    df = m5_frame(_ramp(6))
    m15 = resample(df, 15)
    assert len(m15) == 2
    assert m15["open"].iloc[0] == df["open"].iloc[0]
    assert m15["close"].iloc[0] == df["close"].iloc[2]
    assert m15["high"].iloc[0] == df["high"].iloc[:3].max()
    assert m15["low"].iloc[0] == df["low"].iloc[:3].min()
    # bar_end is the instant the bar becomes knowable
    assert m15["bar_end"].iloc[0] == m15["timestamp"].iloc[0] + pd.Timedelta(minutes=15)


def test_alignment_never_reveals_an_unclosed_m15_bar():
    df = m5_frame(_ramp(60))
    a = build_aligned(df)
    m5_end = a.m5["bar_end"].to_numpy()
    m15_end = a.m15["bar_end"].to_numpy()
    for i in range(len(a.m5)):
        j = int(a.m15_ready[i])
        if j >= 0:
            assert m15_end[j] <= m5_end[i], f"look-ahead at i={i}"
        if j + 1 < len(m15_end):
            assert m15_end[j + 1] > m5_end[i], f"stale M15 at i={i}"


def test_alignment_boundary_is_exact():
    """M15 bar 00:00 closes at 00:15, i.e. at the close of the M5 bar stamped
    00:10 - and NOT at the close of the M5 bar stamped 00:05."""
    df = m5_frame(_ramp(9))
    a = build_aligned(df)
    assert a.m15_ready[0] == -1      # 00:00 M5 closes at 00:05: nothing known yet
    assert a.m15_ready[1] == -1      # 00:05 M5 closes at 00:10: still nothing
    assert a.m15_ready[2] == 0       # 00:10 M5 closes at 00:15: first M15 known
    assert a.m15_ready[3] == 0
    assert a.m15_ready[5] == 1


def test_m1_index_map_points_at_the_right_minute():
    m5 = m5_frame(_ramp(4))
    m1 = m5_frame(_ramp(20))
    m1["timestamp"] = pd.date_range(m5["timestamp"].iloc[0], periods=20, freq="1min")
    a = build_aligned(m5, m1)
    assert a.m1_start is not None
    assert list(a.m1_start[:4]) == [0, 5, 10, 15]


def test_timezone_conversion_from_broker_local_time():
    df = m5_frame(_ramp(3))
    df["timestamp"] = df["timestamp"].dt.tz_localize(None)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.csv")
        df.to_csv(p, index=False)
        utc = load_csv(p, tz="UTC")
        berlin = load_csv(p, tz="Europe/Berlin")
        offset = load_csv(p, tz="UTC", broker_utc_offset_hours=3.0)
    # 00:00 Berlin (CET, UTC+1) is 23:00 UTC the previous day
    assert berlin["timestamp"].iloc[0] == utc["timestamp"].iloc[0] - pd.Timedelta(hours=1)
    assert offset["timestamp"].iloc[0] == utc["timestamp"].iloc[0] - pd.Timedelta(hours=3)
    assert str(utc["timestamp"].dt.tz) == "UTC"


def test_data_report_flags_gaps_and_closures():
    df = m5_frame(_ramp(10))
    df = pd.concat([df.iloc[:5], df.iloc[7:]], ignore_index=True)   # 10-min hole
    rep = data_report(df, 5, max_gap_minutes=90)
    assert rep["gap_count"] == 1
    assert rep["missing_bars_est"] == 2
    assert rep["duplicate_timestamps"] == 0


def test_missing_bars_are_not_fabricated():
    """A synthetic flat bar can invent an engulfing pattern; we drop, never fill."""
    df = m5_frame(_ramp(10))
    holed = pd.concat([df.iloc[:5], df.iloc[7:]], ignore_index=True)
    a = build_aligned(holed)
    assert len(a.m5) == 8
