"""Market data: ingestion, hygiene, resampling and *causal* multi-timeframe
alignment.

Design decision (see SPECIFICATION.md §F): M15 candles are built by resampling
M5 candles rather than downloaded separately.  Two reasons:

1.  A broker's M15 series and its M5 series can disagree at session boundaries
    and around missing ticks.  Resampling guarantees the M15 bar is exactly the
    aggregate of the M5 bars the strategy also sees.
2.  It makes the "when did I know this?" question trivial: an M15 bar labelled
    10:00 is complete only once the M5 bar labelled 10:10 has closed, i.e. at
    10:15:00.  The alignment map built here enforces that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

OHLCV = ["open", "high", "low", "close", "volume"]

_COLUMN_ALIASES = {
    "time": "timestamp", "date": "timestamp", "datetime": "timestamp",
    "<date>": "date_part", "<time>": "time_part",
    "<open>": "open", "<high>": "high", "<low>": "low", "<close>": "close",
    "<tickvol>": "volume", "<vol>": "real_volume", "<spread>": "spread",
    "tick_volume": "volume", "o": "open", "h": "high", "l": "low", "c": "close",
    "v": "volume",
}


class DataQualityError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_csv(path: str, tz: str = "UTC", broker_utc_offset_hours: float = 0.0,
             start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """Load an OHLCV CSV (MT5 export or generic) into a clean UTC frame."""
    df = pd.read_csv(path, sep=None, engine="python")
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={c: _COLUMN_ALIASES.get(c, c) for c in df.columns})

    if "timestamp" not in df.columns:
        if {"date_part", "time_part"} <= set(df.columns):
            df["timestamp"] = (df["date_part"].astype(str).str.replace(".", "-", regex=False)
                               + " " + df["time_part"].astype(str))
        else:
            raise DataQualityError(f"{path}: no timestamp column found")

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=False, errors="coerce")
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(tz)
    df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")
    if broker_utc_offset_hours:
        df["timestamp"] = df["timestamp"] - pd.Timedelta(hours=broker_utc_offset_hours)

    if "volume" not in df.columns:
        df["volume"] = 0.0
    keep = ["timestamp"] + OHLCV + (["spread"] if "spread" in df.columns else [])
    df = df[keep]
    df = clean_ohlcv(df)
    if start:
        df = df[df["timestamp"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df["timestamp"] <= pd.Timestamp(end, tz="UTC")]
    return df.reset_index(drop=True)


def load_mt5(symbol: str, timeframe: str, start: str, end: str,
             broker_utc_offset_hours: float = 0.0) -> pd.DataFrame:
    """Pull history straight from a running MT5 terminal (Windows only)."""
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "MetaTrader5 package unavailable (Windows-only). "
            "Export CSVs instead - see README 'Obtaining MT5 data'."
        ) from exc

    tf_map = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
              "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}
    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        rates = mt5.copy_rates_range(symbol, tf_map[timeframe],
                                     pd.Timestamp(start).to_pydatetime(),
                                     pd.Timestamp(end).to_pydatetime())
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"no rates returned: {mt5.last_error()}")
        df = pd.DataFrame(rates)
    finally:
        mt5.shutdown()

    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    if broker_utc_offset_hours:
        df["timestamp"] = df["timestamp"] - pd.Timedelta(hours=broker_utc_offset_hours)
    df = df.rename(columns={"tick_volume": "volume"})
    cols = ["timestamp"] + OHLCV + (["spread"] if "spread" in df.columns else [])
    return clean_ohlcv(df[cols]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Hygiene
# --------------------------------------------------------------------------- #
def clean_ohlcv(df: pd.DataFrame, drop_bad: bool = True) -> pd.DataFrame:
    """Sort, de-duplicate and validate.  Bad bars are dropped, never repaired:
    silently 'fixing' a broken high/low hides feed problems that also exist live.
    """
    df = df.copy()
    for c in OHLCV:
        if c not in df.columns:
            raise DataQualityError(f"missing column '{c}'")
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["timestamp"] + OHLCV[:4])
    df = df.sort_values("timestamp", kind="mergesort")
    df = df.drop_duplicates(subset="timestamp", keep="last")

    bad = (
        (df["high"] < df["low"])
        | (df["high"] < df[["open", "close"]].max(axis=1) - 1e-12)
        | (df["low"] > df[["open", "close"]].min(axis=1) + 1e-12)
        | (df[OHLCV[:4]] <= 0).any(axis=1)
    )
    if bad.any() and drop_bad:
        df = df[~bad]
    return df.reset_index(drop=True)


def data_report(df: pd.DataFrame, expected_minutes: int,
                max_gap_minutes: int = 90) -> Dict[str, object]:
    """Summarise coverage and gaps.  Missing candles are *not* filled: a
    fabricated flat bar can create a fake engulfing pattern out of nothing.
    """
    ts = df["timestamp"]
    deltas = ts.diff().dt.total_seconds().div(60).dropna()
    gaps = deltas[deltas > expected_minutes]
    return {
        "rows": int(len(df)),
        "first": ts.iloc[0] if len(df) else None,
        "last": ts.iloc[-1] if len(df) else None,
        "missing_bars_est": int((deltas[deltas <= max_gap_minutes] / expected_minutes - 1)
                                .clip(lower=0).sum()),
        "gap_count": int(len(gaps)),
        "closure_count": int((deltas > max_gap_minutes).sum()),
        "largest_gap_minutes": float(deltas.max()) if len(deltas) else 0.0,
        "duplicate_timestamps": int(df["timestamp"].duplicated().sum()),
    }


# --------------------------------------------------------------------------- #
# Resampling & alignment
# --------------------------------------------------------------------------- #
def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate to a higher timeframe.  label='left', closed='left' so a bar's
    timestamp is its OPEN time - the MT5 convention.
    """
    s = df.set_index("timestamp")
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    out = s.resample(f"{minutes}min", label="left", closed="left").agg(agg)
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out.reset_index()
    out["bar_end"] = out["timestamp"] + pd.Timedelta(minutes=minutes)
    return out


