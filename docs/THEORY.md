# Why the sizing looks like this

Reasoning and citations for the inventory, volatility and statistics changes.
Written as they were made, so the argument and the code do not drift apart.

---

## 1. The asymmetry that motivates all of it

A grid is a naive market maker. It earns the spread on each round trip and
carries inventory between them. Those two things scale differently:

| | scales as |
|---|---|
| spread revenue | **linear** in the number of fills |
| inventory risk cost | **quadratic** in the position, ≈ ½·γ·q²·σ² |

A line and a parabola cross exactly once. Below the crossing point each extra
fill is worth taking; above it the spread collected is smaller than the risk
assumed, and every further fill has negative expected value.

Placing identical size at every rung sets γ = 0 — risk-neutral with unlimited
capital. That is a real modelling assumption, and not one anybody would make
deliberately.

The classic treatments:

- **Avellaneda, M. & Stoikov, S. (2008).** "High-frequency trading in a limit
  order book." *Quantitative Finance* 8(3), 217–224. Derives optimal bid/ask
  quotes for a market maker with inventory aversion; the reservation price
  shifts away from mid in proportion to inventory.
- **Guéant, O., Lehalle, C.-A. & Fernandez-Tapia, J. (2013).** "Dealing with
  the inventory risk: a solution to the market making problem." *Mathematics
  and Financial Economics* 7(4), 477–507. Gives closed-form approximations and
  — the part used here — the **boundary result**: at maximum inventory the
  optimal quote on the side that would increase it is no quote at all.
- **Ho, T. & Stoll, H. (1981).** "Optimal dealer pricing under transactions and
  return uncertainty." *Journal of Financial Economics* 9(1), 47–73. The
  original inventory-control dealer model.

---

## 2. The hard bound — `inventory.max_inventory_for`, `Q_max`

**Implemented and ON by default.** At `q ≥ Q_max` the buy side is not quoted,
and stays unquoted until inventory falls.

This is the GLFT boundary result applied literally. It is the highest-value
change in this set because it is not an optimisation — it is the thing that
prevents the ruinous case. A grid in a sustained decline buys every rung on the
way down and ends holding its largest position at its worst average price; the
bound is what stops that being unbounded.

**Measured** on a straight decline through every rung (arithmetic ladder,
94–106, 13 rungs):

| | peak inventory | bars refusing to buy |
|---|---:|---:|
| unbounded | 12.07 units | 0 |
| Q_max = 5 | 5.86 units | 6 |
| Q_max = 5 + skew | 4.66 units | 0 |

**One caveat:** the bound stops *initiating* past Q_max. A fill already
in flight cannot be partially refused, so inventory can end up to one lot above
the bound — 5.86 against a bound of 5. Tested explicitly
(`test_the_overshoot_is_at_most_one_lot`).

**A second, more important one:** across the 29-pair universe the bound alone
changed almost nothing (variance lower on 2/29 pairs). With Q_max set at half
the implied maximum it simply did not bind under ordinary conditions. That is
the expected shape of insurance — it does nothing until the day it does
everything — but it means the bound should not be justified by backtest
improvement, and it is not.

---

## 3. Size skewing — `inventory.buy_multiplier` / `sell_multiplier`

**Implemented and OFF by default.**

Avellaneda-Stoikov skews the *price* of quotes toward unwinding inventory. A
grid cannot: its prices are a fixed lattice chosen in advance. The lattice
equivalent is to modulate **size** at those fixed prices:

```
buy_size_i  = base × max(0, 1 − q/Q_max)
sell_size_j = base × min(2, 1 + q/Q_max)
```

Linear in q, mean-reverts inventory toward zero, and reaches exactly zero on
the buy side at `q = Q_max`, so it subsumes the hard bound rather than needing
a separate check. The sell multiplier is capped at 2× because the formula is
unbounded above if `Q_max` is ever misconfigured small.

**CORRECTION (2026-09-11).** Two claims made in the previous revision of this
section were wrong, and both are retracted here:

1. It said the original A/B table "was fitted on bands that spanned the whole
   in-sample stretch". **False.** The original harness fitted its band on
   `closes[:1500]` — 1 500 bars, 62 days. It was already a short window. The
   wide-band defect was real, but it was in `walk_forward`, not in that table.
