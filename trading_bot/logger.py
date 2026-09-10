"""Structured logging.

Two layers:
  * ``get_logger``  - ordinary human/ops logging (console + rotating file).
  * ``EventLog``    - machine-readable event stream (POI created, retest,
                      engulf, rejection reason, entry, exit, risk violation)
                      persisted to CSV and/or SQLite for later analysis.

Rejection reasons are logged as first-class events on purpose: the ratio of
rejected-to-taken setups is one of the few honest diagnostics of whether a
rule set is doing anything at all.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

_FMT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"


def get_logger(name: str = "bot", level: str = "INFO",
               log_dir: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        return logger
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(sh)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(log_dir, f"{name}.log"),
                                 maxBytes=5_000_000, backupCount=3)
        fh.setFormatter(logging.Formatter(_FMT))
        logger.addHandler(fh)
    logger.propagate = False
    return logger


@dataclass
class EventLog:
    """Append-only event store.  Cheap in-memory by default."""
    enabled: bool = True
    events: List[Dict[str, Any]] = field(default_factory=list)
    max_events: int = 500_000

    def log(self, ts: Any, kind: str, **payload: Any) -> None:
        if not self.enabled or len(self.events) >= self.max_events:
            return
        row = {"timestamp": str(ts), "event": kind}
        row.update({k: v for k, v in payload.items()})
        self.events.append(row)

    # -- persistence ------------------------------------------------------- #
    def to_csv(self, path: str) -> None:
        if not self.events:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        keys: List[str] = []
        for e in self.events:
            for k in e:
                if k not in keys:
                    keys.append(k)
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            for e in self.events:
                w.writerow(e)

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for e in self.events:
            out[e["event"]] = out.get(e["event"], 0) + 1
        return out

    def rejection_counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for e in self.events:
            if e["event"] == "reject":
                r = str(e.get("reason", "?"))
                out[r] = out.get(r, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))


class TradeStore:
    """SQLite persistence for live/paper trades and bot state.

    The ``client_key`` unique index is what prevents duplicate entries when the
    live script restarts mid-setup.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_key TEXT UNIQUE,
        strategy_version TEXT, symbol TEXT, direction TEXT,
        signal_time TEXT, entry_time TEXT, entry REAL, sl REAL, tp REAL,
        volume REAL, sl_pips REAL, risk_amount REAL,
        poi_time TEXT, poi_type TEXT, poi_high REAL, poi_low REAL,
        retest_time TEXT, engulf_time TEXT,
        exit_time TEXT, exit_price REAL, profit REAL, r_multiple REAL,
        exit_reason TEXT, ticket INTEGER, status TEXT, extra TEXT
    );
    CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
    """

    def __init__(self, path: str = "state/trades.db") -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    def already_traded(self, client_key: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM trades WHERE client_key=?", (client_key,))
        return cur.fetchone() is not None

    def record(self, row: Dict[str, Any]) -> None:
        cols = [c for c in row if c != "id"]
        sql = (f"INSERT OR IGNORE INTO trades ({','.join(cols)}) "
               f"VALUES ({','.join('?' * len(cols))})")
        self.conn.execute(sql, [_coerce(row[c]) for c in cols])
        self.conn.commit()

    def update(self, client_key: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE trades SET {sets} WHERE client_key=?",
                          [_coerce(v) for v in fields.values()] + [client_key])
        self.conn.commit()

    def open_trades(self) -> List[Dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM trades WHERE status='open'")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_state(self, key: str) -> Optional[str]:
        cur = self.conn.execute("SELECT v FROM state WHERE k=?", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO state (k, v) VALUES (?, ?)",
                          (key, json.dumps(value, default=str)))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _coerce(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str, bytes)):
        return v
    return str(v)
