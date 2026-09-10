"""Event-driven backtester.

Loop invariant, per M5 bar ``i``:

    1. fill any order queued at the previous bar's close (at THIS bar's open)
    2. progress every open position through bar ``i`` (M1 or M5 resolution)
    3. mark equity
    4. feed bar ``i`` to the strategy engine  -> signals (known only at its close)
    5. size/validate the signal and queue it for bar ``i+1``'s open

Nothing in step 4/5 may read bar ``i+1``.  Nothing in step 2 may read a bar
later than ``i``.  That is the entire look-ahead defence, and it is the reason
the loop is written as an explicit state machine instead of vectorised pandas.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .config import Config, Direction, EntryMode, SameBarPolicy
from .data import AlignedData, ts_at
from .logger import EventLog, get_logger
from .risk import OrderPlan, RiskManager, build_order
from .strategy import Signal, StrategyEngine
from .utils import (in_session, money_per_price_unit, pip_size, pips_to_price,
                    price_to_pips, round_to_tick, trading_day)


# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    trade_id: int
    symbol: str
    direction: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry: float
    sl: float
    tp: float
    sl_distance: float
    sl_pips: float
    risk_amount: float
    volume: float
    poi_id: str = ""
    poi_type: str = ""
    poi_time: Optional[pd.Timestamp] = None
    poi_high: float = 0.0
    poi_low: float = 0.0
    poi_strength: float = 0.0
    retest_time: Optional[pd.Timestamp] = None
    engulf_time: Optional[pd.Timestamp] = None
    exit_time: Optional[pd.Timestamp] = None
    exit_price: float = 0.0
    exit_reason: str = ""
    gross_pnl: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    pnl: float = 0.0
    r_multiple: float = 0.0
    holding_minutes: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    equity_after: float = 0.0
    session_hour: int = 0
    weekday: int = 0
    execution_model: str = "m1"
    strategy_version: str = ""


@dataclass
class _OpenPosition:
    trade: Trade
    plan: OrderPlan
    entry_index: int
    m1_cursor: int = -1
    mfe_price: float = 0.0
    mae_price: float = 0.0


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.DataFrame
    events: EventLog
    config: Config
    rejections: Dict[str, int] = field(default_factory=dict)
    diagnostics: Dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Execution models
# --------------------------------------------------------------------------- #
class ExecutionModel:
    """Resolves SL/TP inside a bar.  Sub-classes differ only in resolution."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.spec = cfg.symbol_spec
        self.pip = pip_size(cfg.symbol_spec)
        self.spread_price = pips_to_price(cfg.costs.spread(), self.spec)
        self.slip_stop = pips_to_price(cfg.costs.slip_stop(), self.spec)
        # how often the model was asked to resolve a bar containing BOTH levels
        self.ambiguous_bars = 0

    # -- level logic shared by both models ---------------------------------- #
    def _check(self, pos: _OpenPosition, o: float, h: float, l: float,
               policy: SameBarPolicy) -> Optional[Tuple[float, str]]:
        t = pos.trade
        long = t.direction == Direction.LONG.value
        if long:
            hit_sl = l <= t.sl
            hit_tp = h >= t.tp
            fill_sl = min(t.sl, o) - self.slip_stop
            fill_tp = max(t.tp, o)
        else:
            # short exits are buy-backs at the ask = bid + spread
            hit_sl = (h + self.spread_price) >= t.sl
            hit_tp = (l + self.spread_price) <= t.tp
            fill_sl = max(t.sl, o + self.spread_price) + self.slip_stop
            fill_tp = min(t.tp, o + self.spread_price)
        if hit_sl and hit_tp:
            self.ambiguous_bars += 1
            if policy is SameBarPolicy.TP_FIRST:
                return fill_tp, "tp_optimistic"
            if policy is SameBarPolicy.PROPORTIONAL:
                mid = 0.5 * (fill_tp + fill_sl)
                return mid, "ambiguous_bar"
            return fill_sl, "sl_pessimistic"
        if hit_sl:
            return fill_sl, "sl"
        if hit_tp:
            return fill_tp, "tp"
        return None

    def _excursion(self, pos: _OpenPosition, h: float, l: float) -> None:
        long = pos.trade.direction == Direction.LONG.value
        if long:
            pos.mfe_price = max(pos.mfe_price, h)
            pos.mae_price = min(pos.mae_price, l)
        else:
            pos.mfe_price = min(pos.mfe_price, l)
            pos.mae_price = max(pos.mae_price, h)


