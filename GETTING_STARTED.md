# Step-by-Step Guide

How to take this repository from a fresh machine to (possibly) a live account.

Follow the steps in order. Steps 1–5 are setup, 6–9 tell you whether the
strategy is worth trading, 10–11 are deployment. **Step 9 is a real decision
point, not a formality** — most strategy ideas do not survive it, and the
system is built to tell you that clearly rather than to flatter you.

---

## Step 1 — Install

You need **Python 3.11 or newer**. Check with `python --version`.

```bash
cd trading_bot
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

`MetaTrader5` only installs on Windows. On macOS/Linux everything except data
export, paper and live trading still works — you can backtest from CSV files
copied off a Windows machine.

**Checkpoint:**

```bash
python tests/run_tests.py
```

Must print `86 passed, 0 failed`. If it does not, stop and fix that first;
nothing downstream is trustworthy.

---

## Step 2 — Prove the pipeline works before touching real data

```bash
python -m trading_bot.main make-synthetic --days 560
python -m trading_bot.main backtest
```

This generates a random walk and trades it. You should see a funnel, a
rejection table, and a **negative** expectancy of roughly −0.2R to −0.35R.

**That negative number is the correct result.** A random walk has no edge, so
after costs the system must lose. If you ever see a *positive* expectancy on
synthetic data, something is broken — most likely look-ahead.

Delete `data/EURUSD_M5.csv` and `data/EURUSD_M1.csv` before Step 3 so you don't
accidentally backtest the random walk later and believe it.

---

## Step 3 — Get real MT5 data

On the Windows machine with your MT5 terminal:

**3a. Load the history properly.** MT5 downloads bars lazily and will silently
give you a truncated series if you skip this.

1. Tools → Options → Charts → *Max bars in chart* → `Unlimited`. Restart MT5.
2. Open your symbol on the **M1** chart. Press `Home` repeatedly until the
   chart stops extending to the left. This can take a minute or two.
3. Repeat on the **M5** chart.

**3b. Export.**

```bash
python scripts/export_mt5_data.py --symbol EURUSD --start 2019-01-01 --end 2025-01-01
```

Writes `data/EURUSD_M5.csv` and `data/EURUSD_M1.csv` and prints a data report
for each.

**Checkpoint — read the report:**

| Field | What you want |
|-------|---------------|
| `rows` | M5: ~70,000 per year of history. Much less means the export truncated. |
| `duplicate_timestamps` | must be `0` |
| `closure_count` | roughly 52 per year (weekends) |
| `missing_bars_est` | small. Large means thin history, which flatters results |

If M1 history is unavailable for the full period, export a shorter period for
both timeframes rather than mismatching them.

---

## Step 4 — Fix the timezone

MT5 timestamps are in **broker server time**, not UTC, and many brokers shift
with US daylight saving. Getting this wrong corrupts every session statistic
and quietly changes which trades the session filter allows.

**Find your offset:** open the M5 chart on a known NFP release date (first
Friday of a month) and find the volatility spike. NFP is at **13:30 UTC**. If
your chart shows the spike at 15:30, your broker is UTC+2.

Put it in `configs/default.yaml`:

```yaml
data:
  symbol: EURUSD
  m5_path: data/EURUSD_M5.csv
  m1_path: data/EURUSD_M1.csv
  data_tz: UTC
  broker_utc_offset_hours: 2.0     # <-- your measured offset
```

**Checkpoint:** after the next backtest run, open
`results/breakdown_by_hour.csv`. Trade activity should cluster in 07:00–16:00
and be near zero at 22:00–02:00. If the pattern looks shifted, your offset is
wrong.

---

## Step 5 — Set the symbol specification

This is the step where a silent mistake costs real money. On Windows with MT5
running:

```python
python -c "import MetaTrader5 as mt5; mt5.initialize(); s=mt5.symbol_info('EURUSD'); print(s.digits, s.point, s.trade_tick_size, s.trade_tick_value, s.trade_contract_size, s.volume_min, s.volume_step)"
```

Copy the values into `configs/default.yaml`:

```yaml
symbol_spec:
  name: EURUSD
  digits: 5
  point: 0.00001
  tick_size: 0.00001
  tick_value: 1.0          # <-- P/L per tick per 1.0 lot, IN YOUR ACCOUNT CURRENCY
  contract_size: 100000.0
  volume_min: 0.01
  volume_max: 100.0
  volume_step: 0.01
  pip_size: null           # null = auto (0.0001 for 5-digit FX). Set explicitly for gold/indices