2. It said the original table "showing bound != fixed cannot have been right".
   **False.** The original used `fraction=0.5` for its bound arm, which does
   engage (73 bars across the universe). The arm that could not differ was
   *mine*, which used `fraction=1.0`.

Re-running the original harness verbatim today reproduces it: **-0.911%**
against the **-0.888%** originally recorded, the difference being candle-cache
top-up since. See `backtest_results.md` for the full reconciliation.

---

### Q_max, corrected so that it binds

The bound was derived as `max_quote_total / lower`, which assumes every rung
fills at the **bottom** of the range. Every rung above the bottom produces
fewer units than that, so the real maximum is strictly smaller and the bound
sat above anything the grid could reach. Measured: `bars_at_bound` was **zero**
on all 29 pairs across 2 500 bars.

It is now the real maximum:

```
max_inventory = sum(quote_per_level / price_i)   over all buy levels
Q_max         = BOUND_FRACTION * max_inventory   (BOUND_FRACTION = 0.6)
```

`BOUND_FRACTION` is 0.6 rather than 1.0 deliberately. At 1.0 the bound is
reachable only when every rung has filled — it engages after the grid is fully
loaded, which is too late to be the thing that stops the left tail.

`test_bars_at_bound_is_positive_on_a_straight_decline` now fails if the bound
stops engaging, and `test_the_old_formula_would_fail_this_test` pins the defect
so it cannot return quietly.

**Which half of the change makes it bind?** The fraction, mostly. Reverting the
formula to `max_quote_total / lower` while keeping `BOUND_FRACTION = 0.6` still
engages on a straight decline — 0.6 of an overstated maximum is reachable.
Reverting `BOUND_FRACTION` to 1.0 stops it engaging outright. So the formula
change makes `Q_max` *correct*; the fraction is what makes it *bind*. Both are
tested separately.

**It still binds rarely on real data** — 24 bars out of ~72 500 across the
universe with rolling re-fits. That is the expected shape of insurance, and it
is why the bound is not justified by backtest improvement. But it is now
attached to something: on a straight decline through every rung it engages, and
before this change it did not.

### Measured, with rolling re-fit

29 pairs, 2 500 out-of-sample hourly bars, re-fit every 14 days, Q_max at
`BOUND_FRACTION` of the real maximum:

| variant | mean return | mean max DD | mean P&L σ | mean cycles | 0-cycle | bars at bound |
|---|---:|---:|---:|---:|---:|---:|
| fixed size | -1.451% | 5.294% | 0.00088 | 161.1 | 2/29 | 0 |
| hard bound | -1.413% | 5.256% | 0.00087 | 161.0 | 2/29 | 24 |
| bound + skew | **-1.984%** | **3.745%** | **0.00053** | 157.3 | 2/29 | 0 |

Against fixed size, bound + skew gives **-0.53pp return, -1.55pp max drawdown,
-39.8% P&L standard deviation**.

That is the predicted trade: slightly lower mean return, substantially lower
variance. It is off by default because it changes every fill size and the
benefit is a variance reduction that should be verified on my pairs, not
assumed — `backtest.run(size_skew=True)` A/Bs it.

---

## 4. Volatility-scaled spacing — `volatility.py`

**Implemented; the fee floor always wins.**

Fixed spacing is a bet that volatility is constant. It is not, and the
forecastability asymmetry is the one robust effect in this literature:
realised volatility is strongly autocorrelated and forecastable out of sample,
while expected returns at these horizons are not forecastable to any useful
degree. The brief cites out-of-sample R² of 43–47% for volatility against under
0.5% for returns; I have not independently verified those particular figures
and record them as the brief's, but the direction and rough magnitude of the
gap is not controversial.

That asymmetry is also the boundary of the module:

> **Used for sizing and spacing only. Never as a directional signal.**

A volatility forecast says how far, not which way. Nothing in `volatility.py`
returns a direction, and `rubric` never consults it to decide whether an asset
is worth trading — only how wide to space a ladder on one that already passed.

- **Corsi, F. (2009).** "A Simple Approximate Long-Memory Model of Realized
  Volatility." *Journal of Financial Econometrics* 7(2), 174–196. HAR-RV, the
  default here: regress realised volatility on its own daily, weekly and
  monthly averages. Three OLS coefficients, captures long memory without a
  long-memory model, and degrades gracefully on short samples.