class M5Execution(ExecutionModel):
    name = "m5"

    def progress(self, pos: _OpenPosition, i: int, aligned: AlignedData
                 ) -> Optional[Tuple[pd.Timestamp, float, str]]:
        a = aligned.m5_arr
        o, h, l = a["open"][i], a["high"][i], a["low"][i]
        self._excursion(pos, h, l)
        res = self._check(pos, o, h, l, self.cfg.backtest.same_bar_policy)
        if res is None:
            return None
        price, reason = res
        return ts_at(a["end"][i]), price, reason


class M1Execution(ExecutionModel):
    name = "m1"

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        self._fallback = M5Execution(cfg)
        self.m5_fallbacks = 0

    def progress(self, pos: _OpenPosition, i: int, aligned: AlignedData
                 ) -> Optional[Tuple[pd.Timestamp, float, str]]:
        m1 = aligned.m1
        if m1 is None or aligned.m1_start is None:
            self.m5_fallbacks += 1
            return self._fallback.progress(pos, i, aligned)
        start = int(aligned.m1_start[i])
        end = int(aligned.m1_start[i + 1]) if i + 1 < len(aligned.m1_start) else len(m1)
        if start >= end:  # no M1 coverage for this M5 bar -> degrade gracefully
            self.m5_fallbacks += 1
            return self._fallback.progress(pos, i, aligned)
        a = aligned.m1_arr
        o_, h_, l_, ts_ = a["open"], a["high"], a["low"], a["ts"]
        policy = self.cfg.backtest.m1_same_bar_policy
        for k in range(max(start, pos.m1_cursor), end):
            self._excursion(pos, h_[k], l_[k])
            res = self._check(pos, o_[k], h_[k], l_[k], policy)
            pos.m1_cursor = k + 1
            if res is not None:
                price, reason = res
                return ts_at(ts_[k]) + pd.Timedelta(minutes=1), price, reason
        return None


def make_execution(cfg: Config) -> ExecutionModel:
    return M1Execution(cfg) if cfg.backtest.execution_model == "m1" else M5Execution(cfg)


