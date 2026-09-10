"""Constrained parameter search.

Deliberate design choices:

* The objective is **not** net profit.  It is a penalised expectancy that
  requires a minimum trade count, because maximising profit over a small
  parameter grid on a small sample is just a machine for finding noise.
* Search ranges are coarse.  A grid with 0.05-granularity on an ATR multiplier
  is false precision: the data cannot resolve it.
* Optuna is optional.  Without it, a seeded random search runs, which is
  reproducible and good enough for a space this size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .backtest import Backtester
from .config import Config
from .data import AlignedData
from .metrics import summarise

# Coarse, defensible search space.  Keys are dotted config paths.
DEFAULT_SPACE: Dict[str, Sequence[Any]] = {
    "poi.displacement_atr_mult": [0.6, 0.8, 1.0, 1.3, 1.6],
    "poi.max_leg_bars": [3, 5, 7],
    "poi.expiry_m15_bars": [48, 96, 192],
    "retest.min_departure_pips": [5.0, 8.0, 12.0, 16.0],
    "retest.max_m5_bars_to_retest": [144, 288, 576],
    "engulf.max_m5_bars_after_retest": [3, 6, 9],
    "engulf.min_body_ratio": [1.0, 1.2, 1.5],
    "trade.sl_buffer_pips": [0.5, 1.0, 2.0],
}


@dataclass
class OptimisationResult:
    best_params: Dict[str, Any]
    best_score: float
    trials: pd.DataFrame


def objective_value(summary: Dict[str, float], min_trades: int = 30) -> float:
    """Penalised expectancy.

    ``expectancy_r * sqrt(n)`` is (up to a constant) the t-statistic: it rewards
    an edge that is both real in size and supported by sample size, instead of
    rewarding a lucky 4-trade run.  Below ``min_trades`` the score is crushed.
    """
    n = summary.get("trades", 0)
    if not n or n < min_trades:
        return -1e6 + n
    exp_r = summary.get("expectancy_r", float("nan"))
    if not np.isfinite(exp_r):
        return -1e6
    dd = max(summary.get("max_drawdown_pct", 0.0), 1e-6)
    score = exp_r * np.sqrt(n)
    if dd > 25:                      # soft penalty for unusable risk profiles
        score -= (dd - 25) / 10.0
    return float(score)


def evaluate(cfg: Config, aligned: AlignedData, overrides: Dict[str, Any],
             min_trades: int = 30) -> Tuple[float, Dict[str, float]]:
    c = cfg.with_overrides(overrides)
    res = Backtester(c, log_events=False).run(aligned)
    s = summarise(res.trades, c, res.equity)
    return objective_value(s, min_trades), s


def random_search(cfg: Config, aligned: AlignedData,
                  space: Optional[Dict[str, Sequence[Any]]] = None,
                  n_trials: int = 40, seed: int = 7,
                  min_trades: int = 30) -> OptimisationResult:
    space = space or DEFAULT_SPACE
    rng = np.random.default_rng(seed)
    rows: List[Dict[str, Any]] = []
    best, best_params = -np.inf, {}
    seen = set()
    for _ in range(n_trials):
        params = {k: v[int(rng.integers(len(v)))] for k, v in space.items()}
        # keep native python types: numpy scalars leak into range() downstream
        key = tuple(sorted(params.items()))
        if key in seen:
            continue
        seen.add(key)
        score, s = evaluate(cfg, aligned, params, min_trades)
        rows.append({**params, "score": score, **{k: s.get(k) for k in
                     ("trades", "win_rate", "expectancy_r", "profit_factor",
                      "net_profit", "max_drawdown_pct")}})
        if score > best:
            best, best_params = score, params
    return OptimisationResult(best_params, best, pd.DataFrame(rows).sort_values(
        "score", ascending=False).reset_index(drop=True))


def optuna_search(cfg: Config, aligned: AlignedData,
                  space: Optional[Dict[str, Sequence[Any]]] = None,
                  n_trials: int = 60, seed: int = 7,
                  min_trades: int = 30) -> OptimisationResult:
    try:
        import optuna
    except ImportError:
        return random_search(cfg, aligned, space, n_trials, seed, min_trades)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    space = space or DEFAULT_SPACE

    def obj(trial: "optuna.Trial") -> float:
        params = {k: trial.suggest_categorical(k, list(v)) for k, v in space.items()}
        score, _ = evaluate(cfg, aligned, params, min_trades)
        return score

    study = optuna.create_study(direction="maximize",
                               sampler=optuna.samplers.TPESampler(seed=seed))
    study.optimize(obj, n_trials=n_trials, show_progress_bar=False)
    trials = study.trials_dataframe()
    return OptimisationResult(study.best_params, study.best_value, trials)


def sensitivity(cfg: Config, aligned: AlignedData,
                space: Optional[Dict[str, Sequence[Any]]] = None,
                metric: str = "expectancy_r") -> pd.DataFrame:
    """One-parameter-at-a-time sweep around the current configuration.

    A parameter whose neighbours collapse to nothing is a sign the result sits
    on a spike, not a plateau - the classic overfitting signature.
    """
    space = space or DEFAULT_SPACE
    rows = []
    for key, values in space.items():
        for v in values:
            _, s = evaluate(cfg, aligned, {key: v}, min_trades=1)
            rows.append({"parameter": key, "value": v,
                         "trades": s.get("trades", 0),
                         metric: s.get(metric, float("nan")),
                         "profit_factor": s.get("profit_factor", float("nan"))})
    df = pd.DataFrame(rows)
    stab = (df.groupby("parameter")[metric]
              .agg(["mean", "std", "min", "max"])
              .rename(columns={"mean": "mean_metric", "std": "std_metric"}))
    return df.merge(stab, on="parameter", how="left")


def cost_stress(cfg: Config, aligned: AlignedData) -> pd.DataFrame:
    """Section 14: spread / slippage sensitivity grid."""
    rows = []
    for sp in (1.0, 1.5, 2.0):
        for sl in (1.0, 2.0):
            _, s = evaluate(cfg, aligned,
                            {"costs.spread_multiplier": sp,
                             "costs.slippage_multiplier": sl}, min_trades=1)
            rows.append({"spread_x": sp, "slippage_x": sl,
                         "trades": s.get("trades", 0),
                         "win_rate": s.get("win_rate", float("nan")),
                         "expectancy_r": s.get("expectancy_r", float("nan")),
                         "profit_factor": s.get("profit_factor", float("nan")),
                         "net_profit": s.get("net_profit", 0.0)})
    return pd.DataFrame(rows)
