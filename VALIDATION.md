# Validation Report — what has and has not been established

Date of runs: environment had **no market data and no network access**.
Everything below was produced on a synthetic driftless random walk
(560 days, 72,000 M5 bars, 360,000 M1 bars, seed 7).

---

## 1. The distinction that matters

| Claim | Status |
|-------|--------|
| The code implements the specification | **evidence: 86 unit tests, all passing** |
| The code contains no look-ahead | **evidence: truncation test + barrier-geometry match** |
| The backtester's costs behave correctly | **evidence: monotone cost stress + closed-form check** |
| The strategy has an edge on EURUSD | **no evidence either way — untested** |
| The strategy has an edge on any real market | **no evidence either way — untested** |

Nothing in this file is a performance claim. A random walk has no order
blocks, no liquidity and no autocorrelation; the correct expectancy on it is
negative by exactly the transaction costs, and that is what makes it a useful
test rather than a useless one.

---

## 2. Unit tests

```
86 passed, 0 failed          python tests/run_tests.py
```

Coverage: bullish/bearish engulfing incl. ties, dojis and partial engulfs;
POI creation, expiry, invalidation, deduplication, width rejection; retest
detection and penetration limits; departure requirements; SL/TP arithmetic; the
20-pip rule at, above and below the boundary; pip conversion for 5-digit,
3-digit and non-FX symbols; lot sizing across instruments and equity levels;
risk gates and their daily reset; intrabar SL/TP resolution M1 vs M5; gap
fills; spread on short buy-backs; timezone conversion; alignment causality;
Monte Carlo mechanics; walk-forward window construction.

---

## 3. Look-ahead evidence

**Direct test.** `test_signal_does_not_depend_on_future_bars` truncates the
series one bar after a signal and asserts the signal is unchanged.

**Indirect and stronger.** On a driftless walk the win rate is determined by
geometry alone:

```
P(TP before SL) = (R - c) / (3R)
```

| quantity | value |
|----------|-------|
| mean stop R | 10.81 pips |
| costs c (spread 0.8 + slippage 0.2) | 1.00 pip |
| P(TP) predicted | 30.25 % |
| P(TP) observed | 26.76 % (−1.1 SE, n = 213) |

A look-ahead bug inflates the win rate *above* theory. The observed value sits
slightly below it, which is what noise looks like.

---

## 4. Execution-model comparison (§13)

| model | trades | win rate | expectancy R | net |
|-------|--------|----------|--------------|-----|
| M1 execution | 213 | 26.8 % | −0.305 | −2,547 |
| M5 SL-first | 213 | 26.8 % | −0.305 | −2,547 |
| M5 TP-first | 213 | 26.8 % | −0.305 | −2,547 |
| M5 50/50 | 213 | 26.8 % | −0.305 | −2,547 |

Identical, because `ambiguous_bars = 0`: no M5 bar in this dataset contained
both the stop and the target. That is a property of low-volatility synthetic
data with a ~32-pip span between barriers, **not** a general finding. On real
FX around news releases the count will be non-zero, and the M1 model is then
the only defensible one. The `ambiguous_bars` diagnostic is printed on every
run precisely so this is checked rather than assumed.

The M1 model is verifiably active: its exits carry minute-level timestamps,
the M5 model's are always on 5-minute boundaries.

---

## 5. Cost sensitivity (§14)

| spread × | slippage × | trades | win rate | expectancy R |
|----------|-----------|--------|----------|--------------|
| 1.0 | 1.0 | 213 | 26.8 % | −0.305 |
| 1.0 | 2.0 | 213 | 26.8 % | −0.334 |
| 1.5 | 1.0 | 212 | 25.9 % | −0.352 |
| 2.0 | 1.0 | 211 | 24.6 % | −0.397 |
| 2.0 | 2.0 | 211 | 24.6 % | −0.427 |

Monotone and of the predicted magnitude: doubling the spread on an ~11-pip stop
costs about 0.08R, matching `Δc / R`. Note the win rate itself falls with
costs — the barrier-shift effect, not a coincidence.

---

## 6. Walk-forward (§18) — the most instructive result here

Train 6m / validate 2m / test 2m, rolling 2m, 12 search trials per fold.

| fold | test window | trades | win rate | expectancy R | net |
|------|-------------|--------|----------|--------------|-----|
| 0 | 2024-09 → 2024-11 | 15 | 40.0 % | **+0.132** | +85.61 |
| 1 | 2024-11 → 2025-01 | 10 | 40.0 % | **+0.126** | +60.15 |
| 2 | 2025-01 → 2025-03 | 4 | 50.0 % | **+0.431** | +76.58 |
| 3 | 2025-03 → 2025-05 | 19 | 31.6 % | −0.146 | −128.60 |
| 4 | 2025-05 → 2025-07 | 27 | 25.9 % | −0.304 | −375.31 |

**Aggregate out-of-sample: 75 trades, 33.3 % win rate, −0.080R,
95 % CI [−0.404, +0.244], p = 0.63.**

Three of five folds are profitable out-of-sample, one of them at +0.43R — on
data that provably has no edge. Reporting folds 0–2 would produce a convincing
and completely false result. This is what cherry-picking looks like from the
inside, and it is why §27 of the brief exists.

---

## 7. Monte Carlo (§19)

5,000 simulations on the 213-trade sequence at 0.5 % risk.

| mode | median return | 5th pct | median max DD | p(DD>20 %) |
|------|---------------|---------|---------------|------------|
| reshuffle | −28.1 % | −28.1 % | 29.4 % | 1.00 |
| bootstrap | −28.1 % | −38.8 % | 30.0 % | 0.92 |
| bootstrap +0.05R slip | −31.3 % | −41.3 % | 32.7 % | 0.97 |
| bootstrap +0.10R slip | −34.1 % | −43.8 % | 35.3 % | 0.99 |

Reshuffling leaves the final return unchanged (multiplication commutes) and
only redistributes the path — which is the point: **drawdown is partly an
ordering accident, terminal return is not.**

---

## 8. Two real defects found by running the code

1. **The consecutive-loss brake disabled the strategy permanently.** As a
   non-resetting counter it can never be cleared, because a halted strategy
   never produces the win that would clear it. It silently blocked 34 setups
   before being converted to a daily circuit breaker.
2. **An exactly-20.0-pip stop was rejected.** `1.1010 − 1.0990` evaluates to
   20.000000000000004 pips in IEEE-754. Caught by a unit test written at the
   boundary, not by reading the code.

Plus two in the test infrastructure itself: a synthetic generator whose bar
extremes were not on a continuous path (biasing the win rate down by ~9 points),
and a `datetime64[us]` cast that put every trade timestamp in 1970.

---

## 9. What must happen before this touches money

1. Export real EURUSD M5 + M1 from your broker; verify the timezone offset.
2. Re-run stages 4–8 on that data. The funnel counts and `ambiguous_bars` are
   the first things to read, not the P/L.
3. Plot 30 random signals (`viz.plot_trade`) and confirm each is the setup you
   intended. Automated tests cannot check that the rules encode your idea —
   only that they encode *some* idea consistently.
4. Judge on the **aggregate walk-forward out-of-sample** figure with its
   confidence interval. If the CI spans zero, the answer is "no evidence of an
   edge", and the honest response is to report that rather than to search
   harder.
