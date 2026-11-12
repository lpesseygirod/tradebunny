# Backtest survey — every candidate, scored

Generated 2026-09-10T11:10:59+00:00 · OKX 1H candles · fees 0.080% maker (base tier, stated) · spacing 0.6436%

Regenerated after the Kelly fix, and now scoring **every** candidate
rather than only survivors — a best score of 0.48 and a best of 0.05 are
different results, and this table is what tells them apart.

## The data window is NOT two years, and it is not consistent

This was worth checking and the answer is no. The cached series span:

- **5,682 to 75,910 bars** (237 to 3163 days) across 29 pairs
- median **36,313 bars** (1513 days)
- 4 pairs have under 400 days; 14 have over 2000

`get_history` pages back until the exchange runs out, so a long-listed
pair returns eight years and a recent listing returns eight months. Any
comparison ACROSS pairs is therefore comparing different amounts of
history, and a per-pair result describes that pair's own window and
nothing wider. The window is now printed with every scan and backtest,
and `cli backtest --deep` says loudly when it is short.

## Every candidate, with its confidence

| Pair | bars | days | from | to | confidence | outcome |
|---|---:|---:|---|---|---:|---|
| `USDC-USDT` | 69,243 | 2885 | 2018-10-16 | 2026-09-09 | 0.61 | rejected: ATR |
| `USDG-USDT` | 10,916 | 455 | 2025-06-11 | 2026-09-09 | 0.48 | rejected: ADX |
| `DOGE-USDT` | 62,641 | 2610 | 2019-07-18 | 2026-09-09 | 0.16 | rejected: ADF |
| `OKB-USDT` | 74,207 | 3092 | 2018-03-23 | 2026-09-09 | 0.16 | rejected: ADF |
| `BNB-USDT` | 32,598 | 1358 | 2022-12-21 | 2026-09-09 | 0.15 | rejected: ADF |
| `FIL-USDT` | 50,547 | 2106 | 2020-12-03 | 2026-09-09 | 0.15 | rejected: ADF |
| `ADA-USDT` | 71,281 | 2970 | 2018-07-23 | 2026-09-09 | 0.15 | rejected: ADF |
| `UNI-USDT` | 52,399 | 2183 | 2020-09-17 | 2026-09-09 | 0.14 | rejected: ADF |
| `SUI-USDT` | 29,397 | 1225 | 2023-05-03 | 2026-09-09 | 0.13 | rejected: ADF |
| `SOPH-USDT` | 11,252 | 469 | 2025-05-28 | 2026-09-09 | 0.13 | rejected: ADF |
| `ETH-USDT` | 75,910 | 3163 | 2018-01-11 | 2026-09-09 | 0.12 | rejected: ADF |
| `NEAR-USDT` | 50,547 | 2106 | 2020-12-03 | 2026-09-09 | 0.12 | rejected: ADF |
| `ARB-USDT` | 30,380 | 1266 | 2023-03-23 | 2026-09-09 | 0.12 | rejected: ADF |
| `SOL-USDT` | 52,079 | 2170 | 2020-09-30 | 2026-09-09 | 0.12 | rejected: ADF |
| `WLD-USDT` | 27,432 | 1143 | 2023-07-24 | 2026-09-09 | 0.11 | rejected: ADF |
| `XAUT-USDT` | 36,313 | 1513 | 2022-07-19 | 2026-09-09 | 0.11 | rejected: ADF |
| `TRUMP-USDT` | 14,356 | 598 | 2025-01-19 | 2026-09-09 | 0.11 | rejected: ADF |
| `PEPE-USDT` | 29,448 | 1227 | 2023-05-01 | 2026-09-09 | 0.09 | rejected: ADF |
| `LINK-USDT` | 75,910 | 3163 | 2018-01-11 | 2026-09-09 | 0.09 | rejected: ADF |
| `PUMP-USDT` | 10,034 | 418 | 2025-07-18 | 2026-09-09 | 0.09 | rejected: ADF |
| `XRP-USDT` | 75,910 | 3163 | 2018-01-11 | 2026-09-09 | 0.08 | rejected: ADF |
| `ENA-USDT` | 8,570 | 357 | 2025-09-17 | 2026-09-09 | 0.08 | rejected: ADF |
| `LIT-USDT` | 5,682 | 237 | 2026-01-15 | 2026-09-09 | 0.08 | rejected: ADF |
| `BTC-USDT` | 75,910 | 3163 | 2018-01-11 | 2026-09-09 | 0.07 | rejected: ADF |
| `DOT-USDT` | 53,015 | 2209 | 2020-08-22 | 2026-09-09 | 0.07 | rejected: ADF |
| `ETC-USDT` | 75,910 | 3163 | 2018-01-11 | 2026-09-09 | 0.07 | rejected: ADF |
| `HYPE-USDT` | 7,411 | 309 | 2025-11-04 | 2026-09-09 | 0.07 | rejected: ADF |
| `LTC-USDT` | 17,519 | 730 | 2024-09-10 | 2026-09-10 | 0.07 | rejected: ADF |
| `ZEC-USDT` | 6,933 | 289 | 2025-11-24 | 2026-09-09 | 0.07 | rejected: ADF |

