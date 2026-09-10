# Strategy Specification — M15 POI Retest / M5 Engulfing

Version: `poi_retest_engulf_v1.0.0`
Status: **research**. No live capital. No validated edge. See §10.

---

## 1. Hypothesis

Stated so it can be falsified rather than admired:

> **H1.** After price displaces away from a narrow M15 origin zone and later
> returns to it, an M5 momentum reversal at that zone predicts continuation in
> the displacement direction with sufficient reliability to beat a 1:2
> risk-reward payoff net of costs.

Breakeven for 1:2 is a 33.3 % win rate before costs. Costs in R units are

```
cost_R = (spread + slippage_entry + slippage_stop) / stop_distance_pips
```

With a 0.8-pip spread, 0.5 pip of total slippage and an 8-pip stop, `cost_R = 0.16`,
so the required win rate becomes `(1 + 0.16)/3 = 38.7 %`.

**This is the central quantitative problem with the concept.** The 20-pip stop
cap forces small stops; small stops maximise proportional cost drag. The
strategy must clear a bar roughly 5 percentage points above the naive one, and
the tighter the stop, the higher that bar. A rule that "improves" results by
selecting tighter stops is therefore probably making things worse, not better.

### 1.1 Costs move the barrier, not just the P/L

The usual "subtract costs from the result" intuition understates the problem.
Because the fill is one spread away from the price series that triggers the
exits, the entry starts closer to the stop than to the target. On a driftless
walk, optional stopping gives

```
P(TP before SL)  =  (R - c) / (3R)          c = spread + entry slippage, in pips
```

against 1/3 with zero costs. At R = 8 pips and c = 1.0 pip that is 29.2 %, not
33.3 %: a 4-point handicap purely from geometry, before any question of whether
the setup predicts anything. Combined with commission (which makes a loss
-1.05R rather than -1.00R), the honest breakeven for this system sits near
38-39 %.

**Measured on a driftless random walk** (213 trades, mean stop 10.8 pips): the
formula predicts 30.2 % wins; the backtester produced 26.8 %, which is 1.1
standard errors away. The engine reproduces the theoretical barrier geometry,
which is the strongest available evidence that it contains no look-ahead: a
look-ahead bug would show up as a win rate *above* the theoretical value.

H1 is rejected if the aggregate walk-forward out-of-sample expectancy is not
positive with a 95 % bootstrap confidence interval excluding zero.

---

## 2. Ambiguity analysis (Task §28.B–D)

The original description contains three undefined terms. Each is resolved below
with the candidates considered, and the selection criterion is always the same:
**which definition is compatible with a ≤20-pip stop and can be computed from
closed candles without judgement?**

### 2.1 "POI"

| # | Candidate | Zone width on EURUSD M15 | Verdict |
|---|-----------|--------------------------|---------|
| 1 | **Order block** — the last opposite-close candle before a displacement leg | 3–10 pips | **SELECTED** |
| 2 | Fair value gap — 3-candle imbalance | 1–8 pips | Implemented as alternative |
| 3 | Swing high/low ± fixed band | band is arbitrary | Implemented, least defensible |
| 4 | Supply/demand area (consolidation base → impulse) | 15–50 pips | Rejected |
| 5 | Support/resistance from clustered pivots | 20–60 pips | Rejected |

Candidates 4 and 5 are rejected on arithmetic, not taste: a stop beyond a
30-pip zone plus a buffer cannot fit inside 20 pips, so nearly every setup
would be discarded and the few survivors would be a biased low-volatility
sample. Candidate 3 needs an invented band height, which reintroduces exactly
the discretion this exercise removes.

The order block is selected because it is (a) narrow, (b) directional by
construction, (c) bounded by two prices that actually exist on the chart —
the origin candle's high and low — so the stop has a non-arbitrary anchor, and
(d) it is the structure the original trading idea is describing when it says
"price leaves the POI".

### 2.2 "Retest"

