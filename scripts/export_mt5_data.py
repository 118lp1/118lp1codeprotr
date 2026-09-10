#!/usr/bin/env python3
"""Export M5 and M1 history from a running MT5 terminal to CSV.

    python scripts/export_mt5_data.py --symbol EURUSD --start 2019-01-01 --end 2025-01-01

Windows only (the MetaTrader5 package has no Linux/macOS build).

Note on limits: copy_rates_range is capped by the terminal's "Max bars in
chart" setting.  Set it to Unlimited and scroll the M1 chart back before
exporting, otherwise the export is silently truncated and every statistic you
compute afterwards is based on a shorter sample than you think.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_bot.data import data_report, load_mt5


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2025-01-01")
    ap.add_argument("--outdir", default="data")
    ap.add_argument("--offset-hours", type=float, default=0.0,
                    help="broker server time offset from UTC")
    ap.add_argument("--timeframes", nargs="+", default=["M5", "M1"])
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    for tf in args.timeframes:
        df = load_mt5(args.symbol, tf, args.start, args.end, args.offset_hours)
        path = os.path.join(args.outdir, f"{args.symbol}_{tf}.csv")
        df.to_csv(path, index=False)
        minutes = {"M1": 1, "M5": 5, "M15": 15}[tf]
        print(f"{path}: {len(df):,} bars")
        print("   ", data_report(df, minutes))


if __name__ == "__main__":
    main()