Confidence here omits the walk-forward component, which only runs for
candidates that clear the gates — so these are a lower bound, and a
rejected candidate's true score could only be higher by at most 0.15.

## What the scores actually say

- Best score overall: **0.61** (`USDC-USDT`), which is a stablecoin rejected on ATR — it does not move enough to cross a grid level, so its statistical calm is worthless.
- Best NON-stablecoin: **0.16**.
- Median across all pairs: **0.11**.

Against a strict threshold of 0.65, the tradeable candidates cluster
around 0.1. This is the distinction the brief asked for and the answer
is unambiguous: **not a near miss.** Nothing is sitting just under the
line waiting for a slightly looser setting — the evidence is absent,
not marginal. Dropping to `exploratory` (0.35) would still surface
nothing.

## Why they were rejected

| Gate | Count |
|---|---:|
| ADF | 27 |
| ATR | 1 |
| ADX | 1 |

ADF dominates: on these windows most pairs cannot reject a random walk
at all, which is the primary evidence the whole strategy rests on.

## What this run does not tell you

- **Windows differ per pair** — see above. This is the most important
  caveat in the file.
- **Fees are the base tier**, stated explicitly. On the EEA fallback
  (0.200% maker) the spacing floor is 2.5x wider and the ATR gate moves
  with it, so more pairs would fail.
- **One split per pair**, not a distribution.
- **Survivorship**: today's cached universe, which was today's top 30 by
  volume when it was fetched.

---

# The range/statistics window split, and the A/B re-run

*Re-run 2026-09-11, after fixing the window split. Supersedes the inventory
A/B previously recorded in `docs/THEORY.md`.*

## What was wrong

Every backtest before this one fitted the grid's **band** on the same stretch
of history it used to judge the asset's **character**. Those are different
questions and they need different windows:

- **Character** — is this thing mean-reverting? — is a slow property. ADF,
  Hurst, ADX, efficiency ratio, stability, HAR volatility. A short window
  cannot see it. **365 days.**
- **Band** — where is price *now*? — is not a property of the asset at all.
  `suggest_range`, containment, touches, ATR. **45 days.**

Using the long window for both produced bands like LTC **66–126**, whose upper
edge is nearly double its lower. Across the 29-pair universe the mean band was
**190% of its own lower bound, spanning 152 levels.** No ladder can trade that:
rungs spaced to clear fees end up so far apart that price drifts between them
for weeks.

The failure is invisible in a returns column and obvious in a cycles column,
which is why one is added below.

## What changed

| | old | new |
|---|---:|---:|
| mean band width (as % of lower) | 190.5% | **26.9%** |
| mean levels per grid | 152 | **34** |
| mean out-of-sample cycles | 79.5 | **35.1** |
| pairs completing **zero** cycles | 10/29 | **14/29** |

Both windows are now printed with dates and bar counts on every proposal,
every backtest row, and every `scan_results.md` entry.

## The A/B, re-run with corrected ranges

29 pairs, 2 500 out-of-sample hourly bars each, three variants. Statistics
window 365 days, range window 45 days, uniform across pairs — the previous run
let the in-sample window vary from 3 181 to 73 410 bars depending on listing
age, which confounded the comparison with history length.