| # | Candidate | Verdict |
|---|-----------|---------|
| 1 | **Wick touches the proximal boundary (+ tolerance)** | **SELECTED** |
| 2 | Candle *closes* inside the zone | Available via `allow_wick_only_touch: false` |
| 3 | Price reaches the zone midpoint (50 % penetration) | Available via `max_penetration_pct` |

Selected #1 because it is the earliest unambiguous event and because entries
are confirmed separately by the engulfing rule — requiring a close inside the
zone *and* an engulfing candle stacks two confirmations and empirically starves
the sample.

### 2.3 "Engulfing"

| # | Candidate | Verdict |
|---|-----------|---------|
| 1 | **Real body engulfs prior real body, ties allowed** | **SELECTED (baseline)** |
| 2 | Full range engulfs full range | `engulf_use_range: true` |
| 3 | Body engulf + close in top/bottom X % + range expansion | `close_position_pct`, `min_range_ratio` |

Ties matter more than they look. In FX the open frequently equals the previous
close exactly; a strict `>` silently discards a large share of real signals.
The implementation uses `<=` / `>=` and excludes degenerate cases with
`min_body_pips` instead — otherwise every flat doji "engulfs" its neighbour.

---

## 3. Mathematical definitions

Notation: M15 bars are indexed `j`, M5 bars `i`. `O,H,L,C` are the usual
prices. `P` is the pip size (`utils.pip_size`, derived from digits or set
explicitly for non-FX symbols). `ATR_j` is Wilder ATR over `poi.atr_period`
M15 bars, known at the close of bar `j`.

### A. M15 POI (order block, primary)

At the close of M15 bar `j`, a **demand** POI exists iff:

```
(1)  C_j > O_j                                        bar j closes up
(2)  o = max{ k < j : C_k < O_k }  and  1 <= j-o <= max_leg_bars
(3)  C_j > H_o                                        the leg cleared the origin
(4)  (C_j - L_o) >= displacement_atr_mult * ATR_o     displacement is significant
(5)  min_zone_width <= (H_o - L_o)/P <= max_zone_width
```

Zone: `[lower, upper] = [L_o - pad, H_o + pad]` (or the body only if
`use_body_only`). `origin_time = t_o`, `created_time = bar_end_j`.
A **supply** POI is the exact mirror image.

Condition (2) is what removes "strong move" hand-waving: because `o` is *by
construction* the last opposite-close candle, the leg is simply "`k`
consecutive same-direction closes", and (4) makes its size ATR-relative rather
than a fixed pip number that would break across volatility regimes.

`strength = (C_j - L_o) / ATR_o`, recorded for later per-strength breakdown.

Optional condition (6) via `require_structure_break`: the leg must close beyond
the most recent *confirmed* swing pivot, where a pivot at `p` is confirmed only
at `p + swing_right`.

### B. Departure

Evaluated on M5 bars after `created_time`. For a demand POI, bar `i` is
*outside* iff `L_i > upper`. Departure is satisfied at the first `i` with

```
consecutive_outside_bars >= min_m5_bars_outside
AND  max(H_k - upper for k in [created, i]) / P >= min_departure_pips
AND  (optional) that same distance >= min_departure_atr * ATR_last_closed_M15
```

### C. Retest

After departure, the first M5 bar `i` with

```
L_i <= upper + touch_tolerance_pips * P          (demand)
```

subject to `penetration = (upper - L_i)/(upper - lower) <= max_penetration_pct`.
If `allow_wick_only_touch` is false, `C_i` must also be inside the zone.
Only the first `max_retests` retests are tradable (default 1).

### D. M5 engulfing

For consecutive M5 candles `(prev, cur)`, bullish engulfing iff:

```
prev.C < prev.O                                   prev bearish
cur.C  > cur.O                                    cur bullish
min(cur.O,cur.C) <= min(prev.O,prev.C)            body engulfs, ties allowed
max(cur.O,cur.C) >= max(prev.O,prev.C)
|cur.C - cur.O| >= min_body_ratio * |prev.C - prev.O|
min_body_pips * P <= |cur.C - cur.O| <= max_body_pips * P
```

