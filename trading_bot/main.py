"""Command line entry point.

    python -m trading_bot.main backtest      --config configs/default.yaml
    python -m trading_bot.main compare-exec  --config configs/default.yaml
    python -m trading_bot.main stress        --config configs/default.yaml
    python -m trading_bot.main optimize      --config configs/default.yaml --trials 40
    python -m trading_bot.main walkforward   --config configs/default.yaml
    python -m trading_bot.main montecarlo    --config configs/default.yaml
    python -m trading_bot.main paper         --config configs/default.yaml
    python -m trading_bot.main live          --config configs/live.yaml --i-understand-the-risk

LIVE requires BOTH ``mode: LIVE`` in the config file AND the CLI flag.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Tuple

import pandas as pd

from .backtest import Backtester, compare_execution_models
from .config import Config, Mode
from .data import AlignedData, build_aligned, data_report, load_csv
from .logger import get_logger
from .metrics import breakdowns, format_summary, summarise
from .montecarlo import monte_carlo_table, run_monte_carlo
from .optimization import cost_stress, random_search, sensitivity
from .walkforward import holdout_split, run_walkforward


def load_data(cfg: Config, log) -> AlignedData:
    d = cfg.data
    if not os.path.exists(d.m5_path):
        raise FileNotFoundError(
            f"{d.m5_path} not found. Export data from MT5 (see README) or run "
            f"`python -m trading_bot.main make-synthetic` for a plumbing test.")
    m5 = load_csv(d.m5_path, d.data_tz, d.broker_utc_offset_hours, d.start, d.end)
    m1 = None
    if d.m1_path and os.path.exists(d.m1_path):
        m1 = load_csv(d.m1_path, d.data_tz, d.broker_utc_offset_hours, d.start, d.end)
    else:
        log.warning("No M1 file: falling back to the M5 execution model, which "
                    "CANNOT resolve SL-vs-TP inside a bar. Results will be biased.")
        cfg.backtest.execution_model = "m5"
    log.info("M5 %s", data_report(m5, 5, d.max_gap_minutes))
    if m1 is not None:
        log.info("M1 %s", data_report(m1, 1, d.max_gap_minutes))
    return build_aligned(m5, m1)


def _write(df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_csv(path, index=False)


def cmd_backtest(cfg: Config, args) -> None:
    log = get_logger("main", cfg.log_level)
    aligned = load_data(cfg, log)
    res = Backtester(cfg).run(aligned)
    out = cfg.output_dir
    os.makedirs(out, exist_ok=True)
    print("\n=== FUNNEL ===")
    for k, v in res.diagnostics.items():
        print(f"{k:<16}: {v:,.0f}")
    print("\n=== REJECTIONS ===")
    for k, v in list(res.rejections.items())[:15]:
        print(f"{k:<28}: {v}")
    s = summarise(res.trades, cfg, res.equity)
    print("\n=== PERFORMANCE ===")
    print(format_summary(s))
    if len(res.trades):
        _write(res.trades, os.path.join(out, "trades.csv"))
        _write(res.equity, os.path.join(out, "equity.csv"))
        res.events.to_csv(os.path.join(out, "events.csv"))
        for name, df in breakdowns(res.trades).items():
            _write(df.reset_index(), os.path.join(out, f"breakdown_{name}.csv"))
        if not args.no_charts:
            from .viz import report_charts
            report_charts(res, out)
        print(f"\nartifacts -> {out}/")


def cmd_compare_exec(cfg: Config, args) -> None:
    log = get_logger("main", cfg.log_level)
    aligned = load_data(cfg, log)
    df = compare_execution_models(cfg, aligned)
    print(df.to_string(index=False))
    _write(df, os.path.join(cfg.output_dir, "execution_model_comparison.csv"))


def cmd_stress(cfg: Config, args) -> None:
    log = get_logger("main", cfg.log_level)
    aligned = load_data(cfg, log)
    df = cost_stress(cfg, aligned)
    print(df.to_string(index=False))
    _write(df, os.path.join(cfg.output_dir, "cost_stress.csv"))


def cmd_optimize(cfg: Config, args) -> None:
    log = get_logger("main", cfg.log_level)
    aligned = load_data(cfg, log)
    ins, oos = holdout_split(aligned, args.oos_fraction)
    log.info("in-sample bars=%d  out-of-sample bars=%d", len(ins.m5), len(oos.m5))
    res = random_search(cfg, ins, n_trials=args.trials, seed=cfg.backtest.seed)
    print("\nTop in-sample trials:")
    print(res.trials.head(10).to_string(index=False))
    best_cfg = cfg.with_overrides(res.best_params)
    oos_res = Backtester(best_cfg, log_events=False).run(oos)
    print("\n=== OUT-OF-SAMPLE with in-sample-best parameters ===")
    print(format_summary(summarise(oos_res.trades, best_cfg, oos_res.equity)))
    print("\nIf out-of-sample expectancy is materially worse than in-sample, "
          "treat the parameters as overfit, not as a discovery.")
    _write(res.trials, os.path.join(cfg.output_dir, "optimisation_trials.csv"))


def cmd_sensitivity(cfg: Config, args) -> None:
    log = get_logger("main", cfg.log_level)
    aligned = load_data(cfg, log)
    df = sensitivity(cfg, aligned)
    print(df.to_string(index=False))
    _write(df, os.path.join(cfg.output_dir, "sensitivity.csv"))


def cmd_walkforward(cfg: Config, args) -> None:
    log = get_logger("main", cfg.log_level)
    aligned = load_data(cfg, log)
    out = run_walkforward(cfg, aligned, train_months=args.train, valid_months=args.valid,
                          test_months=args.test, n_trials=args.trials)
    folds = out["folds"]
    if len(folds) == 0:
        print(out.get("note", "no folds"))
        return
    print(folds.to_string(index=False))
    print("\n=== AGGREGATE OUT-OF-SAMPLE ===")
    print(format_summary(out.get("aggregate", {})))
    _write(folds, os.path.join(cfg.output_dir, "walkforward_folds.csv"))
    if len(out["oos_trades"]):
        _write(out["oos_trades"], os.path.join(cfg.output_dir, "walkforward_oos_trades.csv"))


def cmd_montecarlo(cfg: Config, args) -> None:
    path = os.path.join(cfg.output_dir, "trades.csv")
    if not os.path.exists(path):
        raise FileNotFoundError("run `backtest` first to produce trades.csv")
    trades = pd.read_csv(path)
    table = monte_carlo_table(trades, cfg.risk.risk_per_trade, seed=cfg.backtest.seed)
    print(table.to_string(index=False))
    _write(table, os.path.join(cfg.output_dir, "monte_carlo.csv"))
    if not args.no_charts:
        from .viz import monte_carlo_paths
        mc = run_monte_carlo(trades, cfg.risk.risk_per_trade, seed=cfg.backtest.seed)
        monte_carlo_paths(mc.paths, path=os.path.join(cfg.output_dir, "monte_carlo.png"))


def cmd_make_synthetic(cfg: Config, args) -> None:
    from .synthetic import generate_m1, m5_from
    m1 = generate_m1(n_days=args.days, seed=cfg.backtest.seed)
    m5 = m5_from(m1)
    os.makedirs(os.path.dirname(cfg.data.m5_path) or ".", exist_ok=True)
    m5.to_csv(cfg.data.m5_path, index=False)
    if cfg.data.m1_path:
        m1.to_csv(cfg.data.m1_path, index=False)
    print(f"wrote {len(m5)} M5 and {len(m1)} M1 synthetic bars.")
    print("REMINDER: synthetic random-walk data tests the CODE, never the EDGE.")


def cmd_paper(cfg: Config, args) -> None:
    from .execution import LiveTrader, MT5Broker, PaperBroker
    feed = MT5Broker(cfg)
    broker = PaperBroker(cfg, feed, cfg.backtest.initial_equity)
    if not broker.connect():
        sys.exit("could not connect to MT5")
    cfg.symbol_spec = broker.symbol_spec(cfg.data.symbol)
    LiveTrader(cfg, broker, confirm=True).run(poll_seconds=args.poll)


def cmd_live(cfg: Config, args) -> None:
    if cfg.mode is not Mode.LIVE:
        sys.exit("config mode is not LIVE - refusing to trade")
    if not args.i_understand_the_risk:
        sys.exit("missing --i-understand-the-risk flag - refusing to trade")
    from .execution import LiveTrader, MT5Broker
    broker = MT5Broker(cfg, login=args.login, password=args.password,
                       server=args.server, terminal_path=args.terminal)
    if not broker.connect():
        sys.exit("could not connect to MT5")
    cfg.symbol_spec = broker.symbol_spec(cfg.data.symbol)
    LiveTrader(cfg, broker, confirm=True).run(poll_seconds=args.poll)


def build_parser() -> argparse.ArgumentParser:
    # global flags are also attached to every sub-command, so that both
    # `main.py --config x backtest` and `main.py backtest --config x` work
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="configs/default.yaml")
    common.add_argument("--no-charts", action="store_true")

    p = argparse.ArgumentParser(prog="trading_bot", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backtest", parents=[common])
    sub.add_parser("compare-exec", parents=[common])
    sub.add_parser("stress", parents=[common])
    sub.add_parser("sensitivity", parents=[common])
    o = sub.add_parser("optimize", parents=[common])
    o.add_argument("--trials", type=int, default=40)
    o.add_argument("--oos-fraction", type=float, default=0.3)
    w = sub.add_parser("walkforward", parents=[common])
    w.add_argument("--train", type=int, default=6)
    w.add_argument("--valid", type=int, default=2)
    w.add_argument("--test", type=int, default=2)
    w.add_argument("--trials", type=int, default=30)
    sub.add_parser("montecarlo", parents=[common])
    s = sub.add_parser("make-synthetic", parents=[common])
    s.add_argument("--days", type=int, default=180)
    pa = sub.add_parser("paper", parents=[common])
    pa.add_argument("--poll", type=float, default=15.0)
    lv = sub.add_parser("live", parents=[common])
    lv.add_argument("--i-understand-the-risk", action="store_true")
    lv.add_argument("--poll", type=float, default=15.0)
    lv.add_argument("--login", type=int)
    lv.add_argument("--password")
    lv.add_argument("--server")
    lv.add_argument("--terminal")
    return p


def main(argv: Optional[list] = None) -> None:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config) if os.path.exists(args.config) else Config()
    handlers = {
        "backtest": cmd_backtest, "compare-exec": cmd_compare_exec,
        "stress": cmd_stress, "sensitivity": cmd_sensitivity,
        "optimize": cmd_optimize, "walkforward": cmd_walkforward,
        "montecarlo": cmd_montecarlo, "make-synthetic": cmd_make_synthetic,
        "paper": cmd_paper, "live": cmd_live,
    }
    handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