- **Bollerslev, T. (1986).** "Generalized autoregressive conditional
  heteroskedasticity." *Journal of Econometrics* 31(3), 307–327. GARCH(1,1),
  offered via `arch` as the alternative.

**Measured** on real OKX hourly data, the forecast wants tighter spacing than
the fees allow on most pairs, and the floor correctly overrides:

```
ADA-USDT  HAR σ 1.1022%  baseline 0.9188%  ratio 1.20  →  spacing 0.7720%
BTC-USDT  HAR σ 0.3402%  baseline 0.4274%  ratio 0.80  →  spacing 0.6436% (floor)
```

Scaling is clamped to [0.5×, 3.0×]: a forecast asking to quadruple the grid
interval is more likely a broken fit than a real regime.

---

## 5. Hurst demoted — `rubric.assess`

**Removed from the hard gates. Still measured, still reported.**

Hurst was rejecting 24 of 28 candidates in live scans, which made it the
binding constraint on the entire system. It is also the least-supported
statistic in the framework:

- the estimate is acutely sensitive to the lag range, and nothing here or in
  the literature justifies the particular range chosen;
- the same series can read 0.43 or 0.67 under defensible alternative choices;
- no forward-predictive validation of it exists in this codebase.

**Lo, A. W. (1991).** "Long-Term Memory in Stock Market Prices."
*Econometrica* 59(5), 1279–1313, makes the closely-related point that classical
R/S statistics confound long-range dependence with short-range dependence, and
that apparent long memory largely disappears under a corrected statistic.

ADF is the primary gate instead — it tests a hypothesis, reports an error rate,
and is what "mean-reverting" actually means:

- **Dickey, D. A. & Fuller, W. A. (1979).** "Distribution of the Estimators for
  Autoregressive Time Series with a Unit Root." *JASA* 74(366), 427–431.

Hurst's verdict is still computed and stored in `Assessment.hurst_would_reject`
and surfaced in the notes, so the two can be compared on real scans rather than
argued about. **This is a relaxation of a safety gate.** I made it on purpose, for the
reasons above, and I record it here because a loosened gate should never be
silent.

---

## 6. Markout instrumentation — `markout.py`

**Implemented; measures rather than assumes.**

Sign-adjusted markout: positive always means the fill was favourable.

```
buy  markout(h) = (mid(t+h) − fill) / fill
sell markout(h) = (fill − mid(t+h)) / fill
```

Segmented by time-to-fill, to test the practitioner claim that fast fills are
toxic (adverse selection) and slow fills favourable (genuine liquidity
provision). The brief cites −0.31 and +0.43 fill quality respectively; those
are recorded as the brief's figures.

**Measured** over 2 670 backtest fills across 8 pairs:

| bucket | n | +1h | +4h | +24h |
|---|---:|---:|---:|---:|
| fast (<1m) | 280 | −0.97% | −0.87% | **−0.13%** |
| slow (1–6h) | 524 | −0.83% | −1.01% | −2.52% |
| very slow (>6h) | 1 866 | −0.77% | −0.98% | −1.00% |

**The claim is not confirmed on these pairs.** Fast fills markout *better* at
+24h (−0.13%) than slow ones (−1.76%) — the opposite of the prediction. Every
bucket is negative at every horizon, which says the grid was buying into
declines throughout this window; that is consistent with the trending regime
every other measurement here has found, and is not evidence about adverse
selection either way.

**Two limits worth stating plainly.** These are *backtest* fills, where the
"mid" is the bar close and there is no order book, so this measures price drift
after a fill rather than adverse selection proper. And at hourly resolution the
+1s, +10s, +1m, +5m and +30m horizons are **not observable at all** — there is
no price between one candle and the next. They are omitted rather than
interpolated. An early version of this code silently answered "+1s" with the
next bar's close an hour later, making five horizons report an identical number
and look like a measured curve; the tolerance rule that fixed it is in
`measure()`.

---

## 7. Lookback audit — equity convention vs crypto

Crypto's momentum-to-reversal crossover is roughly **one month**, against
roughly **one year** in equities, so windows inherited from equity convention
are systematically too long.