# --------------------------------------------------------------------------- #
# Backtester
# --------------------------------------------------------------------------- #
class Backtester:
    def __init__(self, cfg: Config, log_events: bool = True) -> None:
        self.cfg = cfg
        self.log = get_logger("backtest", cfg.log_level)
        self.events = EventLog(enabled=log_events)

    def run(self, aligned: AlignedData) -> BacktestResult:
        cfg = self.cfg
        spec = cfg.symbol_spec
        pip = pip_size(spec)
        engine = StrategyEngine(cfg, self.events)
        engine.prepare(aligned)
        risk = RiskManager(cfg)
        risk.reset(cfg.backtest.initial_equity)
        execu = make_execution(cfg)
        mpu = money_per_price_unit(spec)

        m5 = aligned.m5
        arr = aligned.m5_arr
        n = len(m5)
        opens = arr["open"]
        ts_open = arr["ts"]
        ts_end = arr["end"]
        has_spread_col = cfg.costs.use_spread_column and "spread" in m5.columns
        spread_col = (m5["spread"].to_numpy() * spec.point / pip) if has_spread_col else None

        trades: List[Trade] = []
        open_positions: List[_OpenPosition] = []
        pending: List[Tuple[Signal, OrderPlan]] = []
        equity_curve = np.empty(n)
        trade_id = 0
        closed_equity = cfg.backtest.initial_equity

        for i in range(n):
            bar_ts_open = ts_at(ts_open[i])

            # ---- 1. fill orders queued at the previous close ---------------- #
            for sig, plan in pending:
                trade_id += 1
                t = Trade(
                    trade_id=trade_id, symbol=spec.name, direction=plan.direction.value,
                    signal_time=sig.signal_time, entry_time=bar_ts_open,
                    entry=plan.entry, sl=plan.sl, tp=plan.tp,
                    sl_distance=plan.sl_distance, sl_pips=plan.sl_pips,
                    risk_amount=plan.risk_amount, volume=plan.volume,
                    poi_id=sig.poi.poi_id, poi_type=sig.poi.poi_type.value,
                    poi_time=sig.poi.origin_time, poi_high=sig.poi.upper,
                    poi_low=sig.poi.lower, poi_strength=sig.poi.strength,
                    retest_time=sig.poi.retest_time, engulf_time=sig.engulf_time,
                    commission=plan.commission, session_hour=bar_ts_open.hour,
                    weekday=int(bar_ts_open.weekday()), execution_model=execu.name,
                    strategy_version=cfg.strategy_version)
                pos = _OpenPosition(trade=t, plan=plan, entry_index=i,
                                    m1_cursor=int(aligned.m1_start[i]) if aligned.m1_start is not None else -1,
                                    mfe_price=plan.entry, mae_price=plan.entry)
                open_positions.append(pos)
                risk.on_open()
                self.events.log(bar_ts_open, "entry", trade_id=trade_id,
                                direction=t.direction, entry=t.entry, sl=t.sl,
                                tp=t.tp, volume=t.volume, sl_pips=round(t.sl_pips, 2),
                                poi_id=t.poi_id)
            pending = []

            # ---- 2. progress open positions through this bar ---------------- #
            for pos in list(open_positions):
                res = execu.progress(pos, i, aligned)
                if res is None:
                    res = self._time_or_session_exit(pos, i, aligned)
                if res is None and i == n - 1:
                    res = (ts_at(ts_end[i]), float(arr["close"][i]), "end_of_data")
                if res is None:
                    continue
                exit_ts, exit_price, reason = res
                closed_equity = self._close(pos, exit_ts, exit_price, reason, mpu,
                                            closed_equity, risk, i, trades)
                open_positions.remove(pos)

            equity_curve[i] = closed_equity + self._floating(open_positions, arr, i, mpu)

            # ---- 3. strategy step (uses only closed data) ------------------- #
            signals = engine.step(i)
            if not signals or i + 1 >= n:
                continue

            # ---- 4. size & validate for the NEXT bar's open ----------------- #
            next_open = float(opens[i + 1])
            next_ts = ts_at(ts_open[i + 1])
            spread_pips = float(spread_col[i]) if spread_col is not None else cfg.costs.spread()
            risk.equity = closed_equity
            for sig in signals:
                equity_for_size = (closed_equity if cfg.backtest.compound
                                   else cfg.backtest.initial_equity)
                ok, reason = risk.can_open(next_ts, i, spread_pips)
                if not ok:
                    self.events.log(next_ts, "reject", reason=reason, poi_id=sig.poi.poi_id)
                    continue
                plan = build_order(sig, next_open, equity_for_size, cfg, spread_pips)
                if not plan.ok:
                    self.events.log(next_ts, "reject", reason=plan.reason,
                                    poi_id=sig.poi.poi_id,
                                    sl_pips=round(plan.sl_pips, 2))
                    continue
                if plan.pending:
                    self.events.log(next_ts, "reject", reason="pending_mode_unsupported",
                                    poi_id=sig.poi.poi_id)
                    continue
                pending.append((sig, plan))

        df = pd.DataFrame([asdict(t) for t in trades])
        eq = pd.DataFrame({"timestamp": m5["bar_end"], "equity": equity_curve})
        counts = self.events.counts()
        diagnostics = {
            "m5_bars": float(n),
            "m15_bars": float(len(aligned.m15)),
            "pois_created": float(counts.get("poi_created", 0)),
            "pois_superseded": float(counts.get("poi_invalid", 0)),
            "departures": float(counts.get("departure", 0)),
            "retests": float(counts.get("retest", 0)),
            "engulf_signals": float(counts.get("engulf", 0)),
            "orders_rejected": float(counts.get("reject", 0)),
            "trades": float(len(trades)),
            # bars where SL and TP were both inside the same candle: the share
            # of trades whose outcome is an ASSUMPTION rather than an observation
            "ambiguous_bars": float(execu.ambiguous_bars +
                                    getattr(getattr(execu, "_fallback", None), "ambiguous_bars", 0)),
            "m1_missing_fallbacks": float(getattr(execu, "m5_fallbacks", 0)),
        }
        return BacktestResult(trades=df, equity=eq, events=self.events, config=cfg,
                              rejections=self.events.rejection_counts(),
                              diagnostics=diagnostics)

    # -- helpers ------------------------------------------------------------ #
    def _time_or_session_exit(self, pos: _OpenPosition, i: int, aligned: AlignedData
                              ) -> Optional[Tuple[pd.Timestamp, float, str]]:
        cfg = self.cfg
        a = aligned.m5_arr
        close = float(a["close"][i])
        spread_price = pips_to_price(cfg.costs.spread(), cfg.symbol_spec)
        fill = close if pos.trade.direction == Direction.LONG.value else close + spread_price
        if cfg.trade.max_holding_m5_bars and (i - pos.entry_index) >= cfg.trade.max_holding_m5_bars:
            return ts_at(a["end"][i]), fill, "time_stop"
        if cfg.trade.close_at_session_end and cfg.session.enabled:
            nxt = i + 1
            if nxt < len(a["ts"]):
                if not in_session(ts_at(a["ts"][nxt]), cfg.session):
                    return ts_at(a["end"][i]), fill, "session_end"
        return None

    def _close(self, pos: _OpenPosition, exit_ts, exit_price: float, reason: str,
               mpu: float, equity: float, risk: RiskManager, i: int,
               trades: List[Trade]) -> float:
        t = pos.trade
        long = t.direction == Direction.LONG.value
        delta = (exit_price - t.entry) if long else (t.entry - exit_price)
        t.exit_time, t.exit_price, t.exit_reason = exit_ts, exit_price, reason
        t.gross_pnl = delta * mpu * t.volume
        t.swap = self._swap(t, exit_ts)
        t.pnl = t.gross_pnl - t.commission + t.swap
        risk_money = max(t.risk_amount, 1e-9)
        t.r_multiple = t.pnl / risk_money
        t.holding_minutes = (pd.Timestamp(exit_ts) - pd.Timestamp(t.entry_time)).total_seconds() / 60.0
        span = max(t.sl_distance, 1e-12)
        t.mfe_r = (abs(pos.mfe_price - t.entry) / span) * (1 if _favourable(pos) else 0)
        t.mae_r = abs(pos.mae_price - t.entry) / span
        equity += t.pnl
        t.equity_after = equity
        risk.on_close(t.pnl, i)
        risk.equity = equity
        trades.append(t)
        self.events.log(exit_ts, "exit", trade_id=t.trade_id, price=exit_price,
                        reason=reason, pnl=round(t.pnl, 2), r=round(t.r_multiple, 3))
        return equity

    def _swap(self, t: Trade, exit_ts) -> float:
        c = self.cfg.costs
        if not c.apply_swap:
            return 0.0
        days = max((pd.Timestamp(exit_ts) - pd.Timestamp(t.entry_time)).days, 0)
        pips = (c.swap_long_pips_per_day if t.direction == Direction.LONG.value
                else c.swap_short_pips_per_day)
        return pips * pip_size(self.cfg.symbol_spec) * money_per_price_unit(
            self.cfg.symbol_spec) * t.volume * days

    def _floating(self, positions: List[_OpenPosition], arr: dict,
                  i: int, mpu: float) -> float:
        if not positions:
            return 0.0
        close = float(arr["close"][i])
        tot = 0.0
        for p in positions:
            long = p.trade.direction == Direction.LONG.value
            delta = (close - p.trade.entry) if long else (p.trade.entry - close)
            tot += delta * mpu * p.trade.volume - p.trade.commission
        return tot