| pair | stats window | range window | band | width | levels | cycles | fixed | bound | bound+skew |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| `ADA-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.2402-0.2752 | 14.6% | 21 | **0** | +0.00% | +0.00% | +0.00% |
| `ARB-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.10807-0.14169 | 31.1% | 42 | **0** | +0.00% | +0.00% | +0.00% |
| `BNB-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 615.7-671.9 | 9.1% | 13 | **6** | -6.55% | -6.55% | -4.68% |
| `BTC-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 74344.2-81341.8 | 9.4% | 14 | **0** | +0.00% | +0.00% | +0.00% |
| `DOGE-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.09394-0.1135 | 20.8% | 29 | **10** | -2.80% | -2.80% | -2.26% |
| `DOT-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 1.189-1.366 | 14.9% | 21 | **8** | -1.48% | -1.48% | -1.12% |
| `ENA-USDT` | 6070b 2025-09-17..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.09631-0.12935 | 34.3% | 45 | **0** | +0.00% | +0.00% | +0.00% |
| `ETC-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 8.331-9.667 | 16.0% | 23 | **0** | -0.28% | -0.28% | -0.28% |
| `ETH-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 2078.53-2382.86 | 14.6% | 21 | **0** | +0.00% | +0.00% | +0.00% |
| `FIL-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.9059-1.1486 | 26.8% | 36 | **29** | -2.11% | -2.11% | -1.47% |
| `HYPE-USDT` | 4911b 2025-11-04..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 39.682-61.475 | 54.9% | 68 | **182** | +5.76% | +5.76% | +2.19% |
| `LINK-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 9.091-10.517 | 15.7% | 22 | **4** | -1.13% | -1.13% | -0.99% |
| `LIT-USDT` | 3181b 2026-01-15..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.869-1.3159 | 51.4% | 64 | **23** | +5.73% | +5.73% | +1.88% |
| `LTC-USDT` | 6260b 2025-09-10..2026-05-29 | 1080b 2026-04-14..2026-05-29 | 52.19-58.47 | 12.0% | 17 | **1** | -0.63% | -0.63% | -0.61% |
| `NEAR-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 1.281-2.512 | 96.1% | 104 | **565** | +5.71% | +5.71% | -0.70% |
| `OKB-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 81-88.44 | 9.2% | 13 | **6** | -6.88% | -6.88% | -4.85% |
| `PEPE-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 3.541e-06-4.297e-06 | 21.3% | 30 | **0** | +0.00% | +0.00% | +0.00% |
| `PUMP-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.001706-0.002141 | 25.5% | 35 | **33** | -0.82% | -0.82% | -0.23% |
| `SOL-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 83.17-94.77 | 13.9% | 20 | **0** | -0.25% | -0.25% | -0.25% |
| `SOPH-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.00763-0.00952 | 24.8% | 34 | **0** | +0.00% | +0.00% | +0.00% |
| `SUI-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.9163-1.2445 | 35.8% | 47 | **3** | -0.37% | -0.37% | -0.35% |
| `TRUMP-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 2.021-2.974 | 47.2% | 60 | **0** | +0.00% | +0.00% | +0.00% |
| `UNI-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 3.177-3.813 | 20.0% | 28 | **0** | +0.00% | +0.00% | +0.00% |
| `USDC-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.9995-1.001 | 0.2% | 2 | **0** | +0.00% | +0.00% | +0.00% |
| `USDG-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.9995-1.0011 | 0.2% | 2 | **0** | -0.05% | -0.05% | -0.05% |
| `WLD-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 0.2377-0.3271 | 37.6% | 49 | **89** | +3.55% | +3.55% | +1.12% |
| `XAUT-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 4498.4-4795.9 | 6.6% | 9 | **1** | -1.07% | -1.07% | -1.02% |
| `XRP-USDT` | 6260b 2025-09-09..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 1.3354-1.4685 | 10.0% | 14 | **0** | -1.19% | -1.19% | -1.12% |
| `ZEC-USDT` | 4433b 2025-11-24..2026-05-28 | 1080b 2026-04-13..2026-05-28 | 317.39-657.85 | 107.3% | 113 | **57** | -6.45% | -6.45% | -5.65% |

**NEW - band fitted on the last 45 days of the in-sample stretch**

| variant | mean return | mean max DD | mean P&L σ | mean cycles | pairs with 0 cycles |
|---|---:|---:|---:|---:|---:|
| fixed size | -0.390% | 1.719% | 0.00076 | 35.1 | 14/29 |
| hard bound | -0.390% | 1.719% | 0.00076 | 35.1 | 14/29 |
| bound + skew | -0.705% | 1.254% | 0.00056 | 34.0 | 14/29 |

**OLD - band fitted on the whole in-sample stretch (the defect)**

| variant | mean return | mean max DD | mean P&L σ | mean cycles | pairs with 0 cycles |
|---|---:|---:|---:|---:|---:|
| fixed size | +0.606% | 1.267% | 0.00039 | 79.5 | 10/29 |
| hard bound | +0.606% | 1.267% | 0.00039 | 79.5 | 10/29 |
| bound + skew | -0.144% | 1.039% | 0.00030 | 78.0 | 10/29 |

OLD: mean band width 190.5% of lower, mean levels 152

NEW: mean band width 26.9% of lower, mean levels 34

## Reading this honestly

**The corrected numbers are worse, and that is the point.** Mean return goes
from +0.61% to −0.39%. The old figure was not a better result, it was a
measurement of grids that mostly did not trade: a 190%-wide band with 152
rungs catches a lot of incidental oscillation across a year of regimes while
never being a band anyone would have deployed.

**Zero-cycle pairs went UP, from 10 to 14.** A tight, current band is the right
band to place today, but this backtest holds it fixed for 2 500 bars — 104
days — while never re-fitting. In a trending market price leaves a 45-day band
and does not come back, so the grid buys nothing and earns nothing. Fourteen
pairs show exactly that: zero cycles *and* zero peak inventory.

This is a real limitation of the experiment, not of the split. A 45-day band
is designed to be re-fitted; scoring it over 104 days without re-fitting
measures the wrong thing. **The honest reading is that neither column is a
forecast of live performance** — the old one flattered by trading a band
nobody would deploy, the new one penalises by refusing to re-deploy a band
that has expired.

**The hard bound never binds.** `bars_at_bound` is zero across all 29 pairs in
both runs, and "hard bound" is byte-identical to "fixed size" in every column.
See `docs/THEORY.md` — at the default `fraction=1.0` the bound is arithmetically
unreachable. The variance reduction in the third row is entirely the size skew.

**The gates still reject all of this.** Every pair above is run through
`walk_forward` directly, bypassing the rubric. A live scan proposes nothing,
because ADF cannot reject a random walk on almost any of these pairs. Nothing
here is a trading recommendation.

---

# Rolling re-fit, a binding Q_max, and the baseline reconciled

*2026-09-11.*

## Reconciling the three "before" numbers

Three different figures have been quoted as the fixed-size baseline. They are
**three different experiments**, and the previous report was wrong to treat one
as the "before" for another.

| figure | where from | band fitted on | plan built by | fixed arm |
|---|---|---|---|---|
| **−0.888%** | original `docs/THEORY.md` table | 1 500 bars (62d) | `GridPlan` directly, capital 1 000, stop 15% | `max_inventory=1e12` |
| **+0.606%** | my "OLD control", previous report | 6 260 bars (261d) | `walk_forward`, stop 5% | no bound |
| **−0.390%** | my "NEW", previous report | 1 080 bars (45d) | `walk_forward`, stop 5% | no bound |

The original script was recovered from the session transcript and **re-run
verbatim**: it produces **−0.911%** today against **−0.888%** as recorded. The
gap is the candle cache having been topped up since, which shifts the `[-4000:]`
slice. The original number is reproduced and is not in question.

Holding the universe, plan construction and OOS window fixed and varying **only**
the band-fitting window:

| band window | mean return | mean cycles | 0-cycle pairs | mean band width | mean levels |
|---|---:|---:|---:|---:|---:|
| 1 500 bars (62d) — the original | −0.911% | 66.3 | 8/29 | 34.3% | 41 |
| 1 080 bars (45d) — current default | −0.336% | 50.4 | 12/29 | 27.3% | 35 |
| 240 bars (10d) | −3.396% | 41.7 | 9/29 | 15.2% | 20 |

**Two corrections to the previous report**, both of which I got wrong:

1. I described the original table as fitted on "the whole in-sample stretch".
   It was fitted on 62 days. The wide-band defect was real but lived in
   `walk_forward`, not in that table.
2. I said the original's `bound ≠ fixed` result "cannot have been right". It
   used `fraction=0.5`, which does engage. The arm that could not differ was
   mine, at `fraction=1.0`.

**The +0.61% figure should not be quoted again.** It was my own reconstruction's
control, using a 261-day band that nothing in the system ever proposed.

## Fix 1 — rolling re-fit

A single-fit backtest holds one band for 104 days. Live, the analyst re-scans
and proposes a fresh grid, so that measured band staleness, not the strategy.
`backtest.rolling_walk_forward` re-derives the band from the trailing 45 days
every 14 days and rebuilds the ladder. Each segment is still strictly
out-of-sample: the band comes only from bars before the segment starts.

**Inventory rule at re-fit: `LIQUIDATE`, the pessimistic choice.** Tearing down
a grid means cancelling its resting orders; the inventory they bought is sold at
market, paying the **taker** fee and crossing the spread. `CARRY` is available
to measure what that assumption costs, but it is not the default — carrying lots
into a ladder with different rungs hides the cost of re-ranging.

## Fix 2 — Q_max that can bind

`Q_max = max_quote_total / lower` was reachable only if every rung filled at
exactly the bottom price. `bars_at_bound` was **zero** on all 29 pairs. Now:

```
max_inventory = sum(quote_per_level / price_i)  over all buy levels
Q_max         = BOUND_FRACTION * max_inventory  (BOUND_FRACTION = 0.6)
```

`test_bars_at_bound_is_positive_on_a_straight_decline` fails if it stops
engaging. On real data it still engages rarely — 24 bars out of ~72 500 — which
is what insurance looks like.

## A/B with rolling re-fit and a binding Q_max

29 pairs, 2 500 out-of-sample hourly bars, re-fit every 14 days.

| variant | fit | mean return | mean max DD | mean P&L σ | mean cycles | 0-cycle pairs | bars outside band | bars at bound |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| fixed size | single | -0.402% | 1.684% | 0.00077 | 34.2 | 14/29 | 78.7% | 0 |
| fixed size | **rolling** | -1.451% | 5.294% | 0.00088 | 161.1 | 2/29 | 43.9% | 0 |
| hard bound | single | -0.359% | 1.642% | 0.00074 | 34.2 | 14/29 | 78.7% | 4 |
| hard bound | **rolling** | -1.413% | 5.256% | 0.00087 | 161.0 | 2/29 | 43.9% | 24 |
| bound + skew | single | -0.708% | 1.080% | 0.00050 | 32.5 | 14/29 | 78.7% | 0 |
| bound + skew | **rolling** | -1.984% | 3.745% | 0.00053 | 157.3 | 2/29 | 43.9% | 0 |

### The effect of re-fitting, on its own

| | single fit | rolling re-fit |
|---|---:|---:|
| mean cycles | 34.2 | **161.1** |
| pairs with 0 cycles | 14/29 | **2/29** |
| bars outside the band | 78.7% | **43.9%** |
| mean return | -0.402% | **-1.451%** |
| mean max drawdown | 1.684% | **5.294%** |
| mean re-fits | 1.0 | **8.0** |
| total segments stopped out | 23 | **65** |

### Per pair (rolling, fixed size unless noted)

| pair | cycles | outside band | at bound | re-fits | fixed | bound | bound+skew |
|---|---:|---:|---:|---:|---:|---:|---:|
| `ADA-USDT` | 198 | 42% | 0 | 8 | +3.09% | +3.09% | -2.10% |
| `ARB-USDT` | 248 | 36% | 0 | 8 | +5.24% | +5.24% | -0.57% |
| `BNB-USDT` | 51 | 56% | 3 | 8 | -8.67% | -7.85% | -5.29% |
| `BTC-USDT` | 88 | 37% | 0 | 8 | -0.83% | -0.83% | -2.18% |
| `DOGE-USDT` | 69 | 61% | 0 | 8 | -5.76% | -5.76% | -4.80% |
| `DOT-USDT` | 129 | 56% | 0 | 8 | +0.56% | +0.56% | -0.27% |
| `ENA-USDT` | 172 | 47% | 0 | 8 | +5.33% | +5.33% | +0.66% |
| `ETC-USDT` | 123 | 55% | 0 | 8 | -1.48% | -1.48% | -1.75% |
| `ETH-USDT` | 117 | 35% | 0 | 8 | -2.40% | -2.40% | -1.84% |
| `FIL-USDT` | 194 | 40% | 2 | 8 | -7.12% | -7.14% | -6.26% |
| `HYPE-USDT` | 266 | 40% | 0 | 8 | -1.80% | -1.80% | -1.48% |
| `LINK-USDT` | 124 | 48% | 0 | 8 | -2.74% | -2.74% | -1.98% |
| `LIT-USDT` | 290 | 55% | 0 | 8 | +6.81% | +6.81% | +4.10% |
| `LTC-USDT` | 127 | 37% | 0 | 8 | -0.91% | -0.91% | -1.30% |
| `NEAR-USDT` | 356 | 31% | 0 | 8 | -4.65% | -4.65% | -4.74% |
| `OKB-USDT` | 110 | 50% | 1 | 8 | -6.11% | -5.72% | -3.71% |
| `PEPE-USDT` | 199 | 41% | 0 | 8 | +2.62% | +2.62% | +0.12% |
| `PUMP-USDT` | 205 | 55% | 0 | 8 | +0.50% | +0.50% | +0.39% |
| `SOL-USDT` | 166 | 29% | 0 | 8 | -0.05% | -0.05% | -0.76% |
| `SOPH-USDT` | 146 | 65% | 0 | 8 | -1.32% | -1.32% | -2.29% |
| `SUI-USDT` | 158 | 39% | 18 | 8 | -4.30% | -4.40% | -3.60% |
| `TRUMP-USDT` | 158 | 53% | 0 | 8 | -5.50% | -5.50% | -3.45% |
| `UNI-USDT` | 207 | 57% | 0 | 8 | +4.21% | +4.21% | +0.90% |
| `USDC-USDT` | 0 | 19% | 0 | 8 | +0.00% | +0.00% | +0.00% |
| `USDG-USDT` | 0 | 23% | 0 | 8 | +0.00% | +0.00% | +0.00% |
| `WLD-USDT` | 356 | 38% | 0 | 8 | -1.00% | -1.00% | -1.58% |
| `XAUT-USDT` | 21 | 57% | 0 | 8 | -0.96% | -0.96% | -1.23% |
| `XRP-USDT` | 94 | 47% | 0 | 8 | -7.25% | -7.25% | -5.18% |
| `ZEC-USDT` | 301 | 23% | 0 | 8 | -7.55% | -7.55% | -7.33% |

## What this says

**Re-fitting works as a fix and makes the result worse.** Both are true:

- The artefact is gone. Zero-cycle pairs **14/29 → 2/29**, mean cycles
  **34.2 → 161.1**, bars outside the band **78.7% → 43.9%**. A grid that
  re-ranges every two weeks actually trades.
- Mean return **−0.402% → −1.451%**, max drawdown **1.68% → 5.29%**, segments
  stopped out **23 → 65**. Trading these pairs in this regime loses money, and
  the single-fit figure was mostly a grid doing nothing.

The honest summary is that the previous −0.39% was not a better result, it was
a quieter one. Removing the stale-band artefact removed the flattery with it.

**Bars outside the band does not go to zero** — 43.9% even with re-fitting. Half
the time, price is outside a band derived from the 45 days immediately before.
That is a statement about the regime, not about the re-fit interval: these pairs
trend, and a band drawn from the recent past is behind price almost as often as
it contains it. Shortening the interval further would be tuning, and was not
done.

**The bound still barely engages.** 24 bars at the bound out of ~72 500. It now
*can* bind and is tested to do so, but nothing here justifies it on returns. It
is there for the decline that sweeps every rung, which did not occur in this
sample.

**The gates still reject all of it.** Every pair here is run through the
backtest directly, bypassing the rubric. A live scan proposes nothing.

---

# Four-arm variant test

*2026-09-14. Verdict rule pre-registered in `docs/THEORY.md` before running.*

29 pairs, 2 500 out-of-sample hourly bars, 14-day rolling re-fit, identical
fees and execution assumptions across arms. Nothing tuned inside any variant;
each ran once at its stated default.

- **A. One fill then re-analyse** — a single buy at the bottom rung; on fill,
  the full rubric is re-run on data up to that bar and a new order is placed
  only if every gate still passes. Liquidity was passed as ample rather than
  reconstructed, so the volume gate never rejects — the generous direction for A.
- **B. Trend-exit** — normal grid, liquidated at the close (taker) when the
  short-window ADX computed on the 1 080 bars *strictly before* the bar exceeds
  25. No lookahead.
- **C. Asymmetric rung sizes** — weights `0.85^(n-1-i)` over the ladder,
  normalised to mean 1.0 so total commitment is unchanged; largest at the top.

| arm | mean return | mean max DD | cycles | stop-outs | bars outside band | pairs positive |
|---|---:|---:|---:|---:|---:|---:|
| Baseline (rolling re-fit) | **-1.413%** | 5.256% | 161.0 | 65 | 43.9% | 8/29 |
| A. One fill then re-analyse | **-0.187%** | 0.248% | 1.8 | 65 | 43.9% | 9/29 |
| B. Trend-exit on ADX>25 | **-0.189%** | 0.358% | 8.0 | 5 | 43.9% | 9/29 |
| C. Asymmetric rung sizes | **+1.824%** | 5.989% | 158.3 | 65 | 43.9% | 15/29 |

Supporting detail:

| arm | median return | best pair | worst pair | mean capital deployed | signal exits | gate blocks |
|---|---:|---:|---:|---:|---:|---:|
| Baseline (rolling re-fit) | -1.003% | +6.81% | -7.85% | 1.00x | 0 | 0 |
| A. One fill then re-analyse | -0.074% | +0.04% | -1.10% | 0.01x | 0 | 81 |
| B. Trend-exit on ADX>25 | -0.135% | +0.22% | -1.05% | 1.00x | 227 | 0 |
| C. Asymmetric rung sizes | +1.023% | +22.17% | -7.81% | 1.00x | 0 | 0 |

Per pair (return):

| pair | baseline | A | B | C |
|---|---:|---:|---:|---:|
| `ADA-USDT` | +3.09% | -0.24% | -0.52% | +9.64% |
| `ARB-USDT` | +5.24% | -0.18% | -0.05% | +18.48% |
| `BNB-USDT` | -7.85% | -0.36% | +0.19% | -5.85% |
| `BTC-USDT` | -0.83% | -0.47% | -0.17% | +2.05% |
| `DOGE-USDT` | -5.76% | -0.34% | -0.09% | -3.81% |
| `DOT-USDT` | +0.56% | -0.22% | -0.28% | +8.45% |
| `ENA-USDT` | +5.33% | +0.01% | +0.20% | +5.45% |
| `ETC-USDT` | -1.48% | -1.10% | -0.50% | +1.21% |
| `ETH-USDT` | -2.40% | -0.21% | -0.13% | +1.24% |
| `FIL-USDT` | -7.14% | -0.55% | -0.98% | -0.53% |
| `HYPE-USDT` | -1.80% | +0.01% | -0.40% | -7.81% |
| `LINK-USDT` | -2.74% | -0.11% | -0.39% | -1.55% |
| `LIT-USDT` | +6.81% | +0.00% | +0.14% | +22.17% |
| `LTC-USDT` | -0.91% | -0.41% | +0.05% | +2.31% |
| `NEAR-USDT` | -4.65% | -0.35% | +0.08% | -1.09% |
| `OKB-USDT` | -5.72% | -0.39% | +0.18% | -6.47% |
| `PEPE-USDT` | +2.62% | -0.07% | -0.15% | +2.22% |
| `PUMP-USDT` | +0.50% | +0.02% | -1.05% | +1.82% |
| `SOL-USDT` | -0.05% | +0.02% | -0.19% | -2.15% |
| `SOPH-USDT` | -1.32% | -0.05% | +0.21% | +4.01% |
| `SUI-USDT` | -4.40% | -0.02% | -0.36% | -0.81% |
| `TRUMP-USDT` | -5.50% | +0.04% | +0.16% | +1.88% |
| `UNI-USDT` | +4.21% | +0.00% | -0.50% | +10.31% |
| `USDC-USDT` | +0.00% | +0.00% | +0.00% | +0.00% |
| `USDG-USDT` | +0.00% | +0.00% | +0.00% | +0.00% |
| `WLD-USDT` | -1.00% | +0.01% | -0.36% | +1.02% |
| `XAUT-USDT` | -0.96% | +0.01% | -0.02% | -0.25% |
| `XRP-USDT` | -7.25% | -0.48% | -0.79% | -1.69% |
| `ZEC-USDT` | -7.55% | +0.01% | +0.22% | -7.37% |

## Outcome, against the rule above

Run 2026-09-14. The rule was written before the numbers existed.

| arm | mean return | mean max DD | cycles | stop-outs | bars outside band | pairs positive |
|---|---:|---:|---:|---:|---:|---:|
| Baseline (rolling re-fit) | −1.413% | 5.256% | 161.0 | 65 | 43.9% | 8/29 |
| A. One fill then re-analyse | −0.187% | 0.248% | 1.8 | 65 | 43.9% | 9/29 |
| B. Trend-exit on ADX>25 | −0.189% | 0.358% | 8.0 | 5 | 43.9% | 9/29 |
| C. Asymmetric rung sizes | **+1.824%** | 5.989% | 158.3 | 65 | 43.9% | 15/29 |

**The best of the four is positive. The retirement condition is NOT triggered,
and spot grid trading is not retired.**

That is the rule applied as written, and it is the whole of the verdict. What
follows is context, not a renegotiation of it.

### The result is weak, and saying so is not moving the bar

- 95% bootstrap CI on C's mean (20 000 resamples over pairs):
  **[−0.473%, +4.381%]** — it crosses zero.
- Sign test: **15 of 27** non-zero pairs positive. A coin flip.
- Excluding the best two pairs (LIT +22.2%, ARB +18.5%), C's mean falls to
  **+0.454%**.

None of this changes the verdict, because the rule was stated on the mean and
the mean is positive. It does mean C is **not demonstrated edge** — the correct
description is *not retired, not validated*.

### What C actually does, which is not what it looks like

Decomposing P&L per 1 000 of capital, summed across the eight segments:

| arm | realised (cycles − fees) | unrealised (inventory marked out) |
|---|---:|---:|
| baseline | **+25.14** | −38.80 |
| C | +17.29 | **+1.30** |

**C is a worse grid.** It earns less from completed cycles than the baseline
does (+17.29 against +25.14). Its entire advantage is the second column: it
ends segments holding a smaller, better-priced position, so the inventory mark
does not destroy the trading profit the way the flat ladder's does.

That is exactly the mechanism predicted when the variant was specified — a
trend down accumulates less inventory at worse prices. It is confirmed. But it
is an **inventory-exposure effect, not a trading edge**, and it is directional:
front-loading holds less of a rising asset too. In a sample where most pairs
fell, that is a tailwind, and it would be a headwind in the opposite sample.
The 29-pair mean is not evidence about which regime comes next.

### A and B: both are "do almost nothing" strategies

A and B look better than baseline only because they barely trade.

- **A** deploys on average **0.01x** of available capital. Its −0.187% on total
  capital is roughly **−19% on the capital it actually used**. It was blocked by
  the gates 81 times across the universe — the re-analysis almost always fails,
  which is consistent with every live scan proposing nothing.
- **B** signalled an exit **227 times** across 29 pairs and 8 segments — very
  nearly every segment. Short-window ADX is above 25 almost all the time in this
  sample, so B liquidates early and sits in cash. It cut stop-outs from 65 to 5,
  which is real, but it achieves that by not being in the market.

Both have 95% CIs entirely **below** zero (A: [−0.287%, −0.103%], B: [−0.319%,
−0.069%]). They are reliably, slightly negative.

### The pre-committed prediction was wrong

I predicted A would be worse than baseline. On the headline number it was not —
−0.187% against −1.413%. The prediction was wrong because I was reasoning about
trading behaviour and the number is dominated by position sizing: A trades
almost never, so its return is near zero from either direction. On
return-per-unit-of-deployed-capital, the prediction was right. Recording the
miss rather than reinterpreting it into a hit.

### What did not change across any arm

`bars outside band` is **43.9%** in all four arms, because all four use the same
band and the same 14-day re-fit schedule. None of the variants addresses band
staleness; they change what happens inside the band.

## Outcome of the rising-window test

Run 2026-09-14, after the rule and the selection procedure above.

### Window selected

The procedure ran over eleven candidate blocks and took the first qualifying
one, `k = 4` — bars `[N-12500, N-10000)`. **22 of 29** pairs were eligible;
the seven excluded (ENA, HYPE, LIT, PUMP, SOPH, USDG, ZEC) had not been listed
long enough to fit a band before the block. **Median buy-and-hold across the
22: +45.66%.** Blocks `k = 2, 3, 5` had medians of −41.29%, −30.10% and −42.75%
and were skipped because the rule asked for a rising window and takes the first
qualifying block, not the best one.

### Results

Both arms, both windows, same 22 pairs. Realised and unrealised are per 1 000
of capital, summed across segments.

| window | arm | mean return | realised | unrealised | cycles | stop-outs | pairs positive | 95% CI | drop-top-2 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| rising | baseline | −0.660% | **+33.30** | −39.13 | 220.8 | 26 | 7/22 | [−1.41%, +0.13%] | −0.753% |
| rising | C | **+0.396%** | +24.32 | −19.18 | 216.0 | 26 | 9/22 | [−1.46%, +2.41%] | +0.000% |
| falling | baseline | −1.117% | **+23.71** | −34.24 | 166.9 | 48 | 5/22 | [−2.77%, +0.92%] | −1.631% |
| falling | C | **+1.424%** | +18.82 | −3.64 | 163.9 | 48 | 11/22 | [−1.12%, +4.30%] | +0.569% |

C − baseline: **+1.056%** return and **−8.99** realised in the rising window;
**+2.541%** return and **−4.89** realised in the falling window.

### Verdict: RETIRED, via the third branch

Walking the rule as written:

1. *Does C underperform baseline in the rising window?* **No.** It outperformed,
   +0.396% against −0.660%, and beat baseline on 14 of 22 pairs. The
   pre-committed prediction was **wrong**.
2. *Does C beat baseline in both windows **with the realised column also
   improving**?* **No.** C beats baseline in both, but its realised P&L is
   **worse in both** — −8.99 rising, −4.89 falling. The second branch requires
   both conditions and only one is met.
3. Therefore **anything else**, which retires it.

**Spot grid trading is retired. No further variants will be tested.**

The pre-registration anticipated this shape explicitly: "the surviving branch
requires C to win on realised P&L, which it did not do in the falling window."
It did not do so in the rising window either. C is not a better grid in either
direction — it completes fewer profitable cycles and earns less from them. It
is a way of holding less inventory, and holding less inventory is available
directly, without a grid.

### Two corrections to what I said before

1. **The first window was never a falling market.** I described it as one, and
   the whole "directional artefact" hypothesis was framed around that. Median
   buy-and-hold across the 29 pairs over that window is **+8.86%**, with only
   **12 of 29** pairs down. It was a mildly rising market. My earlier sentence,
   "in a sample where most pairs fell, that is a tailwind", was wrong on its
   facts.
2. Consequently this test compared **mildly rising (+8.86%) against strongly
   rising (+45.66%)**, not falling against rising. A genuinely falling window
   existed in the candidate set (`k = 2`, median −41.29%, 26 pairs eligible)
   and was **not** run, because the rule had already resolved and running more
   windows after seeing the answer to reach a different branch is precisely the
   cherry-picking the pre-registration exists to prevent.

The verdict survives that limitation: the retirement is driven by the realised
column, which is worse for C in **both** windows actually tested, and that is a
statement about its trading rather than about market direction.

### Also worth recording: neither arm clears zero honestly

Baseline is negative in both windows. C's confidence intervals both **cross
zero** ([−1.46%, +2.41%] and [−1.12%, +4.30%]), and dropping its best two pairs
takes the rising window to exactly **+0.000%**. Even the arm that triggered the
"not retired" branch in the first test does not survive contact with a second
window as anything distinguishable from noise.
