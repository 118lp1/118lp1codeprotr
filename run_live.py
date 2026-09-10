"""Double-click / single-command launcher for MT5 demo (or live) trading.

    .venv\\Scripts\\python.exe run_live.py

What this does
---------------
* Reads configs/live.yaml.
* Attaches to whatever MT5 terminal is already running and logged in --
  no login/password is ever typed into this script. Log into the account
  you want to trade (demo recommended) in the terminal yourself first.
* Prints the connected account so you can verify it's the one you meant,
  and requires a typed confirmation before sending a single order.
* Then runs the identical strategy/risk engine used by the backtester,
  polling every 15 seconds, until Ctrl-C.

See GETTING_STARTED.md step 11 and README.md section 5 before running this
against a real-money account. This strategy has no validated edge on real
market data (VALIDATION.md) -- start on a demo account.
"""

from __future__ import annotations

import sys

from trading_bot.config import Config, Mode
from trading_bot.execution import LiveTrader, MT5Broker

CONFIG_PATH = "configs/live.yaml"
POLL_SECONDS = 15.0


def main() -> None:
    cfg = Config.load(CONFIG_PATH)
    if cfg.mode is not Mode.LIVE:
        sys.exit(f"{CONFIG_PATH}: mode is not LIVE - refusing to trade")

    broker = MT5Broker(cfg)  # no credentials -> attaches to the logged-in terminal
    if not broker.connect():
        sys.exit(
            "Could not connect to MT5.\n"
            "Is the terminal open, logged in, and is 'Algo Trading' enabled?"
        )

    import MetaTrader5 as mt5  # local import: only needed for the account check below

    acc = mt5.account_info()
    mode_name = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(acc.trade_mode, "UNKNOWN")
    term = mt5.terminal_info()

    print("=" * 70)
    print("MT5 POI-RETEST BOT -- live/demo trading")
    print("=" * 70)
    print(f"Account       : {acc.login} ({mode_name}) on {acc.server}")
    print(f"Balance       : {acc.balance:.2f} {acc.currency}")
    print(f"Symbol        : {cfg.data.symbol}")
    print(f"Risk / trade  : {cfg.risk.risk_per_trade:.2%}")
    print(f"Max open pos. : {cfg.risk.max_open_positions}")
    print(f"Algo trading  : {'enabled' if term.trade_allowed else 'DISABLED - enable it in the terminal'}")
    print("=" * 70)

    if not term.trade_allowed:
        broker.close()
        sys.exit("Algo Trading is disabled in the terminal. Enable it and re-run.")

    if mode_name == "REAL":
        print("\n*** THIS IS A REAL-MONEY ACCOUNT. ***")
        print("The strategy has NOT been validated on real market data (see VALIDATION.md).")
        confirm_word = "I ACCEPT THE RISK"
    else:
        confirm_word = "YES"

    reply = input(f"\nThis will place real orders on the account above. Type '{confirm_word}' to continue: ")
    if reply.strip() != confirm_word:
        broker.close()
        sys.exit("Not confirmed - aborted, no orders sent.")

    cfg.symbol_spec = broker.symbol_spec(cfg.data.symbol)
    print(f"\nSymbol spec pulled from terminal: digits={cfg.symbol_spec.digits} "
          f"tick_value={cfg.symbol_spec.tick_value} contract_size={cfg.symbol_spec.contract_size}")
    print(f"\nStarting poll loop every {POLL_SECONDS:.0f}s. Ctrl-C to stop (open positions keep their SL/TP).")
    print("Watch logs/live.log for signals, vetoes and orders.\n")

    try:
        LiveTrader(cfg, broker, confirm=True).run(poll_seconds=POLL_SECONDS)
    finally:
        broker.close()


if __name__ == "__main__":
    main()
