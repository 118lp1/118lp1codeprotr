# M15 POI Retest / M5 Engulfing — MT5 Research System

An event-driven research and execution framework for one specific idea:
*M15 point of interest → departure → retest → M5 engulfing confirmation →
stop beyond the POI (max 20 pips) → 1:2 target.*

Read **[SPECIFICATION.md](SPECIFICATION.md)** first. It contains the
mathematical definitions, the look-ahead analysis, and the reasons behind the
design choices. This file is only about running things.

> **Status: no validated edge.** The code has never seen real market data. All
> figures produced so far come from a synthetic random walk and test the
> plumbing, not the strategy. Do not attach money to this until stage 7 of the
> research protocol has passed on your own data.

---

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Python 3.11+. `MetaTrader5` is Windows-only and needed only for data export,
paper and live trading — backtesting works anywhere from CSV. `optuna`,
`plotly` and `pytest` are optional; the code falls back gracefully.

Verify:

```bash
python tests/run_tests.py          # or: pytest -q
```

Expect `86 passed, 0 failed`.

---

## 2. Getting MT5 historical data

You need **M5** (signals) and **M1** (execution resolution). Without M1 the
backtester cannot tell whether the stop or the target was hit first inside a
bar, and it will say so loudly.

### 2.1 Load the history first (this is the step people skip)

MT5 downloads bars lazily. If you export before loading, you get a short,
silently truncated series.

1. Tools → Options → Charts → *Max bars in chart*: `Unlimited`.
2. Open the symbol on M1, press `Home` repeatedly until the chart stops
   extending left. Repeat on M5.
3. Tools → History Centre (F2) if your broker exposes it, and download the
   symbol.

### 2.2 Export

Either the terminal UI — right-click the chart → *Save As* → CSV — or, better,
the script that produces exactly the format this project expects:

```bash
python scripts/export_mt5_data.py --symbol EURUSD --start 2019-01-01 --end 2025-01-01
# -> data/EURUSD_M5.csv, data/EURUSD_M1.csv
```

### 2.3 Timezone

MT5 timestamps are in **broker server time**, which is usually not UTC and
often shifts with US daylight saving. Getting this wrong silently corrupts
every session-based statistic.

Find your broker's offset (compare an NFP release candle against 13:30 UTC),
then set it in the config:

```yaml
data:
  data_tz: UTC                     # timestamps are read as this tz
  broker_utc_offset_hours: 2.0     # then shifted by this many hours
```

Sanity check after loading: EURUSD volume should peak around 07:00–16:00 UTC
and collapse around 22:00–06:00 UTC.

### 2.4 Data quality expectations

Every run prints a data report. Watch for:

* `duplicate_timestamps` > 0 — a broken export
* `missing_bars_est` large — thin history; the strategy will look better than
  it is, because gaps hide adverse excursions
* `closure_count` — weekends; expected, roughly one per week

Bad bars (high < low, non-positive prices) are **dropped, never repaired**.
Missing bars are **never** forward-filled: a fabricated flat candle can invent
an engulfing pattern out of nothing.

---

## 3. Running the backtest

```bash
# no data yet? generate a random walk to exercise the pipeline
python -m trading_bot.main make-synthetic --days 180

python -m trading_bot.main backtest --config configs/default.yaml
```

Output goes to `results/`: `trades.csv`, `equity.csv`, `events.csv`,
`breakdown_*.csv`, and PNG charts.

Read the funnel before the P/L:

```
pois_created    : 1,590      how many zones the rules found
pois_superseded :   940      collapsed into a newer overlapping zone
departures      :   323      zones price actually left
retests         :   169      zones price came back to
engulf_signals  :    92      confirmed triggers
orders_rejected :    53      vetoed by risk / stop size / session
trades          :    49
ambiguous_bars  :     0      outcomes that are ASSUMPTIONS, not observations
```

A funnel that collapses from thousands to a handful means the parameters are
starving the sample, and any resulting statistics are noise.

### Other commands