| Parameter | Where | Value | Origin | Crypto-appropriate | Status |
|---|---|---:|---|---|---|
| `atr_pct` period | `metrics.py` | 14 | Wilder (1978), daily equities/futures | 14 **hourly bars** ≈ 14h — already short in absolute terms | keep; the unit saves it |
| `adx` period | `metrics.py` | 14 | Wilder (1978), same | as above | keep |
| `hurst` `max_lag` | `metrics.py` | 40 | convention, unjustified | n/a — this is the sensitivity that demoted it | demoted, §5 |
| `variance_ratio` k | `metrics.py` | 10 | Lo–MacKinlay, monthly equities | 10 hourly bars is fine; it is a note, not a gate | keep |
| `stability_window` | `rubric.py` | 150 bars ≈ 6 days | chosen here | ~1 month = 720h is the crossover; 150 is *shorter*, which is the safe side | keep, documented |
| `suggest_range` half-life | `metrics.py` | window/4 | chosen here | adaptive; at 300 bars ≈ 3 days | keep |
| scan window | `analyst.py` | `CANDLES = 300` ≈ 12 days | OKX page size, not a decision | **~720 bars (1 month)** to match the crossover | ⚠️ **unchanged — see below** |
| HAR daily/weekly/monthly | `volatility.py` | 24 / 168 / 720 bars | Corsi (2009), rescaled to hourly | exactly the crossover horizon at the monthly leg | correct by construction |
| fee tier TTL | `analyst.py` | 24h | VIP tiers move on 30-day volume | 24h is conservative | keep |
| instrument TTL | `okx.py` | 24h | relistings are rare | keep | keep |
| heartbeat staleness | `executor.py` | 300s | operational, not statistical | keep | keep |

**The one that is wrong is the scan window.** `CANDLES = 300` is the OKX
endpoint's page size, not a considered choice, and 12 days is well under the
one-month crossover. Every live scan has been measuring a fortnight. Changing
it to ~720 bars would align it with the crossover horizon, but it also changes
what the live system proposes and interacts with the already-recorded problem
that `suggest_range` over long windows produces bands no grid can trade
(a 66–126 band on LTC over two years, 0 in-sample cycles). I have **not**
changed it: it needs the range-fitting problem solved first, and that is a
decision about what gets proposed rather than a cleanup.

---

## What is still assumed rather than measured

- γ, the inventory-aversion coefficient, is never estimated. Skewing uses
  `q/Q_max` as a proxy for it, which is the linear approximation, not the
  Avellaneda-Stoikov exponential.
- No order book is modelled anywhere. Fill probability is "the close crossed
  the level", not a queue position.
- Markout is measured against bar closes, not a real mid, and therefore
  measures drift rather than adverse selection.
- The `LiveExchange` client does not exist, so none of this has been observed
  against a real venue.

---

# Verdict rule for the variant test

**Written 2026-09-14, BEFORE the variants were run.** Recorded here first so the
stopping rule cannot be renegotiated once the numbers are visible. This is the
same discipline as the ledger's kill criteria: set in advance, honoured without
renegotiation.

## What is being tested

Four arms, on the same 29 pairs, the same 2 500 out-of-sample hourly bars, the
same fees, the same execution assumptions, and the same rolling re-fit schedule:

- **Baseline** — rolling re-fit every 14 days, flat ladder, fixed size.
- **A. One fill then re-analyse** — a single buy at the bottom rung; on fill,
  re-run the full rubric on data up to that bar and only continue if every gate
  still passes.
- **B. Trend-exit** — normal grid, but liquidate and stop when the short-window
  ADX crosses above 25, rather than waiting for the price stop.
- **C. Asymmetric rung sizes** — front-loaded ladder, largest size at the top
  rungs decreasing geometrically toward the bottom, total capital unchanged.

## The rule

> **If the best of the four arms has a negative mean return, spot grid trading
> is retired and no further variants are tested.**

Retired means: no further variant is proposed, designed, or measured. The
gates stay where they are, the analyst keeps proposing nothing, and the
conclusion recorded is that this strategy has no demonstrated edge on this
universe in this regime.

## What does NOT count as passing

Stated in advance, because each of these is a way to talk a negative result into
a positive one after seeing it:

- A single pair being positive. The rule is on the **mean across 29 pairs**.
- A variant being "less negative" than baseline. Less negative is still negative.
- Lower drawdown or lower variance with a negative return. Reduced variance
  around a loss is a cheaper loss, not an edge.
- Excluding pairs that did badly, or re-running on a different universe, window,
  or re-fit interval to find a combination that clears zero.
- Tuning any parameter inside any variant. Each is run once, at its stated
  default.

## Why this rule and not a higher bar

Zero is already a generous threshold. It ignores the cost of capital, the
opportunity cost of the capital sitting in the grid, and the tail risk being
carried — a strategy that merely breaks even before those is losing after them.
A rule at zero is the weakest defensible one, which is the point: if the
strategy cannot clear even this, there is nothing to salvage by arguing about
where the bar should sit.

## Pre-committed prediction

For the record, before running: **A is expected to be worse than baseline.**
The rolling re-fit already degraded results by making the grid trade more in a
trending market, and A is the maximally adaptive form of the same idea. B and C
are both defensive and are expected to reduce drawdown; whether either clears
zero is genuinely unknown.

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

---

# Rising-window test of variant C

**Written 2026-09-14, BEFORE the window was selected and before either arm was
run.** Recorded first so neither the window nor the verdict can be chosen after
seeing the result.

## The hypothesis under test

C's own P&L decomposition says its advantage is not better trading. It earns
**less** from completed cycles than the baseline (+17.29 against +25.14 per
1 000, over eight segments) and wins only on the inventory mark — it ends
segments holding a smaller, better-priced position.

If that is the mechanism, it is directional: front-loading holds less of a
**rising** asset too. So the explanation makes a falsifiable prediction:

> **In a window where most pairs rose, C should UNDERPERFORM the baseline.**

## Window selection procedure, fixed in advance

The selection must not reference C's performance. It references only price
direction, and it takes the first window that qualifies rather than the best one.

1. Candidate windows are **non-overlapping 2 500-bar blocks** stepping backwards
   from the block already used. With `N` bars of history per pair, block `k`
   covers `[N - 2500*(k+1), N - 2500*k)`. Block `k = 0` is the window already
   tested and is excluded as it must be disjoint.
2. A pair is eligible for a block only if it has at least **1 080 bars before
   the block starts**, since the band is fitted on the trailing 45 days.
   Ineligible pairs are excluded and the exclusions are reported — the universe
   will be smaller than 29 and that is a real difference from the first test.
3. For each block, compute each eligible pair's **buy-and-hold return** across
   the block, and take the **median across pairs**.
4. Take the **smallest `k` whose median is positive** — the most recent
   qualifying window. Smallest-k, not largest-median: picking the most
   favourable block in deep history is exactly the cherry-pick this rule exists
   to prevent.
5. If no block qualifies, that is reported as such and nothing is run.

Everything else is held identical to the first test: same 14-day re-fit, same
fees, same execution assumptions, same `0.85` front-loading ratio, no tuning.

## The rule

- **If C underperforms baseline in the rising window**, the inventory-exposure
  explanation is confirmed. C is a short bias, not a strategy. **Grid trading is
  retired and no further variants are tested.**
- **If C beats baseline in BOTH the falling and the rising window, and the
  realised column also improves**, something real may be there and it earns a
  proper multi-window walk-forward.
- **Anything else is inconclusive and also retires it.** A strategy that only
  works in one direction of market is a directional bet, and a directional bet
  is better expressed directly than through a grid.

Note the asymmetry: two of the three branches retire the strategy, and the
surviving branch requires C to win on **realised** P&L, which it did not do in
the falling window. This is deliberate. The bar is high because the prior is
that there is nothing here.

## Pre-committed prediction

C underperforms baseline in the rising window, and the strategy is retired. The
decomposition already showed C trades worse; the only open question is whether
the inventory term flips sign with the market, and there is no mechanism by
which it would not.

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

### Also worth recording: neither arm clearly beats zero

Baseline is negative in both windows. C's confidence intervals both **cross
zero** ([−1.46%, +2.41%] and [−1.12%, +4.30%]), and dropping its best two pairs
takes the rising window to exactly **+0.000%**. Even the arm that triggered the
"not retired" branch in the first test does not survive contact with a second
window as anything distinguishable from noise.