```

`tick_value` is the dangerous one: it depends on your account currency and,
for cross pairs, changes with the exchange rate. If it is wrong, every position
size in every report is wrong by the same factor and nothing will look obviously
broken.

Also set your actual costs — ask your broker or measure the average spread
during your trading session:

```yaml
costs:
  spread_pips: 0.8
  slippage_entry_pips: 0.2
  slippage_stop_pips: 0.3
  commission_per_lot_round_turn: 7.0
```

Being pessimistic here is free. Being optimistic is how backtests lie.

---

## Step 6 — Run the baseline backtest

```bash
python -m trading_bot.main backtest
```

**Read the funnel before you read the profit.**

```
pois_created    : 4,532     zones the rules found
pois_superseded : 1,222     collapsed into a newer overlapping zone
departures      : 2,379     zones price actually left
retests         : 1,947     zones price came back to
engulf_signals  :   516     confirmed triggers
orders_rejected :   392     vetoed by risk / stop size / session
trades          :   213
ambiguous_bars  :     0     outcomes that are ASSUMPTIONS, not observations
```

What to check:

- **`trades` under ~100** — you cannot conclude anything. Loosen the filters or
  extend the history, but do not start interpreting the P/L.
- **`ambiguous_bars` above ~5 % of trades** — a meaningful share of your results
  depends on the SL-before-TP assumption. Run Step 7 and take the pessimistic
  number as your estimate.
- **The rejection table.** If `sl_exceeds_max` dominates, the 20-pip cap is
  rejecting most of the idea. That is a genuine finding about the strategy: the
  zones this market produces are wider than the risk budget allows. Consider
  whether the concept is viable on this symbol at all, rather than shrinking the
  zones until they fit.
- **`out_of_session`** — expected if you have a session filter; only alarming if
  it is removing nearly everything.

Artefacts land in `results/`: `trades.csv`, `equity.csv`, `events.csv`,
per-breakdown CSVs and PNG charts.

---

## Step 7 — Check the execution assumptions and costs

```bash
python -m trading_bot.main compare-exec
python -m trading_bot.main stress
```

`compare-exec` shows M1 execution against the three M5 assumptions. If M1 and
M5 SL-first agree, your results do not depend on intrabar guesswork. If
M5 TP-first is much better than M1, then any backtest without M1 data (including
most retail backtesters) is overstating this strategy.

`stress` re-runs at 1×/1.5×/2× spread and 1×/2× slippage. **An edge that
disappears at 1.5× spread is not an edge** — it is a bet that your broker's
spread never widens, which it does, precisely when you are in a trade.

---

## Step 8 — Visually audit 20–30 signals

Automated tests confirm the code does what the specification says. They cannot
confirm that the specification captures the trade you actually have in mind.
Only your eyes can do that.

```python
import pandas as pd
from trading_bot.viz import plot_trade
from trading_bot.data import load_csv, build_aligned

trades = pd.read_csv("results/trades.csv")
aligned = build_aligned(load_csv("data/EURUSD_M5.csv"))

for _, tr in trades.sample(25, random_state=1).iterrows():
    plot_trade(tr, aligned.m5, aligned.m15,
               path=f"results/audit/trade_{int(tr.trade_id)}.png")
