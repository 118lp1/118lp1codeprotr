"""Risk: stop placement, the hard 20-pip rule, position sizing, and the gates
that can veto an otherwise valid signal.

Quoting convention used everywhere in this project
--------------------------------------------------
OHLC data is treated as the **bid** series (this is what MT5 exports).
``ask = bid + spread``.

  LONG : filled at ask, exited at bid  -> the entry price already contains the
         full spread; SL/TP compare directly against bid high/low.
  SHORT: filled at bid, exited at ask  -> the exit trigger levels are compared
         against ``bid + spread``, which is done by shifting the levels down by
         one spread inside the execution model.

Consequence worth stating explicitly: a trade pays exactly one spread, and the
cost measured **in R** is ``(spread + slippage) / stop_distance``.  With a
0.8-pip spread and a 6-pip stop that is 13 % of risk per trade.  The 20-pip
stop cap therefore does not only filter setups, it also concentrates the book
in the trades where costs bite hardest.  This is analysed, not hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import pandas as pd

from .config import Config, Direction, EntryMode
from .strategy import Signal
from .utils import (money_per_price_unit, pip_size, pips_to_price, price_to_pips,
                    round_to_tick, round_volume, in_session, trading_day)


@dataclass
class OrderPlan:
    ok: bool
    reason: str = ""
    direction: Optional[Direction] = None
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    sl_distance: float = 0.0
    sl_pips: float = 0.0
    volume: float = 0.0
    risk_amount: float = 0.0
    commission: float = 0.0
    pending: bool = False              # limit/stop order rather than market
    pending_price: float = 0.0


# --------------------------------------------------------------------------- #
# Price construction
# --------------------------------------------------------------------------- #
def raw_entry_price(sig: Signal, next_open: float, cfg: Config) -> Tuple[float, bool]:
    """Return (reference price, is_pending) for the configured entry mode."""
    mode = cfg.trade.entry_mode
    if mode is EntryMode.NEXT_OPEN:
        return next_open, False
    if mode is EntryMode.ENGULF_CLOSE:
        # Modelled as a market order at the close print: optimistic, kept for
        # research comparison only.
        return sig.engulf_close, False
    if mode is EntryMode.LIMIT_50:
        return (sig.engulf_high + sig.engulf_low) / 2.0, True
    if mode is EntryMode.BREAKOUT:
        return (sig.engulf_high if sig.direction is Direction.LONG else sig.engulf_low), True
    raise ValueError(f"unknown entry mode {mode}")


def apply_costs_to_entry(price: float, direction: Direction, cfg: Config,
                         spread_pips: Optional[float] = None) -> float:
    spec = cfg.symbol_spec
    sp = cfg.costs.spread() if spread_pips is None else spread_pips
    slip = cfg.costs.slip_entry()
    if direction is Direction.LONG:
        return round_to_tick(price + pips_to_price(sp + slip, spec), spec)
    return round_to_tick(price - pips_to_price(slip, spec), spec)


def stop_loss_price(poi_upper: float, poi_lower: float, direction: Direction,
                    cfg: Config) -> float:
    """Beyond the POI's distal boundary plus a configurable buffer."""
    spec = cfg.symbol_spec
    buf = pips_to_price(cfg.trade.sl_buffer_pips, spec)
    if direction is Direction.LONG:
        return round_to_tick(poi_lower - buf, spec)
    return round_to_tick(poi_upper + buf, spec)


