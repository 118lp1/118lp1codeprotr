"""Performance measurement.

Two kinds of numbers live here and they must never be confused:

  * **descriptive** - what happened in this sample (win rate, profit factor, ...)
  * **inferential** - what, if anything, that implies about the future
    (t-statistic on mean R, bootstrap CI, required sample size)

A profit factor of 1.4 on 38 trades is a descriptive fact and statistically
indistinguishable from noise.  ``summarise`` reports both so the reader cannot
accidentally read the first as the second.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .config import Config
from .utils import consecutive_runs, safe_div

BARS_PER_YEAR_M5 = 288 * 252  # ~ trading minutes per year / 5


# --------------------------------------------------------------------------- #
def _empty_summary() -> Dict[str, float]:
    return {"trades": 0, "wins": 0, "losses": 0, "win_rate": float("nan"),
            "expectancy_r": float("nan"), "net_profit": 0.0, "profit_factor": float("nan")}


def summarise(trades: pd.DataFrame, cfg: Config,
              equity: Optional[pd.DataFrame] = None) -> Dict[str, float]:
    if trades is None or len(trades) == 0:
        return _empty_summary()

    r = trades["r_multiple"].to_numpy(dtype=float)
    pnl = trades["pnl"].to_numpy(dtype=float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gross_profit, gross_loss = float(wins.sum()), float(-losses.sum())
    max_w, max_l = consecutive_runs(list(pnl > 0))

    start_eq = cfg.backtest.initial_equity
    eq_series = (equity["equity"].to_numpy(dtype=float)
                 if equity is not None and len(equity)
                 else np.concatenate([[start_eq], start_eq + np.cumsum(pnl)]))
    dd, dd_pct = _drawdown(eq_series)

    days = _span_days(trades)
    years = max(days / 365.25, 1e-9)
    net = float(pnl.sum())
    total_ret = net / start_eq
    ann = (1 + total_ret) ** (1 / years) - 1 if total_ret > -1 else -1.0

    out: Dict[str, float] = {
        "trades": int(len(trades)),
        "wins": int((pnl > 0).sum()),
        "losses": int((pnl < 0).sum()),
        "breakeven": int((pnl == 0).sum()),
        "win_rate": float((pnl > 0).mean()),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "avg_win_r": float(r[r > 0].mean()) if (r > 0).any() else 0.0,
        "avg_loss_r": float(r[r < 0].mean()) if (r < 0).any() else 0.0,
        "expectancy_money": float(pnl.mean()),
        "expectancy_r": float(r.mean()),
        "r_std": float(r.std(ddof=1)) if len(r) > 1 else float("nan"),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_profit": net,
        "profit_factor": safe_div(gross_profit, gross_loss, float("inf")),
        "return_pct": total_ret * 100,
        "annualised_return_pct": ann * 100,
        "max_drawdown": float(dd),
        "max_drawdown_pct": float(dd_pct * 100),
        "avg_drawdown_pct": float(_avg_drawdown(eq_series) * 100),
        "sharpe_trade": _sharpe(r),
        "sharpe_annualised": _sharpe_annualised(trades, r, years),
        "sortino": _sortino(r),
        "largest_win": float(pnl.max()),
        "largest_loss": float(pnl.min()),
        "max_consecutive_wins": int(max_w),
        "max_consecutive_losses": int(max_l),
        "avg_holding_minutes": float(trades["holding_minutes"].mean()),
        "avg_mfe_r": float(trades["mfe_r"].mean()),
        "avg_mae_r": float(trades["mae_r"].mean()),
        "avg_sl_pips": float(trades["sl_pips"].mean()),
        "trades_per_month": float(len(trades) / max(days / 30.44, 1e-9)),
        "span_days": float(days),
        "tp_rate": float((trades["exit_reason"] == "tp").mean()),
        "sl_rate": float((trades["exit_reason"].str.startswith("sl")).mean()),
    }
    out.update(significance(r))
    return out


# --------------------------------------------------------------------------- #
def significance(r: np.ndarray, n_boot: int = 10_000, seed: int = 7) -> Dict[str, float]:
    """Is the mean R distinguishable from zero?

    The t-test assumes i.i.d. trades - defensible here because trades are
    sequential, non-overlapping and sized in R.  The bootstrap CI makes no
    normality assumption and is the number to quote.
    """
    n = len(r)
    if n < 2:
        return {"t_stat": float("nan"), "p_value": float("nan"),
                "r_ci_low": float("nan"), "r_ci_high": float("nan"),
                "n_for_significance": float("nan")}
    mean, sd = float(r.mean()), float(r.std(ddof=1))
    t = mean / (sd / math.sqrt(n)) if sd > 0 else float("nan")
    try:
        from scipy import stats
        p = float(2 * (1 - stats.t.cdf(abs(t), df=n - 1)))
    except Exception:                                   # normal approximation
        p = float(2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))))
    rng = np.random.default_rng(seed)
    boot = rng.choice(r, size=(n_boot, n), replace=True).mean(axis=1)
    need = float((1.96 * sd / mean) ** 2) if mean != 0 else float("inf")
    return {"t_stat": float(t), "p_value": p,
            "r_ci_low": float(np.percentile(boot, 2.5)),
            "r_ci_high": float(np.percentile(boot, 97.5)),
            "n_for_significance": need}


def breakeven_win_rate(rr: float, cost_r: float = 0.0) -> float:
    """Win rate needed to break even at a given RR, after costs expressed in R."""
    return (1 + cost_r) / (1 + rr)


# --------------------------------------------------------------------------- #
def _drawdown(eq: np.ndarray):
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    dd_pct = dd / np.maximum(peak, 1e-9)
    return float(dd.max()), float(dd_pct.max())


def _avg_drawdown(eq: np.ndarray) -> float:
    peak = np.maximum.accumulate(eq)
    dd_pct = (peak - eq) / np.maximum(peak, 1e-9)
    active = dd_pct[dd_pct > 0]
    return float(active.mean()) if len(active) else 0.0


def _sharpe(r: np.ndarray) -> float:
    if len(r) < 2 or r.std(ddof=1) == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1))


def _sharpe_annualised(trades: pd.DataFrame, r: np.ndarray, years: float) -> float:
    if len(r) < 2 or r.std(ddof=1) == 0:
        return float("nan")
    per_year = len(r) / max(years, 1e-9)
    return float(r.mean() / r.std(ddof=1) * math.sqrt(per_year))


def _sortino(r: np.ndarray) -> float:
    downside = r[r < 0]
    if len(downside) < 2:
        return float("nan")
    dd = math.sqrt(float((downside ** 2).mean()))
    return float(r.mean() / dd) if dd > 0 else float("nan")


def _span_days(trades: pd.DataFrame) -> float:
    t0 = pd.Timestamp(trades["entry_time"].min())
    t1 = pd.Timestamp(trades["exit_time"].max())
    return max((t1 - t0).total_seconds() / 86400.0, 1.0)


# --------------------------------------------------------------------------- #
# Breakdowns
# --------------------------------------------------------------------------- #
def _agg(g: pd.DataFrame) -> pd.Series:
    pnl = g["pnl"]
    return pd.Series({
        "trades": len(g),
        "win_rate": float((pnl > 0).mean()),
        "expectancy_r": float(g["r_multiple"].mean()),
        "net_profit": float(pnl.sum()),
        "profit_factor": safe_div(float(pnl[pnl > 0].sum()),
                                  float(-pnl[pnl < 0].sum()), float("inf")),
    })


def breakdowns(trades: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    if trades is None or len(trades) == 0:
        return {}
    t = trades.copy()
    t["entry_time"] = pd.to_datetime(t["entry_time"], utc=True).dt.tz_localize(None)
    t["month"] = t["entry_time"].dt.to_period("M").astype(str)
    t["year"] = t["entry_time"].dt.year
    t["hour"] = t["entry_time"].dt.hour
    t["weekday_name"] = t["entry_time"].dt.day_name()
    t["sl_bucket"] = pd.cut(t["sl_pips"], bins=[0, 5, 10, 15, 20, 1e9],
                            labels=["0-5", "5-10", "10-15", "15-20", ">20"])
    out = {}
    for key, col in [("monthly", "month"), ("yearly", "year"), ("by_hour", "hour"),
                     ("by_weekday", "weekday_name"), ("by_direction", "direction"),
                     ("by_poi_type", "poi_type"), ("by_sl_bucket", "sl_bucket"),
                     ("by_exit_reason", "exit_reason")]:
        try:
            out[key] = t.groupby(col, observed=True).apply(_agg, include_groups=False)
        except TypeError:                                # older pandas
            out[key] = t.groupby(col, observed=True).apply(_agg)
    out["r_distribution"] = _r_hist(t["r_multiple"])
    return out


def _r_hist(r: pd.Series) -> pd.DataFrame:
    bins = [-np.inf, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, np.inf]
    labels = ["<-1.5", "-1.5..-1", "-1..-0.5", "-0.5..0", "0..0.5",
              "0.5..1", "1..1.5", "1.5..2", ">2"]
    cats = pd.cut(r, bins=bins, labels=labels)
    return cats.value_counts().reindex(labels).fillna(0).to_frame("count")


def format_summary(s: Dict[str, float]) -> str:
    if not s or s.get("trades", 0) == 0:
        return "No trades generated."
    rows = [
        ("Trades", f"{s['trades']:.0f}"),
        ("Win rate", f"{s['win_rate']*100:.1f}%"),
        ("Expectancy (R)", f"{s['expectancy_r']:+.3f}"),
        ("95% CI on mean R", f"[{s['r_ci_low']:+.3f}, {s['r_ci_high']:+.3f}]"),
        ("p-value (mean R = 0)", f"{s['p_value']:.3f}"),
        ("Profit factor", f"{s['profit_factor']:.2f}"),
        ("Net profit", f"{s['net_profit']:,.2f}"),
        ("Return", f"{s['return_pct']:.2f}%"),
        ("Annualised", f"{s['annualised_return_pct']:.2f}%"),
        ("Max drawdown", f"{s['max_drawdown_pct']:.2f}%"),
        ("Sharpe (annualised)", f"{s['sharpe_annualised']:.2f}"),
        ("Sortino (per trade)", f"{s['sortino']:.2f}"),
        ("Avg win / loss", f"{s['avg_win']:,.2f} / {s['avg_loss']:,.2f}"),
        ("Max consec. W/L", f"{s['max_consecutive_wins']:.0f} / {s['max_consecutive_losses']:.0f}"),
        ("Avg SL (pips)", f"{s['avg_sl_pips']:.1f}"),
        ("Avg holding (min)", f"{s['avg_holding_minutes']:.0f}"),
        ("Trades / month", f"{s['trades_per_month']:.1f}"),
        ("Trades needed for signif.", f"{s['n_for_significance']:.0f}"),
    ]
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{k:<{width}} : {v}" for k, v in rows)