```

For each chart ask: is the shaded zone a POI I would have drawn? Did price
genuinely leave and return? Is that an engulfing candle I would have traded?

If a third of them look wrong, adjust the **definitions** in
`SPECIFICATION.md` §3 and the corresponding parameters — then go back to Step 6
and re-run. Do not skip forward with rules you do not believe in.

---

## Step 9 — The decision point: walk-forward

Everything so far is in-sample. This is the only test that answers "would this
have made money on data it never saw?"

```bash
python -m trading_bot.main walkforward --train 6 --valid 2 --test 2 --trials 30
```

Each fold optimises on 6 months, picks among the top candidates on 2 validation
months, then runs **once** on 2 untouched test months.

**Read the aggregate, not the folds.**

```
=== AGGREGATE OUT-OF-SAMPLE ===
Trades            : 75
Win rate          : 33.3%
Expectancy (R)    : -0.080
95% CI on mean R  : [-0.404, +0.244]
p-value           : 0.631
```

### The go / no-go rule

| Aggregate OOS result | Verdict |
|----------------------|---------|
| Mean R > 0 and 95 % CI **excludes** zero | Proceed to Step 10 |
| CI includes zero | **No evidence of an edge.** Stop. |
| Mean R < 0 | The strategy loses. Stop. |

In the demonstration run above, three of five folds were profitable — one at
+0.43R — on data that provably had no edge whatsoever. **Individual profitable
folds mean nothing.** If you find yourself explaining why fold 3 "doesn't
count", you have already left research and entered marketing.

If the answer is "no edge", legitimate next moves are: test a different symbol
chosen *before* seeing results, extend the history, or revisit the POI
definition on theoretical grounds. Illegitimate: re-running with different
parameters until a fold set looks good.

---

## Step 10 — Understand the risk you would be taking

Only if Step 9 passed.

```bash
python -m trading_bot.main montecarlo
```

Look at `p95_max_dd_pct` — the drawdown you should plan for, not the one you
saw. If that number is larger than what you can hold through without
intervening, reduce `risk.risk_per_trade` until it isn't. Halving risk roughly
halves drawdown.

Remember the stated limitation: Monte Carlo assumes independent trades. Real
losses cluster, so the true tail is worse than the simulation shows.

---

## Step 11 — Paper trade, then go live

### 11a. Paper (minimum 3 months)

```bash
python -m trading_bot.main paper --config configs/default.yaml
```

Requires a running MT5 terminal for the data feed. Orders are simulated
in-process but travel the identical code path as live — same engine, same risk
gates, same sizing.

At the end, compare paper fills against a backtest of the same period. If they
diverge, your cost model is wrong; fix it and re-do Step 9 with the corrected
costs.

### 11b. Demo account with the live config

Run the real `live` command against a **demo** account first. Verify:

- fills happen at the prices the backtester assumed
- lot sizes match `results/trades.csv` expectations
- the symbol name matches exactly (many brokers use `EURUSD.pro`, `EURUSDm`)
- restarting the script mid-setup does **not** produce a duplicate entry

### 11c. Live

Two independent switches are required. Neither alone is enough:

1. `mode: LIVE` in `configs/live.yaml`
2. `--i-understand-the-risk` on the command line

```bash
python -m trading_bot.main live --config configs/live.yaml \
    --i-understand-the-risk --login 12345678 --server MyBroker-Live
```

In the terminal: enable *Algo Trading*, and allow it for the symbol.

Start at **half** your intended risk (`configs/live.yaml` ships with 0.25 %).
Raise it only after a month of live fills matching expectations.

---

## Daily operation

| Task | How |
|------|-----|
| Watch the bot | `logs/live.log` — connection, signals, vetoes, orders |
| Check open trades | `state/trades.db`, table `trades`, `status='open'` |
| Review rejections | `events.csv` / the `reject` rows — tells you what the rules blocked |
| Stop the bot | `Ctrl-C`. Open positions keep their broker-side SL and TP. |

The bot rebuilds its entire state from market history on every 5-minute cycle,
so it is safe to restart at any time. Each intended trade is keyed by
`symbol|poi_id|engulf_time` in SQLite before the order is sent, which is what
prevents a restart from double-entering.

---

## Common problems

| Symptom | Cause |
|---------|-------|
| `no rates returned` on export | History not loaded — redo Step 3a |
| Backtest produces 0 trades | Zones wider than 20 pips (check `sl_exceeds_max`), or the session filter is in the wrong timezone |
| Warning about the M5 execution model | No M1 file found; SL-vs-TP inside a bar is then a guess |
| Live: `order_send rejected` | Wrong filling mode, market closed, or `volume` below the broker minimum |
| Live: no signals for days | Normal. This is a ~10 trades/month strategy on one symbol. |
| Timestamps look wrong | `broker_utc_offset_hours` — redo Step 4 |

---

## The one thing to remember

The system is built so that a negative answer is easy to see. That is its main
feature. A strategy that loses money honestly in a backtest has told you
something true and cost you nothing; a strategy that wins through look-ahead or
parameter mining tells you nothing and costs you the account.