plus optional `close_position_pct` and `min_range_ratio` filters. Bearish is
the mirror image.

### E. Valid entry

All of: POI active; departure satisfied; retest occurred; engulfing confirmed
within `max_m5_bars_after_retest` bars of the retest; engulfing bar touches the
zone within `zone_proximity_pips`; risk gates pass; stop within bounds.

Entry price (primary mode `next_open`):

```
LONG :  entry = O_{i+1} + (spread + slippage_entry) * P
SHORT:  entry = O_{i+1} - slippage_entry * P
```

where `i` is the engulfing bar. Alternatives (`engulf_close`, `limit_50`,
`breakout`) exist for research comparison only.

### F. Invalid setup

Any of: `C_i` beyond the distal boundary by more than `invalidation_buffer_pips`;
penetration exceeds `max_penetration_pct`; age exceeds `expiry_m15_bars`;
departure exceeds `max_departure_pips` (ran away, never coming back);
no engulfing within the confirmation window; superseded by an overlapping newer
POI in the same direction.

### G. Stop loss

```
LONG :  SL = lower - sl_buffer_pips * P
SHORT:  SL = upper + sl_buffer_pips * P
```

### H. Maximum stop

```
stop_pips = |entry - SL| / P
reject the trade if stop_pips > 20   (or < min_sl_pips)
```

The stop is **never** moved to fit the cap. Compressing it would silently
change the strategy into a different one with a worse win rate, and the
backtest would not show why.

### I. Take profit

```
LONG :  TP = entry + 2 * (entry - SL)
SHORT:  TP = entry - 2 * (SL - entry)
```

measured from the **actual fill**, not the signal price, so the advertised 1:2
is the 1:2 that is traded. Never modified after entry.

### POI multiplicity

Multiple POIs may be active simultaneously, capped at `max_active_pois` per
direction (oldest retired first). Two same-direction POIs whose ranges overlap
are collapsed to one; `prefer_on_overlap` decides newest (default) or strongest.
Opposite-direction POIs may overlap freely — that is a normal market state.

---

## 4. Look-ahead bias inventory (Task §28.E)

Every vector identified, and how it is closed:

| # | Vector | Mitigation |
|---|--------|------------|
| 1 | POI is defined by a leg that happens *after* the origin candle | POI `created_time = bar_end` of the confirming bar; `created_m5_index` gates all use. The zone's *prices* come from an earlier bar, which is legitimate; its *existence* does not. |
| 2 | **An M15 retest cannot be seen on M15 closes.** Price is inside the zone during a bar that has not closed. | Retest and trigger detection run entirely on the M5 timeline. Only POI *creation* uses closed M15 bars. |
| 3 | Swing pivots need `right` future bars | Pivot at `p` usable only from `p + swing_right`; enforced in `_broke_structure`. |
| 4 | Resampling label conventions | `label='left', closed='left'`; `bar_end = label + 15min`; `m15_ready[i]` = last bar with `bar_end <= m5_bar_end[i]`. Unit-tested at the boundary. |
| 5 | Entry at the engulfing bar's close | Primary mode enters at the *next* M5 open. `engulf_close` mode is flagged as optimistic. |
| 6 | Same-bar SL/TP ambiguity | M1 execution model; `ambiguous_bars` counter reports how many outcomes are assumptions. |
| 7 | Indicator warm-up leaking | ATR has `min_periods`; POIs need `j >= max(atr_period, max_leg_bars+1)`. |
| 8 | Parameter selection on the test set | Walk-forward with a separate validation stage; TEST executed once with frozen parameters. |
| 9 | Symbol/period cherry-picking | Not resolvable in code. Discipline: fix the symbol and period *before* looking at results, and report every run. |
| 10 | Survivorship in the data itself | MT5 history is broker-specific and revised; document the export date and broker. |

Regression test `test_signal_does_not_depend_on_future_bars` truncates the
series right after a signal and asserts the signal is unchanged. That is the
cheapest general defence against reintroducing #1–#5 during refactoring.

---

