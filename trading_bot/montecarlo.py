"""Monte Carlo analysis of a realised trade sequence.

Three separate experiments, because they answer different questions:

1. **Reshuffle (no replacement)** - keeps the exact set of trades and only
   changes their order.  Answers: "how much of the drawdown was ordering luck?"
2. **Bootstrap (with replacement)** - resamples trades.  Answers: "what might a
   different sample of the same process look like?"
3. **Cost perturbation** - adds random extra slippage per trade in R units.
   Answers: "how fragile is this to execution quality?"

Limitations that must be stated whenever these numbers are quoted
-----------------------------------------------------------------
* All three assume trades are **independent and identically distributed**.
  Real trades are not: volatility clusters, so wins and losses cluster, which
  makes the true drawdown tail fatter than the reshuffle suggests.
* Resampling cannot invent market states the sample never contained.  If the
  sample has no 2015-CHF-style event, no simulation will produce one.
* The trade distribution is itself estimated from a finite sample.  With ~50
  trades, the tail of that distribution is essentially unknown, and any
  "probability of ruin" printed below inherits that uncertainty.
* Percentage-based sizing is assumed; the sequences are computed in R and then
  compounded, which understates path dependence of a fixed-lot account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd


@dataclass
class MonteCarloResult:
    summary: Dict[str, float]
    paths: np.ndarray            # (n_sims, n_trades + 1) equity in % of start
    max_drawdowns: np.ndarray
    final_returns: np.ndarray


def _simulate(r_seq: np.ndarray, risk_per_trade: float,
              compound: bool = True) -> np.ndarray:
    """Turn an R sequence into an equity path (start = 1.0)."""
    if compound:
        return np.cumprod(np.concatenate([[1.0], 1.0 + r_seq * risk_per_trade]))
    return np.concatenate([[1.0], 1.0 + np.cumsum(r_seq * risk_per_trade)])


def run_monte_carlo(trades: pd.DataFrame, risk_per_trade: float = 0.005,
                    n_sims: int = 5_000, mode: str = "reshuffle",
                    extra_slippage_r: float = 0.0, seed: int = 7,
                    ruin_threshold: float = 0.5,
                    compound: bool = True) -> MonteCarloResult:
    if trades is None or len(trades) == 0:
        return MonteCarloResult({"note": float("nan")}, np.empty((0, 0)),
                                np.empty(0), np.empty(0))
    r = trades["r_multiple"].to_numpy(dtype=float)
    n = len(r)
    rng = np.random.default_rng(seed)
    paths = np.empty((n_sims, n + 1))

    for s in range(n_sims):
        if mode == "bootstrap":
            seq = rng.choice(r, size=n, replace=True)
        else:
            seq = rng.permutation(r)
        if extra_slippage_r > 0:
            seq = seq - np.abs(rng.normal(0.0, extra_slippage_r, size=n))
        paths[s] = _simulate(seq, risk_per_trade, compound)

    peaks = np.maximum.accumulate(paths, axis=1)
    dd = (peaks - paths) / np.maximum(peaks, 1e-12)
    max_dd = dd.max(axis=1)
    final = paths[:, -1] - 1.0

    summary = {
        "sims": float(n_sims),
        "trades_per_sim": float(n),
        "median_return_pct": float(np.median(final) * 100),
        "mean_return_pct": float(final.mean() * 100),
        "p05_return_pct": float(np.percentile(final, 5) * 100),
        "p95_return_pct": float(np.percentile(final, 95) * 100),
        "prob_negative": float((final < 0).mean()),
        "median_max_dd_pct": float(np.median(max_dd) * 100),
        "p95_max_dd_pct": float(np.percentile(max_dd, 95) * 100),
        "worst_max_dd_pct": float(max_dd.max() * 100),
        "prob_dd_gt_10pct": float((max_dd > 0.10).mean()),
        "prob_dd_gt_20pct": float((max_dd > 0.20).mean()),
        f"prob_ruin_{int(ruin_threshold*100)}pct": float((max_dd >= ruin_threshold).mean()),
    }
    return MonteCarloResult(summary, paths, max_dd, final)


def monte_carlo_table(trades: pd.DataFrame, risk_per_trade: float = 0.005,
                      seed: int = 7) -> pd.DataFrame:
    rows = []
    for mode, slip in (("reshuffle", 0.0), ("bootstrap", 0.0),
                       ("bootstrap", 0.05), ("bootstrap", 0.10)):
        res = run_monte_carlo(trades, risk_per_trade, mode=mode,
                              extra_slippage_r=slip, seed=seed)
        row = {"mode": mode, "extra_slippage_R": slip}
        row.update(res.summary)
        rows.append(row)
    return pd.DataFrame(rows)