@dataclass
class AlignedData:
    """Everything the engine iterates over, plus the causal index maps.

    The ``*_arr`` fields are plain numpy views built once.  They exist because
    ``df.iloc[i]`` in an inner loop that runs a few million times turns a
    two-second backtest into a two-minute one, and because ``.to_numpy()`` on a
    timezone-aware column silently iterates element by element.  Timestamps are
    carried as int64 nanoseconds and converted only when something is actually
    logged.
    """
    m5: pd.DataFrame
    m15: pd.DataFrame
    m1: Optional[pd.DataFrame]
    m15_ready: np.ndarray   # m15_ready[i] = idx of last M15 bar CLOSED at close of M5 bar i (-1 if none)
    m1_start: Optional[np.ndarray] = None  # m1_start[i] = idx of first M1 bar at/after M5 bar i open

    def __post_init__(self) -> None:
        self.m5_arr = _arrays(self.m5)
        self.m1_arr = _arrays(self.m1) if self.m1 is not None and len(self.m1) else None

    def m15_slice(self, i5: int) -> int:
        return int(self.m15_ready[i5])


def _to_ns(col: pd.Series) -> np.ndarray:
    """Datetime column -> int64 nanoseconds.

    ``col.astype("int64")`` is NOT safe: pandas 3 stores datetimes as
    ``datetime64[us]`` by default, so that cast silently yields microseconds and
    every timestamp in the trade log comes out in 1970.  The resolution is
    forced here instead.
    """
    return (col.dt.tz_convert("UTC").dt.tz_localize(None)
               .to_numpy(dtype="datetime64[ns]").astype("int64"))


def _arrays(df: pd.DataFrame) -> Dict[str, np.ndarray]:
    out = {c: df[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close")}
    out["ts"] = _to_ns(df["timestamp"])
    out["end"] = _to_ns(df["bar_end"]) if "bar_end" in df.columns else out["ts"]
    return out


def ts_at(ns: int) -> pd.Timestamp:
    """int64 nanoseconds -> tz-aware Timestamp (UTC)."""
    return pd.Timestamp(int(ns), tz="UTC")


def build_aligned(m5: pd.DataFrame, m1: Optional[pd.DataFrame] = None) -> AlignedData:
    """Build M15 from M5 and compute the look-ahead-safe alignment map.

    ``m15_ready[i]`` answers: standing at the *close* of M5 bar ``i``
    (wall-clock = open_i + 5min), which is the most recent M15 bar I am allowed
    to have seen?  Answer: the last one whose ``bar_end`` is <= that instant.
    """
    m5 = m5.reset_index(drop=True).copy()
    m5["bar_end"] = m5["timestamp"] + pd.Timedelta(minutes=5)
    m15 = resample(m5[["timestamp"] + OHLCV], 15)

    m5_end = m5["bar_end"].to_numpy()
    m15_end = m15["bar_end"].to_numpy()
    # side='right' -> count of M15 bars with bar_end <= m5_end; minus 1 -> index
    m15_ready = np.searchsorted(m15_end, m5_end, side="right") - 1

    m1_start = None
    if m1 is not None and len(m1):
        m1 = m1.reset_index(drop=True)
        m1_start = np.searchsorted(m1["timestamp"].to_numpy(),
                                   m5["timestamp"].to_numpy(), side="left")
    return AlignedData(m5=m5, m15=m15, m1=m1, m15_ready=m15_ready, m1_start=m1_start)


def slice_period(aligned: AlignedData, start: pd.Timestamp,
                 end: pd.Timestamp) -> AlignedData:
    """Re-slice for walk-forward windows (rebuilds maps; never reuses stale ones)."""
    m5 = aligned.m5[(aligned.m5["timestamp"] >= start) &
                    (aligned.m5["timestamp"] < end)].reset_index(drop=True)
    m1 = None
    if aligned.m1 is not None:
        m1 = aligned.m1[(aligned.m1["timestamp"] >= start) &
                        (aligned.m1["timestamp"] < end + pd.Timedelta(days=3))
                        ].reset_index(drop=True)
    return build_aligned(m5[["timestamp"] + OHLCV], m1)