```bash
python -m trading_bot.main compare-exec    # M1 vs M5 execution assumptions (§13)
python -m trading_bot.main stress          # spread/slippage sensitivity  (§14)
python -m trading_bot.main sensitivity     # one-parameter-at-a-time sweep
python -m trading_bot.main optimize --trials 40   # in-sample search + honest OOS check
python -m trading_bot.main walkforward --train 6 --valid 2 --test 2
python -m trading_bot.main montecarlo      # needs results/trades.csv
```

Recommended order: `backtest` → `compare-exec` → `stress` → `walkforward` →
`montecarlo`. Run `optimize` **only** after you have looked at the unoptimised
result, so you know what the baseline was.

### Reading the statistics honestly

`format_summary` prints an expectancy *and* a 95 % bootstrap confidence
interval, a p-value, and `n_for_significance` — the number of trades needed to
resolve an edge of the observed size. If the CI spans zero, you have measured
nothing, whatever the profit factor says.

---

## 4. Paper trading (stage 9)

```bash
python -m trading_bot.main paper --config configs/default.yaml
```

Requires a running MT5 terminal for the data feed. Orders are simulated
in-process but travel the identical code path — same engine, same risk gates,
same sizing. Run for at least three months and compare against a backtest over
the same period. Divergence means your cost model is wrong, not that live is
"different".

---

## 5. Live trading (stage 10)

**Two independent switches are required.** Neither alone is enough:

1. `mode: LIVE` in the config file
2. `--i-understand-the-risk` on the command line

```bash
python -m trading_bot.main live --config configs/live.yaml \
    --i-understand-the-risk --login 12345678 --server MyBroker-Demo
```

Before you do:

* Start on a **demo** account with the live config. Confirm fills, symbol
  digits, tick value and lot rounding against `results/trades.csv` expectations.
* Confirm `symbol_spec` is being pulled from the terminal, not from the YAML.
  `tick_value` is the field that silently ruins position sizing.
* Set `risk.risk_per_trade` to something you would accept losing four times in
  a row on day one, because `max_consecutive_losses` allows exactly that.
* Enable *Algo Trading* in the terminal, and allow it for the symbol.

**Restart safety.** State is reconstructed from market history on every poll,
and each intended trade is keyed by `symbol|poi_id|engulf_time` in
`state/trades.db` before the order is sent. Restarting mid-setup cannot produce
a duplicate entry.

**What is not handled:** requotes, partial fills, symbol suffixes that differ
between brokers (`EURUSD.pro`), and swap on positions held over the rollover.
Check each before trusting the account.

---

## 6. Configuration

`configs/default.yaml` is generated from the dataclasses, so it is always in
sync with the code. Regenerate:

```python
from trading_bot.config import Config
Config().save("configs/default.yaml")
```

Parameters most worth thinking about:

| Key | Default | Why it matters |
|-----|---------|----------------|
| `poi.displacement_atr_mult` | 1.0 | defines "strong move" in ATR units |
| `poi.max_leg_bars` | 5 | how far back the origin candle may sit |
| `retest.min_departure_pips` | 8.0 | how far price must leave before returning |
| `retest.max_penetration_pct` | 1.0 | 1.0 = a wick may reach the distal edge |
| `engulf.max_m5_bars_after_retest` | 6 | confirmation window |
| `trade.max_sl_atr_mult` | 2.5 | stop ceiling in the POI's own volatility (replaces the fixed 20 pips) |
| `trade.min_stop_spread_mult` | 8.0 | rejects stops so tight the spread eats the edge |
| `trade.max_entry_dist_atr` | 1.0 | how far past the zone the fill may sit |
| `risk.risk_per_trade` | 0.005 | 0.5 % |
| `costs.spread_pips` | 0.8 | check against your broker's actual average |

Every one of these is a hypothesis. Changing one after seeing results and
keeping the better number is how overfitting happens — use `walkforward`
instead, which selects parameters without ever seeing the test segment.

---

## 7. Project layout

```
trading_bot/     package (see SPECIFICATION.md §7)
tests/           86 unit tests
scripts/         MT5 export helper
configs/         default.yaml (BACKTEST), live.yaml (LIVE, gated)
data/            your CSVs
results/         backtest artefacts
```

---

## 8. Licence and disclaimer

Research code. Not investment advice. Retail FX accounts lose money at high
rates, and a backtested edge is a hypothesis about the future, not a property
of it.
