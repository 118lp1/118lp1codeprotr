"""Configuration objects for the M15-POI / M5-engulfing retest strategy.

Every threshold that could otherwise be a discretionary judgement call lives here
as a named, typed, serialisable parameter.  Nothing in the strategy code is
allowed to contain a magic number.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional

try:  # PyYAML is optional; JSON configs work without it
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

import json


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Mode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class POIType(str, Enum):
    ORDER_BLOCK = "order_block"       # primary definition (see SPECIFICATION.md)
    FVG = "fvg"                       # fair value gap
    SWING = "swing"                   # confirmed swing high/low band


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class EntryMode(str, Enum):
    NEXT_OPEN = "next_open"           # PRIMARY / production
    ENGULF_CLOSE = "engulf_close"
    LIMIT_50 = "limit_50"
    BREAKOUT = "breakout"


class SameBarPolicy(str, Enum):
    """How an M5-only backtest resolves a bar that contains both SL and TP."""
    SL_FIRST = "sl_first"             # pessimistic
    TP_FIRST = "tp_first"             # optimistic (never use for decisions)
    PROPORTIONAL = "proportional"     # 50/50 expectation


# --------------------------------------------------------------------------- #
# Symbol specification
# --------------------------------------------------------------------------- #
@dataclass
class SymbolSpec:
    """Instrument metadata.

    In LIVE/PAPER mode this is populated from ``mt5.symbol_info``.  For
    backtests it must be supplied explicitly, because getting ``tick_value``
    wrong silently corrupts every position size in the report.
    """
    name: str = "EURUSD"
    digits: int = 5
    point: float = 0.00001
    tick_size: float = 0.00001
    tick_value: float = 1.0           # account currency P/L per tick per 1.0 lot
    contract_size: float = 100_000.0
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    pip_size: Optional[float] = None  # None -> derived from digits (see pip_size_of)
    stops_level_points: int = 0       # broker minimum SL/TP distance, in points

    def resolved_pip_size(self) -> float:
        if self.pip_size is not None:
            return self.pip_size
        # FX convention: a 3/5-digit quote is a fractional-pip quote.
        if self.digits in (3, 5):
            return self.point * 10.0
        return self.point


# --------------------------------------------------------------------------- #
# Strategy sub-configs
# --------------------------------------------------------------------------- #
@dataclass
class POIConfig:
    poi_type: POIType = POIType.ORDER_BLOCK

    # --- shared -----------------------------------------------------------
    atr_period: int = 14              # ATR on M15, used to normalise displacement
    use_body_only: bool = False       # zone = candle body instead of full range
    zone_padding_pips: float = 0.0    # symmetric padding added to both boundaries
    max_zone_width_pips: float = 18.0 # reject zones that cannot fit a 20-pip stop
    min_zone_width_pips: float = 0.5  # reject degenerate/dot zones
    expiry_m15_bars: int = 96         # POI dies 24h (96 M15 bars) after creation
    max_active_pois: int = 6          # per direction
    overlap_min_separation_pips: float = 0.0  # >0 also drops merely *adjacent* POIs
    prefer_on_overlap: str = "newest"  # newest | strongest

    # --- order-block specific --------------------------------------------
    displacement_atr_mult: float = 1.0   # impulse leg size, in ATR units
    max_leg_bars: int = 5                # consecutive same-direction M15 candles
    min_leg_bars: int = 1
    require_structure_break: bool = False  # leg must take out last confirmed swing
    swing_left: int = 2                    # pivot definition, used if the above is on
    swing_right: int = 2

    # --- fvg specific -----------------------------------------------------
    min_fvg_pips: float = 1.0

    # --- swing specific ---------------------------------------------------
    swing_zone_height_pips: float = 5.0


@dataclass
class RetestConfig:
    # departure: price must genuinely leave the zone before a retest counts
    min_departure_pips: float = 8.0
    min_departure_atr: float = 0.0        # additional ATR-based requirement (0 = off)
    min_m5_bars_outside: int = 3          # consecutive M5 bars fully outside the zone
    max_departure_pips: float = 250.0     # if price runs further, the POI goes stale

    # retest window
    min_m5_bars_before_retest: int = 0
    max_m5_bars_to_retest: int = 288      # 24h of M5 bars

    # what counts as "touching" the zone
    touch_tolerance_pips: float = 0.5     # tolerance around the proximal boundary
    max_penetration_pct: float = 1.0      # 1.0 = may trade down to the distal edge
    invalidate_on_close_beyond_distal: bool = True
    invalidation_buffer_pips: float = 1.0
    allow_wick_only_touch: bool = True    # False -> require an M5 close inside zone

    max_retests: int = 1                  # only the FIRST retest is tradable


@dataclass
class EngulfConfig:
    max_m5_bars_after_retest: int = 6     # confirmation window after the first touch
    require_body_engulf: bool = True      # body-vs-body (see spec for range variant)
    engulf_use_range: bool = False        # True -> current range must engulf prior range
    min_body_ratio: float = 1.0           # cur_body >= ratio * prev_body
    min_range_ratio: float = 0.0          # cur_range >= ratio * prev_range (0 = off)
    close_position_pct: float = 0.0       # close in top/bottom X% of its own range
    min_body_pips: float = 0.5            # kills doji-engulfs-doji noise
    max_body_pips: float = 40.0           # a huge bar means the entry is already late
    require_prev_opposite: bool = True
    require_touch_zone: bool = True       # engulfing bar must itself touch the zone
    zone_proximity_pips: float = 2.0      # tolerance for the above


@dataclass
class TradeConfig:
    entry_mode: EntryMode = EntryMode.NEXT_OPEN
    limit_valid_m5_bars: int = 3          # for LIMIT_50 / BREAKOUT modes
    rr: float = 2.0                       # take-profit multiple of risk
    sl_buffer_pips: float = 1.0           # beyond the POI distal boundary
    max_sl_pips: float = 20.0             # HARD constraint: reject, never compress
    min_sl_pips: float = 3.0              # below this the stop is inside the noise
    # Cost drag is (spread + entry slippage) / stop.  An absolute pip floor does
    # not express that: 3 pips is survivable at a 0.2-pip spread and hopeless at
    # 1.5.  This floor is relative, so it tightens exactly when costs do.
    min_stop_spread_mult: float = 8.0     # reject stop < mult x spread; 0 disables
    max_holding_m5_bars: int = 288        # time stop (24h); 0 disables
    close_at_session_end: bool = False


@dataclass
class SessionConfig:
    """Sessions are expressed in the *data* timezone (UTC by default)."""
    enabled: bool = True
    windows: List[List[str]] = field(
        default_factory=lambda: [["07:00", "16:00"]]  # London + London/NY overlap
    )
    weekdays: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    block_minutes_after_open: int = 0
    block_minutes_before_close: int = 0


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.005         # 0.5 % of equity
    max_open_positions: int = 1
    max_trades_per_day: int = 3
    max_daily_loss_pct: float = 0.02      # stop trading for the day at -2 %
    max_consecutive_losses: int = 4       # 0 disables
    cooldown_m5_bars_after_loss: int = 3
    cooldown_m5_bars_after_any: int = 0
    max_spread_pips: float = 2.0
    max_slippage_pips: float = 1.0
    allow_fractional_lots: bool = True
    reject_if_below_min_lot: bool = True   # honest: skip rather than over-risk


@dataclass
class CostConfig:
    spread_pips: float = 0.8              # constant spread model (baseline)
    spread_multiplier: float = 1.0        # stress-test knob
    use_spread_column: bool = False       # use per-bar spread from the data if present
    slippage_entry_pips: float = 0.2
    slippage_stop_pips: float = 0.3       # stops slip, limits generally do not
    slippage_multiplier: float = 1.0
    commission_per_lot_round_turn: float = 7.0   # account currency
    swap_long_pips_per_day: float = 0.0
    swap_short_pips_per_day: float = 0.0
    apply_swap: bool = False

    def spread(self) -> float:
        return self.spread_pips * self.spread_multiplier

    def slip_entry(self) -> float:
        return self.slippage_entry_pips * self.slippage_multiplier

    def slip_stop(self) -> float:
        return self.slippage_stop_pips * self.slippage_multiplier


@dataclass
class BacktestConfig:
    initial_equity: float = 10_000.0
    execution_model: str = "m1"           # "m1" | "m5"
    same_bar_policy: SameBarPolicy = SameBarPolicy.SL_FIRST
    m1_same_bar_policy: SameBarPolicy = SameBarPolicy.SL_FIRST
    compound: bool = True                 # size off current equity vs initial
    seed: int = 7


@dataclass
class DataConfig:
    symbol: str = "EURUSD"
    m5_path: str = "data/EURUSD_M5.csv"
    m1_path: Optional[str] = "data/EURUSD_M1.csv"
    data_tz: str = "UTC"                  # timezone of the raw timestamps
    broker_utc_offset_hours: float = 0.0  # applied when reading raw MT5 exports
    start: Optional[str] = None
    end: Optional[str] = None
    max_gap_minutes: int = 90             # larger gaps are treated as market closure


@dataclass
class Config:
    mode: Mode = Mode.BACKTEST            # NEVER default to LIVE
    strategy_version: str = "poi_retest_engulf_v1.0.0"
    symbol_spec: SymbolSpec = field(default_factory=SymbolSpec)
    data: DataConfig = field(default_factory=DataConfig)
    poi: POIConfig = field(default_factory=POIConfig)
    retest: RetestConfig = field(default_factory=RetestConfig)
    engulf: EngulfConfig = field(default_factory=EngulfConfig)
    trade: TradeConfig = field(default_factory=TradeConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    log_level: str = "INFO"
    output_dir: str = "results"

    # -- (de)serialisation ------------------------------------------------- #
    def to_dict(self) -> Dict[str, Any]:
        def _enc(o: Any) -> Any:
            if isinstance(o, Enum):
                return o.value
            return o
        return json.loads(json.dumps(asdict(self), default=_enc))

    def save(self, path: str) -> None:
        payload = self.to_dict()
        if path.endswith((".yaml", ".yml")) and yaml is not None:
            with open(path, "w") as fh:
                yaml.safe_dump(payload, fh, sort_keys=False)
        else:
            with open(path, "w") as fh:
                json.dump(payload, fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path) as fh:
            if path.endswith((".yaml", ".yml")):
                if yaml is None:
                    raise RuntimeError("PyYAML is required to read YAML configs")
                raw = yaml.safe_load(fh)
            else:
                raw = json.load(fh)
        return cls.from_dict(raw or {})

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Config":
        return _build(cls, raw)

    def with_overrides(self, overrides: Dict[str, Any]) -> "Config":
        """Return a copy with dotted-path overrides, e.g. {'poi.atr_period': 20}.

        Used by the optimiser so that a trial never mutates shared state.
        """
        raw = self.to_dict()
        for dotted, value in overrides.items():
            node = raw
            parts = dotted.split(".")
            for p in parts[:-1]:
                if p not in node:
                    raise KeyError(f"unknown config section: {p} (in {dotted})")
                node = node[p]
            if parts[-1] not in node:
                raise KeyError(f"unknown config key: {dotted}")
            node[parts[-1]] = value
        return Config.from_dict(raw)


def _build(cls: Any, raw: Any) -> Any:
    """Recursively rebuild nested dataclasses from plain dicts."""
    if not dataclasses.is_dataclass(cls):
        return raw
    kwargs: Dict[str, Any] = {}
    fields = {f.name: f for f in dataclasses.fields(cls)}
    for key, value in (raw or {}).items():
        if key not in fields:
            raise KeyError(f"unknown configuration key '{key}' for {cls.__name__}")
        ftype = fields[key].type
        if isinstance(ftype, str):  # postponed annotations
            ftype = _resolve(ftype)
        if dataclasses.is_dataclass(ftype) and isinstance(value, dict):
            kwargs[key] = _build(ftype, value)
        elif isinstance(ftype, type) and issubclass(ftype, Enum) and value is not None:
            kwargs[key] = ftype(value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


_TYPES = {
    "SymbolSpec": SymbolSpec, "DataConfig": DataConfig, "POIConfig": POIConfig,
    "RetestConfig": RetestConfig, "EngulfConfig": EngulfConfig,
    "TradeConfig": TradeConfig, "SessionConfig": SessionConfig,
    "RiskConfig": RiskConfig, "CostConfig": CostConfig,
    "BacktestConfig": BacktestConfig, "Mode": Mode, "POIType": POIType,
    "EntryMode": EntryMode, "SameBarPolicy": SameBarPolicy,
}


def _resolve(name: str) -> Any:
    name = name.replace("Optional[", "").replace("]", "").strip()
    return _TYPES.get(name, name)
