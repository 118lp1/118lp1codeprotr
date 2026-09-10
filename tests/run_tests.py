#!/usr/bin/env python3
"""Run the test suite.

Written so it works either way:

    pytest -q                     # if pytest is installed
    python tests/run_tests.py     # if it is not
"""

from __future__ import annotations

import importlib
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MODULES = ["tests.test_indicators", "tests.test_data", "tests.test_risk",
           "tests.test_strategy", "tests.test_backtest", "tests.test_montecarlo"]


def main() -> int:
    passed, failed = 0, []
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception:
            failed.append((name, traceback.format_exc()))
            continue
        for attr in sorted(dir(mod)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(mod, attr)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
            except Exception:
                failed.append((f"{name}.{attr}", traceback.format_exc()))
    print(f"\n{passed} passed, {len(failed)} failed")
    for name, tb in failed:
        print("\n" + "=" * 70)
        print("FAILED:", name)
        print(tb)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
