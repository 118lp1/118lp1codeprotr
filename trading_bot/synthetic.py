"""Synthetic OHLCV generator.

PURPOSE: exercise the plumbing (indexing, alignment, execution, reporting) on a
machine with no market data.

NOT A PURPOSE: saying anything whatsoever about whether the strategy works.  A
random walk has no order blocks, no liquidity, no session structure and no
autocorrelation.  Any P/L produced from this data is noise by construction, and
the correct expectation on it is *negative* by exactly the transaction costs.
That property is actually useful: it is a sanity check.  If a backtest on a
driftless random walk shows a positive expectancy well beyond cost drag, the
engine has a look-ahead bug.
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pandas as pd


def generate_m1(n_days: int = 120, start: str = "2024-01-01",
                start_price: float = 1.1000, annual_vol: float = 0.07,
                seed: int = 7, drift: float = 0.0,
                session_only: bool = True, substeps: int = 12) -> pd.DataFrame:
    """Minute bars aggregated from a genuine sub-minute random walk.

    The sub-step simulation is not cosmetic.  An earlier version drew each
    bar's high and low as independent noise around the open/close, which is
    cheaper but produces bars whose extremes are not on any continuous path.
    Barrier-touch probabilities are then inflated, and because a 1:2 trade has
    its stop nearer than its target, the *near* barrier gains more.  That
    version reported a 21 % win rate on driftless data where theory says 33 %,
    which looks exactly like a broken strategy and was in fact broken test data.

    With a real path the benchmark is sharp: on a driftless walk the win rate
    must approach 1/3 and expectancy must approach -(costs in R).
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"),
                        periods=n_days * 24 * 60, freq="1min")
    idx = idx[idx.weekday < 5]
    if session_only:
        idx = idx[(idx.hour >= 6) & (idx.hour < 21)]
    n = len(idx)

    minutes_per_year = 252 * 24 * 60
    sigma = annual_vol / np.sqrt(minutes_per_year)
    # mild volatility clustering so ATR-based rules see a varying regime
    vol = sigma * np.exp(rng.normal(0, 0.35, n).cumsum() * 0.02)
    vol = np.clip(vol, sigma * 0.3, sigma * 3.0)

    step_sd = np.repeat(vol / np.sqrt(substeps), substeps)
    mu = drift / minutes_per_year / substeps
    path = start_price * np.exp(np.cumsum(rng.normal(mu, 1.0, n * substeps) * step_sd))
    grid = path.reshape(n, substeps)

    df = pd.DataFrame({
        "timestamp": idx,
        "open": grid[:, 0],
        "high": grid.max(axis=1),
        "low": grid.min(axis=1),
        "close": grid[:, -1],
        "volume": rng.integers(20, 400, n).astype(float),
    })
    price_cols = ["open", "high", "low", "close"]
    df[price_cols] = df[price_cols].round(5)
    # rounding must not break the OHLC invariant
    df["high"] = df[price_cols].max(axis=1)
    df["low"] = df[price_cols].min(axis=1)
    return df


def m1_to_m5(m1: pd.DataFrame) -> pd.DataFrame:
    from .data import resample
    out = resample(m1, 5)
    return out.drop(columns=["bar_end"])


def make_dataset(n_days: int = 120, seed: int = 7, **kw) -> Tuple[pd.DataFrame, pd.DataFrame]:
    m1 = generate_m1(n_days=n_days, seed=seed, **kw)
    return m5_from(m1), m1


def m5_from(m1: pd.DataFrame) -> pd.DataFrame:
    return m1_to_m5(m1)
