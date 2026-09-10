"""Strategy engine: M15 POI -> departure -> retest -> M5 engulfing -> signal.

The whole file obeys one rule: **a decision taken at the close of M5 bar i may
only read data with a timestamp <= the close of bar i.**  Everything that could
violate that is routed through ``AlignedData.m15_ready`` (see data.py) or
through an explicit confirmation delay.

State machine per POI
---------------------
    CREATED --(departure satisfied)--> DEPARTED --(price touches zone)--> RETESTED
    RETESTED --(engulfing within window)--> TRIGGERED   (signal emitted)
    any state --(close beyond distal | expiry | too deep)--> INVALID / EXPIRED
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .config import Config, Direction, POIType
from .data import AlignedData, ts_at
from .indicators import Candle, atr, is_engulfing, swing_flags
from .logger import EventLog
from .utils import pip_size

# POI lifecycle states
CREATED, DEPARTED, RETESTED, TRIGGERED, INVALID, EXPIRED = (
    "created", "departed", "retested", "triggered", "invalid", "expired")


# --------------------------------------------------------------------------- #
@dataclass
class POI:
    poi_id: str
    direction: Direction
    poi_type: POIType
    upper: float                    # upper price boundary
    lower: float                    # lower price boundary
    origin_time: pd.Timestamp       # timestamp of the candle that forms the zone
    created_time: pd.Timestamp      # instant it became KNOWN (= M15 bar_end)
    created_m5_index: int = -1
    strength: float = 0.0           # impulse size in ATR units
    state: str = CREATED
    # --- lifecycle bookkeeping ---
    bars_outside: int = 0
    max_departure_pips: float = 0.0
    departed_time: Optional[pd.Timestamp] = None
    departed_index: int = -1
    retest_time: Optional[pd.Timestamp] = None
    retest_index: int = -1
    retest_count: int = 0
    dead_reason: str = ""

    @property
    def height(self) -> float:
        return self.upper - self.lower

    @property
    def proximal(self) -> float:
        """Boundary price approaches first on the retest."""
        return self.upper if self.direction is Direction.LONG else self.lower

    @property
    def distal(self) -> float:
        """Boundary the stop-loss sits beyond."""
        return self.lower if self.direction is Direction.LONG else self.upper

    @property
    def is_active(self) -> bool:
        return self.state in (CREATED, DEPARTED, RETESTED)


@dataclass
class Signal:
    signal_time: pd.Timestamp       # close instant of the engulfing bar
    signal_index: int               # M5 index of the engulfing bar
    direction: Direction
    poi: POI
    engulf_open: float
    engulf_high: float
    engulf_low: float
    engulf_close: float
    engulf_time: pd.Timestamp       # OPEN time of the engulfing bar


# --------------------------------------------------------------------------- #
# POI detection (runs at the close of each completed M15 bar)
# --------------------------------------------------------------------------- #
class POIDetector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.pip = pip_size(cfg.symbol_spec)
        self._ids = itertools.count(1)
        self._seen: set = set()      # (direction, origin_time) dedupe

    def prepare(self, m15: pd.DataFrame) -> None:
        """Pre-compute per-bar series.  Every value used at bar j depends only
        on bars <= j (ATR) or is explicitly delayed (swings)."""
        self.m15 = m15
        self.atr = atr(m15, self.cfg.poi.atr_period).to_numpy()
        if self.cfg.poi.require_structure_break:
            self.sh, self.sl = swing_flags(m15, self.cfg.poi.swing_left,
                                           self.cfg.poi.swing_right)
        else:
            self.sh = self.sl = np.zeros(len(m15), dtype=bool)
        self._seen.clear()

    # -- dispatch ---------------------------------------------------------- #
    def detect(self, j: int, m5_index: int) -> List[POI]:
        t = self.cfg.poi.poi_type
        if t is POIType.ORDER_BLOCK:
            found = self._order_blocks(j)
        elif t is POIType.FVG:
            found = self._fvg(j)
        else:
            found = self._swing(j)
        out = []
        for poi in found:
            key = (poi.direction, poi.origin_time)
            if key in self._seen:
                continue
            if not self._width_ok(poi):
                continue
            self._seen.add(key)
            poi.created_m5_index = m5_index
            out.append(poi)
        return out

    def _width_ok(self, poi: POI) -> bool:
        c = self.cfg.poi
        w = poi.height / self.pip
        return c.min_zone_width_pips <= w <= c.max_zone_width_pips

    def _mk(self, direction: Direction, lo: float, hi: float, origin_time,
            created_time, strength: float, ptype: POIType) -> POI:
        pad = self.cfg.poi.zone_padding_pips * self.pip
        return POI(poi_id=f"POI{next(self._ids):05d}", direction=direction,
                   poi_type=ptype, upper=hi + pad, lower=lo - pad,
                   origin_time=origin_time, created_time=created_time,
                   strength=strength)

    # -- order block (PRIMARY) --------------------------------------------- #
    def _order_blocks(self, j: int) -> List[POI]:
        """Origin candle of a displacement leg.

        Demand: bar j closes an unbroken run of up-closing candles that began
        immediately after the last down-closing candle `o`.  If that run is
        long enough, big enough (ATR-normalised) and closed above high[o], the
        candle `o` becomes the demand POI.  Supply is the mirror image.

        Because `o` is by construction the LAST opposite candle before j, the
        leg definition needs no extra 'strong move' hand-waving: it is simply
        'k consecutive same-direction closes covering >= m x ATR'.
        """
        c = self.cfg.poi
        m = self.m15
        if j < max(int(c.atr_period), int(c.max_leg_bars) + 1):
            return []
        o_, h_, l_, cl_ = (m["open"].to_numpy(), m["high"].to_numpy(),
                           m["low"].to_numpy(), m["close"].to_numpy())
        created = m["bar_end"].iloc[j]
        out: List[POI] = []

        for bullish in (True, False):
            if bullish and not cl_[j] > o_[j]:
                continue
            if not bullish and not cl_[j] < o_[j]:
                continue
            # walk back to the last opposite-closing candle
            o = -1
            for k in range(1, int(c.max_leg_bars) + 1):
                idx = j - k
                if idx < 1:
                    break
                if (bullish and cl_[idx] < o_[idx]) or (not bullish and cl_[idx] > o_[idx]):
                    o = idx
                    break
            if o < 0:
                continue
            leg = j - o
            if not (c.min_leg_bars <= leg <= c.max_leg_bars):
                continue
            a = self.atr[o]
            if not np.isfinite(a) or a <= 0:
                continue
            if bullish:
                impulse = cl_[j] - l_[o]
                if cl_[j] <= h_[o]:
                    continue
            else:
                impulse = h_[o] - cl_[j]
                if cl_[j] >= l_[o]:
                    continue
            if impulse < c.displacement_atr_mult * a:
                continue
            if c.require_structure_break and not self._broke_structure(j, o, bullish):
                continue
            if c.use_body_only:
                lo, hi = min(o_[o], cl_[o]), max(o_[o], cl_[o])
            else:
                lo, hi = l_[o], h_[o]
            out.append(self._mk(Direction.LONG if bullish else Direction.SHORT,
                                lo, hi, m["timestamp"].iloc[o], created,
                                float(impulse / a), POIType.ORDER_BLOCK))
        return out

    def _broke_structure(self, j: int, o: int, bullish: bool) -> bool:
        """Leg must close beyond the most recently CONFIRMED swing before it."""
        c = self.cfg.poi
        m = self.m15
        limit = o - c.swing_right          # pivot p is confirmed at p + right
        flags = self.sh if bullish else self.sl
        idxs = np.flatnonzero(flags[:max(limit, 0)])
        if len(idxs) == 0:
            return False
        p = int(idxs[-1])
        if bullish:
            return m["close"].iloc[j] > m["high"].iloc[p]
        return m["close"].iloc[j] < m["low"].iloc[p]

    # -- fair value gap ----------------------------------------------------- #
    def _fvg(self, j: int) -> List[POI]:
        c = self.cfg.poi
        m = self.m15
        if j < 2:
            return []
        h, l = m["high"].to_numpy(), m["low"].to_numpy()
        created = m["bar_end"].iloc[j]
        out: List[POI] = []
        if l[j] - h[j - 2] > c.min_fvg_pips * self.pip:
            out.append(self._mk(Direction.LONG, h[j - 2], l[j],
                                m["timestamp"].iloc[j - 1], created, 1.0, POIType.FVG))
        if l[j - 2] - h[j] > c.min_fvg_pips * self.pip:
            out.append(self._mk(Direction.SHORT, h[j], l[j - 2],
                                m["timestamp"].iloc[j - 1], created, 1.0, POIType.FVG))
        return out

    # -- swing band --------------------------------------------------------- #
    def _swing(self, j: int) -> List[POI]:
        c = self.cfg.poi
        m = self.m15
        p = j - c.swing_right
        if p < c.swing_left:
            return []
        created = m["bar_end"].iloc[j]
        band = c.swing_zone_height_pips * self.pip
        out: List[POI] = []
        if self.sl[p]:
            lo = m["low"].iloc[p]
            out.append(self._mk(Direction.LONG, lo, lo + band,
                                m["timestamp"].iloc[p], created, 1.0, POIType.SWING))
        if self.sh[p]:
            hi = m["high"].iloc[p]
            out.append(self._mk(Direction.SHORT, hi - band, hi,
                                m["timestamp"].iloc[p], created, 1.0, POIType.SWING))
        return out


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class StrategyEngine:
    """Consumes closed M5 bars, emits Signals.  Holds no execution logic."""

    def __init__(self, cfg: Config, events: Optional[EventLog] = None) -> None:
        self.cfg = cfg
        self.pip = pip_size(cfg.symbol_spec)
        self.events = events or EventLog(enabled=False)
        self.detector = POIDetector(cfg)
        self.pois: List[POI] = []
        self._last_m15 = -1

    # -- setup -------------------------------------------------------------- #
    def prepare(self, aligned: AlignedData) -> None:
        self.aligned = aligned
        self.arr = aligned.m5_arr
        self.detector.prepare(aligned.m15)
        self.pois = []
        self._last_m15 = -1
        self._m15_atr = self.detector.atr

    # -- main step ---------------------------------------------------------- #
    def step(self, i: int) -> List[Signal]:
        """Process the close of M5 bar ``i``.  Returns signals confirmed on it."""
        a = self.aligned
        ready = int(a.m15_ready[i])
        for j in range(self._last_m15 + 1, ready + 1):
            for poi in self.detector.detect(j, i):
                self._admit(poi)
        self._last_m15 = max(self._last_m15, ready)

        signals: List[Signal] = []
        if not self.pois:
            return signals
        ts = ts_at(self.arr["end"][i])            # one Timestamp per bar, not per POI
        for poi in self.pois:
            if not poi.is_active:
                continue
            sig = self._update_poi(poi, i, ts)
            if sig is not None:
                signals.append(sig)
        self.pois = [p for p in self.pois if p.is_active]
        return signals

    # -- POI admission / overlap handling ----------------------------------- #
    def _admit(self, poi: POI) -> None:
        c = self.cfg.poi
        same_dir = [p for p in self.pois if p.is_active and p.direction == poi.direction]
        sep = c.overlap_min_separation_pips * self.pip
        for other in same_dir:
            overlap = min(poi.upper, other.upper) - max(poi.lower, other.lower)
            if overlap > -sep:      # touching or overlapping
                if c.prefer_on_overlap == "strongest" and other.strength >= poi.strength:
                    self.events.log(poi.created_time, "reject", reason="overlap_weaker",
                                    poi_id=poi.poi_id)
                    return
                other.state = INVALID
                other.dead_reason = "superseded_by_overlap"
                self.events.log(poi.created_time, "poi_invalid", poi_id=other.poi_id,
                                reason=other.dead_reason)
        if len(same_dir) >= c.max_active_pois:
            oldest = min(same_dir, key=lambda p: p.created_time)
            oldest.state = EXPIRED
            oldest.dead_reason = "max_active_pois"
        self.pois.append(poi)
        self.events.log(poi.created_time, "poi_created", poi_id=poi.poi_id,
                        direction=poi.direction.value, poi_type=poi.poi_type.value,
                        upper=poi.upper, lower=poi.lower,
                        width_pips=round(poi.height / self.pip, 2),
                        strength=round(poi.strength, 2),
                        origin_time=str(poi.origin_time))

    # -- per-POI state machine ---------------------------------------------- #
    def _update_poi(self, poi: POI, i: int, ts: pd.Timestamp) -> Optional[Signal]:
        rc, tc = self.cfg.retest, self.cfg.trade
        a = self.arr
        long = poi.direction is Direction.LONG
        hi, lo, cl = a["high"][i], a["low"][i], a["close"][i]
        tol = rc.touch_tolerance_pips * self.pip
        inval_buf = rc.invalidation_buffer_pips * self.pip

        # 1. hard expiry (age)
        age_bars = i - poi.created_m5_index
        if age_bars > self.cfg.poi.expiry_m15_bars * 3:
            return self._kill(poi, EXPIRED, "poi_age", ts)

        # 2. invalidation: a CLOSE beyond the distal boundary means the zone lost
        if rc.invalidate_on_close_beyond_distal:
            if long and cl < poi.lower - inval_buf:
                return self._kill(poi, INVALID, "close_below_zone", ts)
            if not long and cl > poi.upper + inval_buf:
                return self._kill(poi, INVALID, "close_above_zone", ts)

        # 3. departure
        if poi.state == CREATED:
            outside = (lo > poi.upper) if long else (hi < poi.lower)
            poi.bars_outside = poi.bars_outside + 1 if outside else 0
            dist = (hi - poi.upper) if long else (poi.lower - lo)
            poi.max_departure_pips = max(poi.max_departure_pips, dist / self.pip)
            atr_ok = True
            if rc.min_departure_atr > 0:
                j = int(self.aligned.m15_ready[i])
                a = self._m15_atr[j] if j >= 0 else np.nan
                atr_ok = np.isfinite(a) and dist >= rc.min_departure_atr * a
            if (poi.bars_outside >= rc.min_m5_bars_outside
                    and poi.max_departure_pips >= rc.min_departure_pips and atr_ok):
                poi.state, poi.departed_time, poi.departed_index = DEPARTED, ts, i
                self.events.log(ts, "departure", poi_id=poi.poi_id,
                                departure_pips=round(poi.max_departure_pips, 1))
            return None

        # 4. waiting for the retest
        if poi.state == DEPARTED:
            dist = (hi - poi.upper) if long else (poi.lower - lo)
            poi.max_departure_pips = max(poi.max_departure_pips, dist / self.pip)
            if poi.max_departure_pips > rc.max_departure_pips:
                return self._kill(poi, EXPIRED, "ran_too_far", ts)
            if i - poi.departed_index > rc.max_m5_bars_to_retest:
                return self._kill(poi, EXPIRED, "retest_window", ts)
            if i - poi.departed_index < rc.min_m5_bars_before_retest:
                return None

            touched = (lo <= poi.upper + tol) if long else (hi >= poi.lower - tol)
            if not touched:
                return None
            if not rc.allow_wick_only_touch:
                closed_in = (cl <= poi.upper + tol) if long else (cl >= poi.lower - tol)
                if not closed_in:
                    return None
            pen = ((poi.upper - lo) if long else (hi - poi.lower)) / max(poi.height, 1e-12)
            if pen > rc.max_penetration_pct:
                return self._kill(poi, INVALID, "penetration_too_deep", ts)

            poi.state, poi.retest_time, poi.retest_index = RETESTED, ts, i
            poi.retest_count += 1
            self.events.log(ts, "retest", poi_id=poi.poi_id, retest_no=poi.retest_count,
                            penetration_pct=round(float(pen), 3))
            # the retest bar itself may already be the engulfing bar
            return self._check_engulf(poi, i, ts)

        # 5. waiting for the engulfing confirmation
        if poi.state == RETESTED:
            if i - poi.retest_index > self.cfg.engulf.max_m5_bars_after_retest:
                if poi.retest_count < rc.max_retests:
                    poi.state = DEPARTED       # allow a further retest
                    self.events.log(ts, "rearm", poi_id=poi.poi_id)
                    return None
                return self._kill(poi, EXPIRED, "no_engulf_in_window", ts)
            return self._check_engulf(poi, i, ts)
        return None

    def _check_engulf(self, poi: POI, i: int, ts: pd.Timestamp) -> Optional[Signal]:
        ec = self.cfg.engulf
        if i < 1:
            return None
        a = self.arr
        prev = Candle(a["open"][i - 1], a["high"][i - 1],
                      a["low"][i - 1], a["close"][i - 1])
        cur = Candle(a["open"][i], a["high"][i], a["low"][i], a["close"][i])
        long = poi.direction is Direction.LONG
        res = is_engulfing(prev, cur, long, ec, self.pip)
        if not res.ok:
            return None
        if ec.require_touch_zone:
            prox = ec.zone_proximity_pips * self.pip
            touch = (cur.low <= poi.upper + prox) if long else (cur.high >= poi.lower - prox)
            if not touch:
                self.events.log(ts, "reject", reason="engulf_not_at_zone",
                                poi_id=poi.poi_id)
                return None
        poi.state = TRIGGERED
        self.events.log(ts, "engulf", poi_id=poi.poi_id,
                        direction=poi.direction.value,
                        body_pips=round(cur.body / self.pip, 2))
        return Signal(signal_time=ts, signal_index=i, direction=poi.direction,
                      poi=replace(poi), engulf_open=cur.open, engulf_high=cur.high,
                      engulf_low=cur.low, engulf_close=cur.close,
                      engulf_time=ts_at(a["ts"][i]))

    def _kill(self, poi: POI, state: str, reason: str, ts) -> None:
        poi.state, poi.dead_reason = state, reason
        self.events.log(ts, "poi_dead", poi_id=poi.poi_id, state=state, reason=reason)
        return None