def take_profit_price(entry: float, sl: float, direction: Direction,
                      cfg: Config) -> float:
    spec = cfg.symbol_spec
    risk = abs(entry - sl)
    if direction is Direction.LONG:
        return round_to_tick(entry + cfg.trade.rr * risk, spec)
    return round_to_tick(entry - cfg.trade.rr * risk, spec)


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #
def position_size(equity: float, sl_distance: float, cfg: Config) -> Tuple[float, float]:
    """Lots and the money actually risked.

    ``loss_per_lot = sl_distance / tick_size * tick_value`` works for any MT5
    instrument (FX, metals, indices, crypto) because it is expressed purely in
    the broker's own tick units.  Commission is folded into the denominator so
    that total loss-if-stopped stays within the risk budget.
    """
    spec = cfg.symbol_spec
    if sl_distance <= 0:
        return 0.0, 0.0
    risk_budget = equity * cfg.risk.risk_per_trade
    loss_per_lot = sl_distance * money_per_price_unit(spec)
    denom = loss_per_lot + max(cfg.costs.commission_per_lot_round_turn, 0.0)
    if denom <= 0:
        return 0.0, 0.0
    lots = round_volume(risk_budget / denom, spec)
    lots = min(lots, spec.volume_max)
    return lots, lots * loss_per_lot