def _favourable(pos: _OpenPosition) -> bool:
    long = pos.trade.direction == Direction.LONG.value
    return (pos.mfe_price > pos.trade.entry) if long else (pos.mfe_price < pos.trade.entry)


def compare_execution_models(cfg: Config, aligned: AlignedData) -> pd.DataFrame:
    """Section 13: quantify how much the M5-only assumption flatters results."""
    from .metrics import summarise
    rows = []
    variants = [("M1 execution", "m1", SameBarPolicy.SL_FIRST),
                ("M5 SL-first (pessimistic)", "m5", SameBarPolicy.SL_FIRST),
                ("M5 TP-first (optimistic)", "m5", SameBarPolicy.TP_FIRST),
                ("M5 50/50", "m5", SameBarPolicy.PROPORTIONAL)]
    for label, model, policy in variants:
        c = cfg.with_overrides({"backtest.execution_model": model,
                                "backtest.same_bar_policy": policy.value})
        res = Backtester(c, log_events=False).run(aligned)
        s = summarise(res.trades, c)
        s["model"] = label
        rows.append(s)
    df = pd.DataFrame(rows)
    cols = ["model", "trades", "win_rate", "expectancy_r", "profit_factor",
            "net_profit", "max_drawdown_pct"]
    return df[[c for c in cols if c in df.columns]]
