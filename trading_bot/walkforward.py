"""Walk-forward analysis.

Window layout per fold:

    |<-- TRAIN 6m -->|<-- VALID 2m -->|<-- TEST 2m -->|
                                       ^ only this is counted as out-of-sample

TRAIN selects candidate parameters, VALID picks among the top-k candidates
(this second stage exists so the winner is not simply the largest in-sample
fluke), TEST is executed exactly once with the frozen parameters and never
influences anything.  The window then rolls forward by the TEST length.

The aggregate of the TEST segments is the only performance number in this
project that is honest about the future.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .backtest import Backtester
from .config import Config
from .data import AlignedData, slice_period
from .metrics import summarise
from .optimization import DEFAULT_SPACE, evaluate, objective_value, random_search


def _cast(space_values: Sequence[Any], value: Any) -> Any:
    """Restore the parameter's original type.

    Values round-trip through a pandas DataFrame during the search, which
    promotes ints to float64.  A float where the code expects an int then blows
    up inside range() - two folds into a walk-forward run, after minutes of
    compute.  Cast back against the declared search space.
    """
    proto = space_values[0]
    if isinstance(proto, bool):
        return bool(value)
    if isinstance(proto, (int, np.integer)) and not isinstance(proto, bool):
        return int(round(float(value)))
    if isinstance(proto, float):
        return float(value)
    return value


@dataclass
class Fold:
    index: int
    train: Tuple[pd.Timestamp, pd.Timestamp]
    valid: Tuple[pd.Timestamp, pd.Timestamp]
    test: Tuple[pd.Timestamp, pd.Timestamp]


def make_folds(start: pd.Timestamp, end: pd.Timestamp, train_months: int = 6,
               valid_months: int = 2, test_months: int = 2) -> List[Fold]:
    folds: List[Fold] = []
    cur = pd.Timestamp(start)
    i = 0
    while True:
        tr_end = cur + pd.DateOffset(months=train_months)
        va_end = tr_end + pd.DateOffset(months=valid_months)
        te_end = va_end + pd.DateOffset(months=test_months)
        if te_end > end:
            break
        folds.append(Fold(i, (cur, tr_end), (tr_end, va_end), (va_end, te_end)))
        cur = cur + pd.DateOffset(months=test_months)
        i += 1
    return folds


def run_walkforward(cfg: Config, aligned: AlignedData,
                    space: Optional[Dict[str, Sequence[Any]]] = None,
                    train_months: int = 6, valid_months: int = 2,
                    test_months: int = 2, n_trials: int = 30,
                    top_k: int = 5, min_trades: int = 15,
                    seed: int = 7) -> Dict[str, Any]:
    space = space or DEFAULT_SPACE
    start = pd.Timestamp(aligned.m5["timestamp"].iloc[0])
    end = pd.Timestamp(aligned.m5["timestamp"].iloc[-1])
    folds = make_folds(start, end, train_months, valid_months, test_months)
    if not folds:
        return {"folds": pd.DataFrame(), "oos_trades": pd.DataFrame(),
                "note": "insufficient history for the requested window layout"}

    rows: List[Dict[str, Any]] = []
    oos_frames: List[pd.DataFrame] = []

    for f in folds:
        train_data = slice_period(aligned, *f.train)
        valid_data = slice_period(aligned, *f.valid)
        test_data = slice_period(aligned, *f.test)
        if len(train_data.m5) < 500 or len(test_data.m5) < 100:
            continue

        search = random_search(cfg, train_data, space, n_trials=n_trials,
                               seed=seed + f.index, min_trades=min_trades)
        cands = search.trials.head(top_k)
        best_params, best_valid = None, -np.inf
        for _, row in cands.iterrows():
            params = {k: _cast(space[k], row[k]) for k in space if k in row}
            score, _ = evaluate(cfg, valid_data, params, min_trades=max(min_trades // 2, 5))
            if score > best_valid:
                best_valid, best_params = score, params
        if best_params is None:
            best_params = search.best_params

        test_cfg = cfg.with_overrides(best_params)
        res = Backtester(test_cfg, log_events=False).run(test_data)
        s = summarise(res.trades, test_cfg, res.equity)
        if len(res.trades):
            oos_frames.append(res.trades.assign(fold=f.index))
        rows.append({
            "fold": f.index,
            "train": f"{f.train[0]:%Y-%m-%d}..{f.train[1]:%Y-%m-%d}",
            "test": f"{f.test[0]:%Y-%m-%d}..{f.test[1]:%Y-%m-%d}",
            "params": ", ".join(f"{k.split('.')[-1]}={v}" for k, v in best_params.items()),
            "trades": s.get("trades", 0),
            "win_rate": s.get("win_rate", float("nan")),
            "expectancy_r": s.get("expectancy_r", float("nan")),
            "profit_factor": s.get("profit_factor", float("nan")),
            "net_profit": s.get("net_profit", 0.0),
            "max_dd_pct": s.get("max_drawdown_pct", float("nan")),
        })

    oos = pd.concat(oos_frames, ignore_index=True) if oos_frames else pd.DataFrame()
    agg = summarise(oos, cfg) if len(oos) else {}
    return {"folds": pd.DataFrame(rows), "oos_trades": oos, "aggregate": agg}


def holdout_split(aligned: AlignedData, oos_fraction: float = 0.3
                  ) -> Tuple[AlignedData, AlignedData]:
    """Simple chronological in-sample / out-of-sample split (Stage 6)."""
    ts = aligned.m5["timestamp"]
    cut = ts.iloc[int(len(ts) * (1 - oos_fraction))]
    return (slice_period(aligned, ts.iloc[0], cut),
            slice_period(aligned, cut, ts.iloc[-1] + pd.Timedelta(minutes=5)))