## 5. M15 / M5 synchronisation (Task §28.F)

M15 candles are **resampled from M5**, never downloaded separately, for two
reasons: a broker's native M15 series can disagree with its M5 series around
missing ticks and session edges; and resampling makes the knowability question
mechanical.

```
M15 bar labelled 10:00  =  M5 bars 10:00, 10:05, 10:10
                        =  complete at 10:15:00
                        =  first knowable at the CLOSE of the M5 bar labelled 10:10
```

`AlignedData.m15_ready[i]` stores, for every M5 bar `i`, the index of the last
M15 bar that had already closed. The strategy engine may only ever look at
`m15[:m15_ready[i]+1]`. This costs up to 15 minutes of latency on POI creation
and is the correct price to pay.

M1 data is aligned the same way (`m1_start[i]`) and is used *only* for
execution, never for signals.

---

## 6. Backtesting methodology (Task §28.G)

Event-driven, single pass over M5 bars. Per bar `i`:

1. fill orders queued at bar `i-1`'s close, at bar `i`'s **open**
2. progress open positions through bar `i` (M1 resolution when available)
3. mark equity
4. feed bar `i` to the strategy engine → signals
5. size, validate, queue for bar `i+1`

Steps 4–5 may not read bar `i+1`; step 2 may not read beyond bar `i`.

**Quoting convention.** OHLC is treated as the bid series (MT5 convention),
`ask = bid + spread`. Longs fill at the ask and exit at the bid; shorts fill at
the bid and are bought back at the ask. Each trade pays exactly one spread.
Gaps fill at the bar open when that is worse than the level.

**Intrabar resolution.** With a 1:2 payoff the SL and TP are ~24 pips apart on
an 8-pip stop; an M5 bar spanning both is uncommon but not rare on news. The
M1 model walks minute bars in order. Where both levels sit inside the same
*minute*, the pessimistic assumption (SL first) applies. `compare-exec`
reports M1 against M5 SL-first / TP-first / 50-50 so the size of the
assumption is visible rather than buried.

**Costs.** Spread, entry slippage, stop slippage, per-lot commission, optional
swap. Sensitivity grid at 1×/1.5×/2× spread and 1×/2× slippage.

**Validation ladder.** In-sample → chronological holdout → walk-forward
(train 6m / validate 2m / test 2m, rolling by 2m) → Monte Carlo. The only
number that means anything about the future is the aggregate of the walk-forward
TEST segments.

---

## 7. Architecture

```
trading_bot/
  config.py        typed dataclasses, YAML/JSON, dotted-path overrides
  data.py          ingestion, hygiene, resampling, causal alignment maps
  indicators.py    ATR, swing pivots, engulfing predicate
  strategy.py      POI detection + lifecycle state machine + signals
  risk.py          SL/TP, 20-pip rule, position sizing, risk gates
  backtest.py      event loop, M1/M5 execution models, trade records
  metrics.py       descriptive stats AND inferential stats
  optimization.py  penalised objective, random/Optuna search, cost stress
  walkforward.py   train/validate/test folds, aggregate OOS
  montecarlo.py    reshuffle, bootstrap, cost perturbation, ruin
  viz.py           equity, drawdown, R distribution, per-trade charts
  execution.py     MT5 broker, paper broker, live loop, state store
  logger.py        structured logs, event stream, SQLite trade store
  synthetic.py     random-walk generator for plumbing tests only
  main.py          CLI
tests/             86 unit tests, runnable with or without pytest
configs/           default.yaml (BACKTEST), live.yaml (LIVE, gated)
```

Separation is enforced by dependency direction: `strategy.py` imports no
execution code and knows nothing about money; `risk.py` knows nothing about
bars; `backtest.py` and `execution.py` both consume the same `StrategyEngine`,
which is what makes backtest and live behaviour comparable.

---

## 8. Design decisions worth defending