def build_order(sig: Signal, next_open: float, equity: float, cfg: Config,
                spread_pips: Optional[float] = None) -> OrderPlan:
    """Turn a signal into a fully specified order, or an explicit rejection."""
    spec = cfg.symbol_spec
    pip = pip_size(spec)
    ref, pending = raw_entry_price(sig, next_open, cfg)
    eff_spread = cfg.costs.spread() if spread_pips is None else spread_pips
    entry = apply_costs_to_entry(ref, sig.direction, cfg, spread_pips)
    sl = stop_loss_price(sig.poi.upper, sig.poi.lower, sig.direction, cfg)
    # A short is closed at the ASK, but the zone and the stop are bid prices, so
    # an unadjusted short stop triggers a full spread earlier than the mirrored
    # long one -- it sits ON the boundary price just retested.  Push it out by a
    # spread so both directions are stopped at the same distance from the zone.
    if sig.direction is Direction.SHORT:
        sl = round_to_tick(sl + pips_to_price(eff_spread, spec), spec)

    # the stop must be on the correct side of the entry
    if sig.direction is Direction.LONG and sl >= entry:
        return OrderPlan(False, "sl_wrong_side")
    if sig.direction is Direction.SHORT and sl <= entry:
        return OrderPlan(False, "sl_wrong_side")

    sl_distance = abs(entry - sl)
    sl_pips = sl_distance / pip
    # 1.1010 - 1.0990 is 0.0020000000000000018 in IEEE-754, i.e. 20.000000000000004
    # pips.  Without this tolerance a stop of exactly the maximum is rejected.
    tol = 1e-6

    # HARD RULE: never compress the stop to fit; reject the trade instead.
    if cfg.trade.max_sl_pips > 0 and sl_pips > cfg.trade.max_sl_pips + tol:
        return OrderPlan(False, "sl_exceeds_max", sl_pips=sl_pips)
    if sl_pips < cfg.trade.min_sl_pips - tol:
        return OrderPlan(False, "sl_below_min", sl_pips=sl_pips)
    if spec.stops_level_points and sl_distance < spec.stops_level_points * spec.point:
        return OrderPlan(False, "below_broker_stops_level", sl_pips=sl_pips)
    # Ceiling in the POI's own volatility.  A fixed pip cap rejects wide zones in
    # a busy regime and passes them in a quiet one, and because it rejects rather
    # than compresses it quietly concentrates the book in the narrowest -- i.e.
    # highest cost-drag -- setups.  ATR travels; 20 pips does not.
    atr_o = getattr(sig.poi, "atr_origin", 0.0) or 0.0
    if cfg.trade.max_sl_atr_mult > 0 and atr_o > 0:
        if sl_distance > cfg.trade.max_sl_atr_mult * atr_o + tol * spec.point:
            return OrderPlan(False, "sl_exceeds_atr_cap", sl_pips=sl_pips)
    # How far past the zone the fill sits.  sl = overshoot + zone_width + buffer,
    # and only the overshoot is unbounded: without this a candle that merely
    # wicked the zone and closed 40 pips away is still "at" the POI.
    if cfg.trade.max_entry_dist_atr > 0 and atr_o > 0:
        overshoot = (entry - sig.poi.upper) if sig.direction is Direction.LONG \
                    else (sig.poi.lower - entry)
        if overshoot > cfg.trade.max_entry_dist_atr * atr_o:
            return OrderPlan(False, "entry_too_far_from_zone", sl_pips=sl_pips)
    # A stop only a spread or two wide needs a win rate the setup cannot deliver:
    # at 1:2 the breakeven is (1 + c/R)/3, so R = 4c already demands 41.7 %.  The
    # 20-pip cap selects *for* these trades, so without this floor the surviving
    # sample is concentrated in exactly the setups that cannot win.
    if cfg.trade.min_stop_spread_mult > 0:
        eff_spread = cfg.costs.spread() if spread_pips is None else spread_pips
        if sl_pips < cfg.trade.min_stop_spread_mult * eff_spread - tol:
            return OrderPlan(False, "stop_too_tight_vs_spread", sl_pips=sl_pips)

    tp = take_profit_price(entry, sl, sig.direction, cfg)
    volume, risk_amount = position_size(equity, sl_distance, cfg)
    if volume < spec.volume_min:
        if cfg.risk.reject_if_below_min_lot:
            return OrderPlan(False, "below_min_lot", sl_pips=sl_pips)
        volume = spec.volume_min
        risk_amount = volume * sl_distance * money_per_price_unit(spec)

    commission = volume * cfg.costs.commission_per_lot_round_turn
    return OrderPlan(True, "", sig.direction, entry, sl, tp, sl_distance, sl_pips,
                     volume, risk_amount, commission, pending, ref)


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
class RiskManager:
    """Portfolio-level vetoes.  Deliberately stateful and explicitly reset per
    backtest run so results are reproducible."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.reset(cfg.backtest.initial_equity)

    def reset(self, equity: float) -> None:
        self.equity = equity
        self.day: str = ""
        self.day_start_equity = equity
        self.trades_today = 0
        self.consecutive_losses = 0
        self.cooldown_until_index = -1
        self.open_positions = 0
        self.halted_days: set = set()

    def roll_day(self, ts: pd.Timestamp) -> None:
        d = trading_day(ts)
        if d != self.day:
            self.day = d
            self.day_start_equity = self.equity
            self.trades_today = 0
            # The consecutive-loss brake is a *daily* circuit breaker.  Without
            # this reset the counter can never be cleared (no trades -> no wins
            # -> no reset) and the strategy silently dies mid-backtest.
            self.consecutive_losses = 0

    def can_open(self, ts: pd.Timestamp, i: int,
                 spread_pips: float) -> Tuple[bool, str]:
        r = self.cfg.risk
        self.roll_day(ts)
        if self.open_positions >= r.max_open_positions:
            return False, "max_open_positions"
        if not in_session(ts, self.cfg.session):
            return False, "out_of_session"
        if spread_pips > r.max_spread_pips:
            return False, "spread_too_wide"
        if r.max_trades_per_day and self.trades_today >= r.max_trades_per_day:
            return False, "max_trades_per_day"
        if self.day in self.halted_days:
            return False, "daily_loss_halt"
        if r.max_daily_loss_pct > 0:
            dd = (self.day_start_equity - self.equity) / max(self.day_start_equity, 1e-9)
            if dd >= r.max_daily_loss_pct:
                self.halted_days.add(self.day)
                return False, "daily_loss_halt"
        if r.max_consecutive_losses and self.consecutive_losses >= r.max_consecutive_losses:
            return False, "consecutive_losses"
        if i < self.cooldown_until_index:
            return False, "cooldown"
        return True, ""

    def on_open(self) -> None:
        self.open_positions += 1
        self.trades_today += 1

    def on_close(self, pnl: float, exit_index: int) -> None:
        self.open_positions = max(0, self.open_positions - 1)
        self.equity += pnl
        r = self.cfg.risk
        if pnl < 0:
            self.consecutive_losses += 1
            self.cooldown_until_index = exit_index + r.cooldown_m5_bars_after_loss
        else:
            self.consecutive_losses = 0
            self.cooldown_until_index = exit_index + r.cooldown_m5_bars_after_any
