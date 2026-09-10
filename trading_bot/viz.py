"""Charts.  matplotlib only (Plotly optional and not required anywhere).

The per-trade chart is the most important one in the project: it is how you
verify that the *code* found the setup you think it found.  Stage 3 of the
research workflow is 'eyeball 30 random signals'; skipping it is how silent
indexing bugs survive into a live account.
"""

from __future__ import annotations

import os
from typing import Optional, Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def _utc(ts) -> pd.Timestamp:
    """Timestamps arrive from CSV as naive strings and from memory as tz-aware.
    Comparing the two raises, so everything is normalised here."""
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _save(fig, path: Optional[str]):
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fig.savefig(path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return path
    return fig


def equity_curve(equity: pd.DataFrame, trades: Optional[pd.DataFrame] = None,
                 path: Optional[str] = None):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [2, 1]})
    eq = equity["equity"].to_numpy()
    ts = pd.to_datetime(equity["timestamp"])
    ax1.plot(ts, eq, lw=1.2, color="#1f77b4")
    ax1.set_title("Equity curve")
    ax1.set_ylabel("Equity")
    ax1.grid(alpha=0.3)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / np.maximum(peak, 1e-9) * 100
    ax2.fill_between(ts, -dd, 0, color="#d62728", alpha=0.5)
    ax2.set_ylabel("Drawdown %")
    ax2.grid(alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    return _save(fig, path)


def r_distribution(trades: pd.DataFrame, path: Optional[str] = None):
    fig, ax = plt.subplots(figsize=(8, 4.5))
    r = trades["r_multiple"].to_numpy(dtype=float)
    ax.hist(r, bins=30, color="#4c72b0", edgecolor="white")
    ax.axvline(0, color="k", lw=1)
    ax.axvline(r.mean(), color="#d62728", ls="--", lw=1.4,
               label=f"mean = {r.mean():+.3f}R")
    ax.set_title("R-multiple distribution")
    ax.set_xlabel("R")
    ax.legend()
    ax.grid(alpha=0.3)
    return _save(fig, path)


def monthly_returns(trades: pd.DataFrame, initial_equity: float,
                    path: Optional[str] = None):
    t = trades.copy()
    t["entry_time"] = pd.to_datetime(t["entry_time"], utc=True).dt.tz_localize(None)
    m = t.groupby(t["entry_time"].dt.to_period("M"))["pnl"].sum()
    fig, ax = plt.subplots(figsize=(10, 4))
    colors = ["#2ca02c" if v > 0 else "#d62728" for v in m.values]
    ax.bar([str(p) for p in m.index], m.values / initial_equity * 100, color=colors)
    ax.set_title("Monthly return (% of starting equity)")
    ax.grid(alpha=0.3, axis="y")
    plt.xticks(rotation=90, fontsize=7)
    return _save(fig, path)


def win_loss_distribution(trades: pd.DataFrame, path: Optional[str] = None):
    fig, ax = plt.subplots(figsize=(8, 4.5))
    wins = trades.loc[trades["pnl"] > 0, "pnl"]
    losses = trades.loc[trades["pnl"] < 0, "pnl"]
    ax.hist([wins, losses], bins=20, stacked=False,
            color=["#2ca02c", "#d62728"], label=["wins", "losses"])
    ax.set_title("Win / loss distribution (money)")
    ax.legend()
    ax.grid(alpha=0.3)
    return _save(fig, path)


def monte_carlo_paths(paths: np.ndarray, n_show: int = 200,
                      path: Optional[str] = None):
    fig, ax = plt.subplots(figsize=(10, 5))
    idx = np.linspace(0, len(paths) - 1, min(n_show, len(paths))).astype(int)
    for p in paths[idx]:
        ax.plot((p - 1) * 100, color="#1f77b4", alpha=0.06, lw=0.8)
    ax.plot((np.median(paths, axis=0) - 1) * 100, color="k", lw=1.8, label="median")
    ax.plot((np.percentile(paths, 5, axis=0) - 1) * 100, color="#d62728", lw=1.2,
            ls="--", label="5th pct")
    ax.set_title("Monte Carlo equity paths")
    ax.set_xlabel("trade #")
    ax.set_ylabel("return %")
    ax.legend()
    ax.grid(alpha=0.3)
    return _save(fig, path)


def plot_trade(trade: pd.Series, m5: pd.DataFrame, m15: pd.DataFrame,
               bars_before: int = 60, bars_after: int = 40,
               path: Optional[str] = None):
    """M5 candles with the M15 POI, retest, engulfing bar, entry, SL and TP."""
    ts_all = pd.to_datetime(m5["timestamp"], utc=True)
    entry_time = _utc(trade["entry_time"])
    i = int(ts_all.searchsorted(entry_time))
    lo, hi = max(0, i - bars_before), min(len(m5), i + bars_after)
    w = m5.iloc[lo:hi]
    fig, ax = plt.subplots(figsize=(13, 6))
    x = np.arange(len(w))
    for k, (_, c) in enumerate(w.iterrows()):
        color = "#2ca02c" if c["close"] >= c["open"] else "#d62728"
        ax.vlines(k, c["low"], c["high"], color=color, lw=0.8)
        ax.add_patch(plt.Rectangle((k - 0.3, min(c["open"], c["close"])), 0.6,
                                   max(abs(c["close"] - c["open"]), 1e-9),
                                   color=color, alpha=0.85))
    ax.axhspan(trade["poi_low"], trade["poi_high"], color="#ff7f0e", alpha=0.18,
               label=f"M15 POI ({trade['poi_type']})")
    for key, color, style, label in (("sl", "#d62728", "--", "SL"),
                                     ("tp", "#2ca02c", "--", "TP"),
                                     ("entry", "#1f77b4", "-", "Entry")):
        ax.axhline(trade[key], color=color, ls=style, lw=1.2, label=label)

    ts_win = pd.to_datetime(w["timestamp"], utc=True).reset_index(drop=True)

    def mark(ts, label, color):
        if ts is None or pd.isna(ts):
            return
        k = int(ts_win.searchsorted(_utc(ts)))
        if 0 <= k < len(w):
            ax.axvline(k, color=color, ls=":", lw=1.1)
            ax.text(k, ax.get_ylim()[1], label, rotation=90, va="top",
                    fontsize=8, color=color)

    mark(trade.get("retest_time"), "retest", "#8c564b")
    mark(trade.get("engulf_time"), "engulf", "#9467bd")
    mark(trade.get("exit_time"), f"exit:{trade.get('exit_reason','')}", "#333333")
    step = max(len(w) // 12, 1)
    ax.set_xticks(x[::step])
    ax.set_xticklabels([pd.Timestamp(t).strftime("%m-%d %H:%M")
                        for t in w["timestamp"].iloc[::step]], rotation=45, fontsize=8)
    ax.set_title(f"Trade #{trade['trade_id']} {trade['direction']} "
                 f"{trade['exit_reason']} R={trade['r_multiple']:+.2f}")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.25)
    return _save(fig, path)


def report_charts(result, out_dir: str, sample_trades: int = 6) -> Sequence[str]:
    os.makedirs(out_dir, exist_ok=True)
    made = []
    if len(result.equity):
        made.append(equity_curve(result.equity, result.trades,
                                 os.path.join(out_dir, "equity_curve.png")))
    if len(result.trades):
        made.append(r_distribution(result.trades, os.path.join(out_dir, "r_distribution.png")))
        made.append(monthly_returns(result.trades, result.config.backtest.initial_equity,
                                    os.path.join(out_dir, "monthly_returns.png")))
        made.append(win_loss_distribution(result.trades,
                                          os.path.join(out_dir, "win_loss.png")))
    return made
