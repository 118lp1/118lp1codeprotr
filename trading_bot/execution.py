"""Execution: MT5 connection, order routing, and the live/paper trading loop.

Safety properties this module is responsible for
------------------------------------------------
* ``Config.mode`` must be LIVE **and** the caller must pass ``confirm=True``
  before a single real order is sent.  Two independent switches.
* Signals are recomputed from scratch over a rolling history buffer on every
  closed M5 bar.  Restarting the process therefore reproduces exactly the same
  strategy state - no serialised state to corrupt or drift.
* Every intended trade gets a deterministic ``client_key``
  (symbol|poi_id|engulf_time).  It is written to SQLite before the order goes
  out, so a crash-and-restart cannot double-enter the same setup.
* Only bars that are fully closed are ever fed to the engine: the last bar
  returned by MT5 is dropped unconditionally.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .config import Config, Direction, Mode, SymbolSpec
from .data import build_aligned, clean_ohlcv
from .logger import EventLog, TradeStore, get_logger
from .risk import RiskManager, build_order
from .strategy import Signal, StrategyEngine
from .utils import pip_size


# --------------------------------------------------------------------------- #
@dataclass
class OrderResult:
    ok: bool
    ticket: int = 0
    price: float = 0.0
    comment: str = ""
    retcode: int = 0


class BrokerBase:
    def connect(self) -> bool: raise NotImplementedError
    def symbol_spec(self, symbol: str) -> SymbolSpec: raise NotImplementedError
    def bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame: raise NotImplementedError
    def tick(self, symbol: str) -> Tuple[float, float]: raise NotImplementedError
    def equity(self) -> float: raise NotImplementedError
    def positions(self, symbol: str, magic: int) -> List[Dict[str, Any]]: raise NotImplementedError
    def send_market(self, *a, **k) -> OrderResult: raise NotImplementedError
    def close(self) -> None: pass


# --------------------------------------------------------------------------- #
class MT5Broker(BrokerBase):
    def __init__(self, cfg: Config, login: Optional[int] = None,
                 password: Optional[str] = None, server: Optional[str] = None,
                 terminal_path: Optional[str] = None, magic: int = 900101) -> None:
        self.cfg = cfg
        self.log = get_logger("mt5", cfg.log_level, "logs")
        self.creds = (login, password, server, terminal_path)
        self.magic = magic
        self._mt5 = None

    # -- connection --------------------------------------------------------- #
    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError as exc:
            raise RuntimeError("MetaTrader5 package not installed (Windows only)") from exc
        self._mt5 = mt5
        login, password, server, path = self.creds
        kwargs: Dict[str, Any] = {}
        if path:
            kwargs["path"] = path
        if login:
            kwargs.update(login=int(login), password=password, server=server)
        if not mt5.initialize(**kwargs):
            self.log.error("initialize failed: %s", mt5.last_error())
            return False
        info = mt5.account_info()
        if info is None:
            self.log.error("account_info failed: %s", mt5.last_error())
            return False
        self.log.info("connected: login=%s server=%s balance=%.2f currency=%s",
                      info.login, info.server, info.balance, info.currency)
        if not mt5.symbol_select(self.cfg.data.symbol, True):
            self.log.error("symbol_select failed for %s", self.cfg.data.symbol)
            return False
        return True

    def ensure_connected(self, retries: int = 5, delay: float = 5.0) -> bool:
        for attempt in range(retries):
            try:
                if self._mt5 is not None and self._mt5.terminal_info() is not None:
                    return True
                self.log.warning("reconnecting (attempt %d/%d)", attempt + 1, retries)
                if self._mt5 is not None:
                    self._mt5.shutdown()
                if self.connect():
                    return True
            except Exception as exc:  # noqa: BLE001
                self.log.error("reconnect error: %s", exc)
            time.sleep(delay * (attempt + 1))
        return False

    # -- market data -------------------------------------------------------- #
    def symbol_spec(self, symbol: str) -> SymbolSpec:
        mt5 = self._mt5
        s = mt5.symbol_info(symbol)
        if s is None:
            raise RuntimeError(f"symbol_info({symbol}) failed: {mt5.last_error()}")
        return SymbolSpec(
            name=s.name, digits=s.digits, point=s.point,
            tick_size=s.trade_tick_size or s.point,
            tick_value=s.trade_tick_value, contract_size=s.trade_contract_size,
            volume_min=s.volume_min, volume_max=s.volume_max,
            volume_step=s.volume_step, stops_level_points=s.trade_stops_level)

    def bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        mt5 = self._mt5
        tf = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
              "M15": mt5.TIMEFRAME_M15}[timeframe]
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"copy_rates_from_pos failed: {mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        off = self.cfg.data.broker_utc_offset_hours
        if off:
            df["timestamp"] = df["timestamp"] - pd.Timedelta(hours=off)
        df = df.rename(columns={"tick_volume": "volume"})
        cols = ["timestamp", "open", "high", "low", "close", "volume"]
        if "spread" in df.columns:
            cols.append("spread")
        # ALWAYS drop the last (still forming) bar
        return clean_ohlcv(df[cols].iloc[:-1]).reset_index(drop=True)

    def tick(self, symbol: str) -> Tuple[float, float]:
        t = self._mt5.symbol_info_tick(symbol)
        if t is None:
            raise RuntimeError("symbol_info_tick failed")
        return float(t.bid), float(t.ask)

    def equity(self) -> float:
        info = self._mt5.account_info()
        return float(info.equity) if info else 0.0

    def positions(self, symbol: str, magic: int) -> List[Dict[str, Any]]:
        pos = self._mt5.positions_get(symbol=symbol) or []
        return [p._asdict() for p in pos if p.magic == magic]

    # -- orders ------------------------------------------------------------- #
    def _filling_mode(self, symbol: str) -> int:
        mt5 = self._mt5
        info = mt5.symbol_info(symbol)
        mode = getattr(info, "filling_mode", 0)
        if mode & 2:
            return mt5.ORDER_FILLING_IOC
        if mode & 1:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def send_market(self, symbol: str, direction: Direction, volume: float,
                    sl: float, tp: float, comment: str,
                    deviation_points: int = 20) -> OrderResult:
        mt5 = self._mt5
        bid, ask = self.tick(symbol)
        price = ask if direction is Direction.LONG else bid
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
            "type": mt5.ORDER_TYPE_BUY if direction is Direction.LONG else mt5.ORDER_TYPE_SELL,
            "price": price, "sl": float(sl), "tp": float(tp),
            "deviation": deviation_points, "magic": self.magic,
            "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(symbol),
        }
        res = mt5.order_send(req)
        if res is None:
            return OrderResult(False, comment=str(mt5.last_error()))
        ok = res.retcode == mt5.TRADE_RETCODE_DONE
        if not ok:
            self.log.error("order_send rejected retcode=%s comment=%s", res.retcode, res.comment)
        return OrderResult(ok, getattr(res, "order", 0), getattr(res, "price", 0.0),
                           getattr(res, "comment", ""), res.retcode)

    def modify(self, ticket: int, sl: float, tp: float) -> bool:
        mt5 = self._mt5
        pos = [p for p in (mt5.positions_get(ticket=ticket) or [])]
        if not pos:
            return False
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket,
               "symbol": pos[0].symbol, "sl": float(sl), "tp": float(tp)}
        res = mt5.order_send(req)
        return res is not None and res.retcode == mt5.TRADE_RETCODE_DONE

    def close(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()


# --------------------------------------------------------------------------- #
class PaperBroker(BrokerBase):
    """Stage 9: identical interface, no orders leave the machine.

    Feeds itself from a live MT5 connection if available, otherwise from a CSV
    replay, so paper trading exercises exactly the same code path as live.
    """

    def __init__(self, cfg: Config, feed: BrokerBase, start_equity: float = 10_000.0):
        self.cfg = cfg
        self.feed = feed
        self._equity = start_equity
        self.open_positions: List[Dict[str, Any]] = []
        self.log = get_logger("paper", cfg.log_level, "logs")

    def connect(self) -> bool: return self.feed.connect()
    def symbol_spec(self, symbol): return self.feed.symbol_spec(symbol)
    def bars(self, symbol, timeframe, count): return self.feed.bars(symbol, timeframe, count)
    def tick(self, symbol): return self.feed.tick(symbol)
    def equity(self) -> float: return self._equity
    def positions(self, symbol, magic): return list(self.open_positions)

    def send_market(self, symbol, direction, volume, sl, tp, comment, **kw) -> OrderResult:
        bid, ask = self.tick(symbol)
        price = ask if direction is Direction.LONG else bid
        ticket = len(self.open_positions) + 1
        self.open_positions.append({"ticket": ticket, "symbol": symbol,
                                    "direction": direction.value, "volume": volume,
                                    "price_open": price, "sl": sl, "tp": tp,
                                    "comment": comment})
        self.log.info("PAPER order %s %s %.2f lots @ %.5f sl=%.5f tp=%.5f",
                      direction.value, symbol, volume, price, sl, tp)
        return OrderResult(True, ticket, price, "paper")


# --------------------------------------------------------------------------- #
class LiveTrader:
    """The live/paper loop.  Deliberately boring and single-threaded."""

    def __init__(self, cfg: Config, broker: BrokerBase, confirm: bool = False,
                 history_bars: int = 3000, magic: int = 900101,
                 db_path: str = "state/trades.db") -> None:
        if cfg.mode is Mode.LIVE and not confirm:
            raise RuntimeError(
                "Refusing to start LIVE trading: pass confirm=True *and* set "
                "mode: LIVE in the config file. Both switches are required.")
        self.cfg = cfg
        self.broker = broker
        self.magic = magic
        self.history_bars = history_bars
        self.store = TradeStore(db_path)
        self.events = EventLog()
        self.log = get_logger("live", cfg.log_level, "logs")
        self.risk = RiskManager(cfg)
        self._last_bar_time: Optional[pd.Timestamp] = None

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def client_key(symbol: str, sig: Signal) -> str:
        return f"{symbol}|{sig.poi.poi_id}|{pd.Timestamp(sig.engulf_time).isoformat()}"

    def _spread_pips(self) -> float:
        bid, ask = self.broker.tick(self.cfg.data.symbol)
        return (ask - bid) / pip_size(self.cfg.symbol_spec)

    # -- one iteration ------------------------------------------------------ #
    def poll_once(self) -> Optional[Signal]:
        symbol = self.cfg.data.symbol
        m5 = self.broker.bars(symbol, "M5", self.history_bars)
        if not len(m5):
            return None
        last_closed = pd.Timestamp(m5["timestamp"].iloc[-1])
        if self._last_bar_time is not None and last_closed <= self._last_bar_time:
            return None                      # no new closed bar yet
        self._last_bar_time = last_closed

        aligned = build_aligned(m5)
        engine = StrategyEngine(self.cfg, EventLog(enabled=False))
        engine.prepare(aligned)
        signals: List[Signal] = []
        for i in range(len(aligned.m5)):
            signals = engine.step(i)         # full deterministic replay
        if not signals:
            self.log.debug("bar %s: no signal", last_closed)
            return None

        sig = signals[0]
        key = self.client_key(symbol, sig)
        if self.store.already_traded(key):
            self.log.info("duplicate suppressed for %s", key)
            return None

        equity = self.broker.equity()
        self.risk.equity = equity
        self.risk.open_positions = len(self.broker.positions(symbol, self.magic))
        spread = self._spread_pips()
        now = pd.Timestamp.utcnow().tz_localize(None).tz_localize("UTC")
        ok, reason = self.risk.can_open(now, 0, spread)
        if not ok:
            self.log.info("signal vetoed: %s", reason)
            return None

        bid, ask = self.broker.tick(symbol)
        ref = ask if sig.direction is Direction.LONG else bid
        plan = build_order(sig, bid, equity, self.cfg, spread_pips=spread)
        if not plan.ok:
            self.log.info("signal rejected by risk: %s (sl=%.1f pips)",
                          plan.reason, plan.sl_pips)
            return None

        self.store.record({
            "client_key": key, "strategy_version": self.cfg.strategy_version,
            "symbol": symbol, "direction": plan.direction.value,
            "signal_time": str(sig.signal_time), "entry_time": str(now),
            "entry": plan.entry, "sl": plan.sl, "tp": plan.tp,
            "volume": plan.volume, "sl_pips": plan.sl_pips,
            "risk_amount": plan.risk_amount, "poi_time": str(sig.poi.origin_time),
            "poi_type": sig.poi.poi_type.value, "poi_high": sig.poi.upper,
            "poi_low": sig.poi.lower, "retest_time": str(sig.poi.retest_time),
            "engulf_time": str(sig.engulf_time), "status": "pending",
        })
        res = self.broker.send_market(symbol, plan.direction, plan.volume,
                                      plan.sl, plan.tp,
                                      comment=f"{self.cfg.strategy_version[:12]}")
        self.store.update(key, status="open" if res.ok else "failed",
                          ticket=res.ticket, entry=res.price or plan.entry)
        self.log.info("order %s ticket=%s price=%.5f", "OK" if res.ok else "FAILED",
                      res.ticket, res.price)
        if res.ok:
            self.risk.on_open()
        return sig if res.ok else None

    def run(self, poll_seconds: float = 15.0, max_iterations: int = 0) -> None:
        self.log.info("starting %s loop for %s (version %s)", self.cfg.mode.value,
                      self.cfg.data.symbol, self.cfg.strategy_version)
        it = 0
        while True:
            it += 1
            try:
                if isinstance(self.broker, MT5Broker) and not self.broker.ensure_connected():
                    self.log.error("cannot reach terminal; sleeping")
                    time.sleep(60)
                    continue
                self.poll_once()
                self.reconcile()
            except KeyboardInterrupt:
                self.log.info("stopped by user")
                break
            except Exception as exc:  # noqa: BLE001
                self.log.exception("loop error: %s", exc)
                time.sleep(10)
            if max_iterations and it >= max_iterations:
                break
            time.sleep(poll_seconds)

    def reconcile(self) -> None:
        """Mark DB rows closed when the broker no longer reports the position."""
        symbol = self.cfg.data.symbol
        live_tickets = {p.get("ticket") for p in self.broker.positions(symbol, self.magic)}
        for row in self.store.open_trades():
            if row.get("ticket") and row["ticket"] not in live_tickets:
                self.store.update(row["client_key"], status="closed",
                                  exit_time=str(pd.Timestamp.utcnow()))
                self.log.info("position %s closed (reconciled)", row["ticket"])
