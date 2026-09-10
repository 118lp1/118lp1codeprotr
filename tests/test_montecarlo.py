"""Monte Carlo / walk-forward tests.

These check mechanics only.  No test here can validate the *assumptions* behind
a Monte Carlo simulation - that is a modelling judgement, documented in
montecarlo.py, not a property the code can assert.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading_bot.montecarlo import run_monte_carlo
from trading_bot.walkforward import make_folds


def _trades(rs) -> pd.DataFrame:
    return pd.DataFrame({"r_multiple": rs, "pnl": [r * 50 for r in rs]})


def test_reshuffle_preserves_the_final_return():
    """Order changes the path, never the destination (with linear sizing)."""
    t = _trades([2.0, -1.0, 2.0, -1.0, -1.0, 2.0])
    res = run_monte_carlo(t, risk_per_trade=0.01, n_sims=200, mode="reshuffle",
                          compound=False, seed=3)
    assert np.allclose(res.final_returns, res.final_returns[0])


def test_reshuffle_changes_the_drawdown():
    t = _trades([2.0, -1.0, 2.0, -1.0, -1.0, -1.0, 2.0, 2.0])
    res = run_monte_carlo(t, risk_per_trade=0.01, n_sims=500, mode="reshuffle",
                          compound=False, seed=3)
    assert res.max_drawdowns.std() > 0


def test_bootstrap_spreads_the_outcome_distribution():
    t = _trades([2.0, -1.0] * 25)
    res = run_monte_carlo(t, risk_per_trade=0.01, n_sims=500, mode="bootstrap", seed=3)
    assert res.final_returns.std() > 0
    assert res.summary["p05_return_pct"] < res.summary["p95_return_pct"]


def test_extra_slippage_can_only_hurt():
    t = _trades([2.0, -1.0] * 25)
    clean = run_monte_carlo(t, 0.01, n_sims=400, mode="bootstrap", seed=5)
    dirty = run_monte_carlo(t, 0.01, n_sims=400, mode="bootstrap",
                            extra_slippage_r=0.2, seed=5)
    assert dirty.summary["median_return_pct"] < clean.summary["median_return_pct"]


def test_monte_carlo_is_reproducible():
    t = _trades([2.0, -1.0] * 20)
    a = run_monte_carlo(t, 0.01, n_sims=200, seed=11)
    b = run_monte_carlo(t, 0.01, n_sims=200, seed=11)
    assert np.allclose(a.final_returns, b.final_returns)


def test_empty_trade_list_is_handled():
    res = run_monte_carlo(pd.DataFrame(), 0.005)
    assert res.paths.size == 0


def test_walkforward_windows_do_not_overlap_between_train_and_test():
    folds = make_folds(pd.Timestamp("2022-01-01"), pd.Timestamp("2024-01-01"),
                       train_months=6, valid_months=2, test_months=2)
    assert len(folds) > 0
    for f in folds:
        assert f.train[1] <= f.valid[0]
        assert f.valid[1] <= f.test[0]
        assert f.train[1] < f.test[0]


def test_walkforward_rolls_forward_by_the_test_length():
    folds = make_folds(pd.Timestamp("2022-01-01"), pd.Timestamp("2025-01-01"))
    assert folds[1].train[0] > folds[0].train[0]
    assert folds[1].test[0] == folds[0].test[0] + pd.DateOffset(months=2)