**Why the engine replays from scratch in live mode.** `LiveTrader.poll_once`
rebuilds the entire POI state from the last N closed bars on every new bar
instead of maintaining incremental state. It is O(N) wasteful and completely
deterministic: a restart mid-setup reproduces exactly the same state, and there
is no serialised state file to corrupt. For a 5-minute cycle this is free.

**Why commission enters the position-sizing denominator.**
`lots = risk_budget / (loss_per_lot + commission_per_lot)` keeps total
loss-if-stopped inside the risk budget. Sizing on the stop alone systematically
over-risks by the commission.

**Why `reject_if_below_min_lot` defaults to true.** When the computed size
rounds below the broker minimum, taking the minimum lot means silently risking
more than the mandate. Skipping the trade is the honest option, and the skip is
logged so the frequency is visible.

**Why the consecutive-loss brake resets daily.** Found by running the code: as
a permanent counter it cannot ever be cleared, because a halted strategy never
produces the win that would clear it. The first version silently disabled
itself part-way through the backtest and blocked 34 setups. It is now a daily
circuit breaker.

**Why a 20.0-pip stop must compare with a tolerance.** `1.1010 - 1.0990` is
`0.0020000000000000018` in IEEE-754, i.e. 20.000000000000004 pips, which fails
a naive `> 20` test. Caught by unit test, not by inspection.

**Why the synthetic generator simulates sub-minute steps.** The first version
drew each bar's high and low as independent noise around the open and close.
That is cheap and looks fine on a chart, but the extremes do not lie on any
continuous path, so barrier touches are inflated - and since the stop is nearer
than the target, the *stop* gains more. It reported a 21 % win rate on data
that should give ~30 %, which is indistinguishable from a broken strategy. The
generator now walks 12 sub-steps per minute and aggregates true OHLC. Test data
needs validating as carefully as the code it tests.

**Why timestamps are carried as int64 nanoseconds, converted explicitly.**
`series.astype("int64")` on a datetime column is not portable: pandas 3 stores
`datetime64[us]` by default, so that cast yields microseconds and every
timestamp in the trade log silently becomes 1970. The conversion is forced to
`datetime64[ns]` in one place (`data._to_ns`).

**Why the objective is `expectancy_R * sqrt(n)`.** That is, up to a constant,
the t-statistic. Maximising net profit over a parameter grid on a few hundred
trades is a machine for finding noise; requiring the edge to be both material
and supported by sample size is the minimum defence.

---

## 9. What the system does NOT do

* It does not know whether the strategy works. No market data was available in
  the environment where it was written. Every number produced so far comes from
  a **random walk** and is meaningful only as a plumbing check.
* It does not model partial fills, requotes, variable spread by time of day
  (unless a spread column is supplied), or broker-side stop hunting.
* It does not model correlation across symbols; it is single-symbol.
* Monte Carlo assumes i.i.d. trades. Real trades cluster with volatility, so
  the true drawdown tail is fatter than the simulation shows.
* It has no news filter. High-impact releases are precisely when a 20-pip stop
  and a wide spread interact badly.

---

## 10. Research protocol and decision gates

| Stage | Action | Gate to pass |
|-------|--------|--------------|
| 1 | Specification | this document |
| 2 | Signal detector | unit tests green |
| 3 | Visual audit | 30 random signals plotted and manually confirmed |
| 4 | Event-driven backtest | funnel counts plausible; ambiguous-bar rate reported |
| 5 | Costs | baseline + 1.5× + 2× spread |
| 6 | Out-of-sample holdout | OOS expectancy not materially below IS |
| 7 | Walk-forward | aggregate OOS mean R > 0 with 95 % CI excluding zero |
| 8 | Monte Carlo | p(drawdown > 20 %) acceptable at chosen risk |
| 9 | Paper trading | ≥ 3 months, fills within tolerance of the backtest |
| 10 | Live, minimum size | only after 9 |

**If stage 7 fails, the correct output is a report saying so.** Re-running with
different parameters until it passes is not research; it is the mechanism by
which overfitted systems reach live accounts. The rejection rate at each funnel
stage is logged specifically so that a "fix" that merely shrinks the sample is
visible as such.
