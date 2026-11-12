# Grid Pilot

**There is no AI in this system.** Not in the analyst, not in the executor, not
anywhere at runtime. Every number in a proposal comes from `metrics.py` and is
tested by `rubric.py`; the approval note is a template over those same numbers.
No API key, no model call, no network dependency beyond OKX's public endpoints.

There used to be a model call that wrote the approval note. It was removed: it
could only ever rephrase measurements that already existed, and a paragraph of
fluent prose attached to a trading decision invites you to read the prose
instead of the numbers.

A spot grid-trading system in two halves that never mix:

- **The analyst** (`src/grid/rubric.py`, `analyst.py`) — runs when you ask it to,
  measures which instruments oscillate without going anywhere, and proposes grids.
  It measures. It never trades.
- **The executor** (`src/grid/executor.py`) — takes an approved plan and applies it
  forever: buy at each level down, sell at each level up, stop if the range breaks.
  It has **no judgement and no discretion**. It reads parameters and obeys them.

Between them sits you: every proposal needs your approval, with your chosen amount,
through the Grid Pilot app.

```
   OKX candles ──► analyst ──► proposal ──► YOU ──► approved plan ──► executor ──► OKX
                 (measures)               (decide)                   (obeys)
```

---

## What is built and tested

| Module | What it does | Tests |
|---|---|---|
| `grid/fees.py` | Fee model; the break-even spacing formula | 23 |
| `grid/metrics.py` | Hurst, ADX, efficiency ratio, ATR, range quality | — |
| `grid/rubric.py` | Selection gates + fit score + the analyst's brief | 34 combined |
| `grid/plan.py` | The command format between analyst and executor | — |
| `grid/control.py` | Three kill switches, fail-closed | 42 combined |
| `ledger/` | The earlier news-prediction journal — separate, still works | 47 |

**1269 tests, all passing.** Run `python -m pytest`.

## What is not built yet

`grid/okx.py`, `grid/backtest.py`, `grid/analyst.py`, `grid/sync.py`,
`grid/executor.py`. The build plan covers these in order.

---

## The rules this system will not let you break

1. **Spot only.** No margin, no futures. That is where funding interest lives
   (~11%/yr) and it would eat the grid alive. Enforced in
   `plan.spot_instrument_problems()` via `plan.validate()`, so it is re-checked
   on every executor pass, not just at approval. An id check cannot detect
   margin — OKX margin shares the spot instrument id — so the live client must
   send `tdMode="cash"`.
2. **Limit orders only.** Maker fee, not taker, and a grid rests orders anyway.
   The executor must never cross the spread.

   The rates are **read from your account**, not hardcoded —
   `okx.get_trade_fee()` needs a READ-ONLY API key. If it cannot be read the
   system falls back to the **pessimistic** EEA tier (0.200% / 0.350%), never
   the optimistic one: an unavailable fee rate must not make a grid look more
   viable than it is. On that tier the minimum viable spacing goes from 0.64%
   to 1.62%, and the ATR floor moves with it. Every plan and every backtest
   records the rates it assumed.
3. **Minimum spacing ~0.8%.** Below that fees take more than a quarter of gross.
   Enforced in `plan.validate()` — a plan that fails cannot run even if approved.
4. **Every plan has a stop**, placed at
   `lower - max(3 x ATR, 0.20 x (upper - lower))`. Two terms because either
   alone fails: three ATRs alone sits inside ordinary noise on a quiet asset,
   a fixed fraction of the range alone ignores how violent the asset is.
   A grid without a stop accumulates losses through a breakout until the
   capital is gone. A 20% drawdown needs ~31 clean cycles to recover at 0.8%
   spacing.

9. **Position size assumes the worst case, which is a stop-out.** A grid does
   not hold one position — it accumulates all the way down and is holding its
   largest position at its worst average price exactly when the stop fires. So
   size against the average entry, not the range bottom:
   `quote = available x risk / ((avg_entry - stop) / avg_entry)` where
   `avg_entry = (upper + stop) / 2`. Sizing off `lower` understated the loss by
   about half and turned a "2% risk" into 4.25% — measured, not estimated.
5. **Volatile is not the criterion.** The asset must move a lot AND go nowhere.
   Hurst < 0.45, ADX < 25, efficiency ratio < 0.30.
6. **The range comes from where price has traded**, never from the return you want.
7. **Paper by default.** Live trading requires `confirm="I ACCEPT THE RISK"`.
8. **Fail closed.** An unreadable control file halts everything.

## Data freshness

Every source is stamped **at the fetch**, never inferred from a file mtime — an
mtime moves when bytes are rewritten, copied or restored, none of which makes
the data newer.

- **Candles are a hard gate**: nothing older than `max_data_age` (1 hour) can
  produce a proposal, measured from the bar's own close time rather than from
  when it was fetched.
- **Instrument state** must be `live`. Suspended, pre-open, expired and test
  instruments are rejected, as is a pair that has vanished from the list.
- **Fee tier** refreshes daily and is never reused past 7 days; a failed fetch
  falls back to the **pessimistic** EEA rate, and the rate used is recorded on
  every plan and every backtest.
- `grid freshness` prints every source, its age and whether it is in tolerance.
  The same chips appear in the web header.

## The interface

`grid web` — one file, inline CSS, no framework, mobile-first. It is a **window
onto state the executor owns**: it reads and writes `data/grid.db` and the plan
files, and it has no exchange client and no path to one. Closing the browser
changes nothing. There is a test asserting that structurally (`web.py` calls no
`place_limit`, `market_sell`, `run_once` or `make_exchange`).

- **Home** — a grand total, then PAPER and LIVE as visually distinct sections
  with different backgrounds, borders and permanent uppercase labels. Real
  money must never be mistaken for fake at a glance on a phone. Each shows
  invested, current value, realised and unrealised P&L, fees, and
  **buy-and-hold over the same period**, because a grid that made 2% while the
  asset rose 40% did not work.
- **Proposals** carry their confidence and three buttons: Paper, Reject, and
  Live. Live is disabled with its reason shown, and the server refuses a live
  approval independently — a disabled button is a suggestion.
- **Grid detail** (`/grid/<id>`, or `grid grid <id>`) is a bank statement:
  every transaction with timestamp, side, price, quantity, value, fee, and the
  running position and running P&L after each one, newest first. Above it the
  parameters, the ladder with filled rungs marked, where price is now, and
  where the stop sits.
- **Notifications** persist in the database, with an unread count in the
  header and mark-as-read. Nothing is ever deleted — this is the record.
- **Live updating**: `GET /api/state` every 3 seconds, a "last updated" stamp
  so a stalled poll is visible, and an immediate fetch when the tab regains
  focus.
- **Hypothetical sizes** on paper grids: 50 / 200 / 500 / 2000 re-derives every
  figure from the same fills, and flags the levels the exchange minimum would
  have blocked at that size.

### Why there is now a database

Everything else persists as JSON files, deliberately — a plan is a file you can
read and edit, and the executor rebuilds from the plan plus the exchange rather
than from a database. That has not changed. But an append-only transaction
ledger, unread notification state, and the executor writing while the web reads
are not file-shaped, so `data/grid.db` is SQLite in WAL mode. It is a record of
what happened, never a cache of what is on the exchange.

## Keeping the executor up (macOS launchd)

The executor enforces the only stop-loss this system has, so it needs to be
running whenever a grid is. A launchd **agent** supervises it.

```bash
grid service print       # show the plist without writing anything
grid service install     # write ~/Library/LaunchAgents/com.gridpilot.executor.plist
grid service status      # is it installed? loaded? actually running?
grid service uninstall   # unload and remove
```

`install` writes the file and stops there — **loading starts the executor,
which starts placing orders**, so that is a separate deliberate step:

```bash
launchctl load -w ~/Library/LaunchAgents/com.gridpilot.executor.plist
launchctl unload  ~/Library/LaunchAgents/com.gridpilot.executor.plist
launchctl list com.gridpilot.executor        # raw launchd view
```

**An agent, not a daemon, on purpose.** A LaunchDaemon runs at boot as root,
before login. Two problems with that here: the login keychain is still locked,
so API credentials are unreadable and the executor would run in a degraded
state that looks identical to a healthy one; and nobody is awake to notice it
misbehaving. `RunAtLoad` on an agent means "when you log in", which is when you
are present to watch trading start.

**`KeepAlive = { SuccessfulExit = false }`** restarts a crash and respects a
clean stop. Plain `KeepAlive = true` would relaunch the process every time you
deliberately stopped it.

**Logs** go to `data/logs/executor.out.log` and `.err.log`. launchd holds those
files open and does not rotate them, so renaming would leave launchd writing to
the old inode and the live log permanently empty. Rotation is therefore
copy-then-truncate — the inode survives, the handle stays valid — driven from
the executor loop, which ticks every 60 seconds. Five generations are kept.

## Regime drift on running grids

Every other gate fires once, at proposal time. `plan.stop_below` catches a fast
break; nothing caught a **slow** one, so a grid could drift into a trending
market and bleed for weeks without ever touching its stop.

The executor now re-runs the evidence daily on each live grid, using **only
bars since approval**, and alerts when the thesis has decayed — ADF no longer
significant, Hurst over the gate, ADX over 25, or more than 30% of bars outside
the band. It sends a before/after comparison.

**It never closes anything.** Stopping is your decision, the same principle as
`halt_all` cancelling orders without liquidating. `grid drift` runs the check
by hand.

## Sizing modes

`config.yaml: grid.mode` is `kelly` (default) or `calibration`.

**Kelly** sizes for growth. Note that Kelly's `f*` is the fraction of capital
**at risk**, not the amount deployed: a grid stop-out loses `loss_fraction`
(~10%) of what is deployed, not all of it, so

    capital_at_risk = quarter_kelly x available
    deployed        = capital_at_risk / loss_fraction

Deploying `f* x capital` risked a tenth of the intended bet — a real 10x
under-sizing bug, fixed and regression-tested.

**Calibration** is for very small capital, where P&L is noise and execution
quality is the signal. It sizes by feasibility — `quote_per_level = minSz x 1.5`,
levels capped by both the range and the capital — labels every proposal
"CALIBRATION RUN — sized to be runnable, not optimal", and still shows what
Kelly would have said including when Kelly says do not trade. It will not hand
back to Kelly sizing until **50 fills** are recorded; `grid execution` and the
web panel show progress toward that.

## Seeing what you are not seeing

Every candidate is scored, including ones a gate rejected, because zero
proposals with a best score of 0.48 is a different result from zero with a best
of 0.05. `grid scan --show-all` prints the lot and every run writes
`scan_results.md`.

Every scan and backtest prints the actual date range and bar count used, and
says loudly when the window is short.

## Two windows, not one

An asset's **character** and its **band** are different questions and are
measured over deliberately different stretches of history.

| | window | answers | used by |
|---|---|---|---|
| **statistics** | 365 days | is this asset mean-reverting *by character*? | ADF, Hurst, ADX, efficiency ratio, rolling stability, HAR volatility |
| **range** | 45 days | where is price *now*? | `suggest_range`, containment, touches, ATR |

A grid trades the band it is in today. Drawing that band from a year of data
gives the union of every regime in the year — on LTC, a 66–126 band whose upper
edge is nearly double its lower, which no ladder can trade. Across the 29-pair
universe the old behaviour produced bands averaging **190% of their own lower
bound across 152 levels**; the corrected ones average **27% across 34 levels**.

Both windows, with dates and bar counts, are printed on every proposal, every
backtest row and every `scan_results.md` entry. Backtest rows also carry a
**cycles** column, because a band no grid can trade shows up as near-zero
cycles while its *return* column still looks respectable — see
`backtest_results.md`.

## RETIRED

**Spot grid trading is retired as of 2026-09-14.** No further variants will be
tested. The rule that retired it was written to `docs/THEORY.md` before the
test was run, and the decision followed it as written.

The last candidate, variant C (front-loaded rung sizes), beat the baseline in
both windows tested — but on **return**, not on **trading**. Its realised P&L
(completed cycles minus fees) was worse than baseline in both: −8.99 in the
rising window and −4.89 in the falling one, per 1 000 of capital. C wins only
by ending segments holding less inventory. That is available directly and does
not need a grid. The surviving branch of the rule required the realised column
to improve; it did not, so the third branch applied.

Both arms' confidence intervals cross zero, and dropping C's best two pairs
takes its rising-window mean to +0.000%.

**The code is not deleted and the gates are unchanged.** The analyst keeps
proposing nothing, which is the correct behaviour and has been throughout.
Everything below documents a system that was measured and found not to have an
edge — it is kept as the record of that, not as a plan to trade.

## Variants tested, and the retirement rule

A verdict rule is pre-registered in `docs/THEORY.md` and was written before the
numbers existed: **if the best of the four arms has a negative mean return,
spot grid trading is retired and no further variants are tested.**

Four arms over 29 pairs and 2 500 out-of-sample bars: baseline rolling re-fit,
one-fill-then-re-analyse, trend-exit on ADX>25, and asymmetric (front-loaded)
rung sizes. Best arm was **C at +1.824%**, so the rule was **not triggered** and
the strategy is not retired.

It is not validated either. C's 95% bootstrap CI is **[−0.473%, +4.381%]**, it
is positive on 15 of 27 non-zero pairs, and dropping its best two pairs takes it
to +0.454%. Decomposed, C earns **less** from completed cycles than the baseline
(+17.29 vs +25.14 per 1 000) and wins only on the inventory mark — it is an
exposure effect, not a trading edge, and a directional one. Full numbers in
`backtest_results.md`.

A and B both look better than baseline only because they barely trade: A
deploys 0.01x of capital, B exits on the trend signal in nearly every segment.
Both have CIs entirely below zero.

## Re-fitting, and the inventory bound

A grid is not fitted once and held. `backtest.rolling_walk_forward` re-derives
the band from the trailing 45 days every **14 days** and rebuilds the ladder,
which is what the analyst does live. A single-fit backtest measures band
staleness instead of the strategy: across 29 pairs it left price outside the
band **78.7%** of the time with **14/29** pairs completing zero cycles. With
re-fitting those become **43.9%** and **2/29**.

It also makes the measured return worse (−0.402% → −1.451%), because a grid
that re-ranges actually trades, and trading these pairs in this regime loses
money. Both numbers are in `backtest_results.md`; the quieter one was not the
better one.

At each re-fit the grid is torn down and inventory is **liquidated at market,
paying the taker fee**. Carrying it instead would hide the cost of re-ranging.

The inventory bound is `BOUND_FRACTION` (0.6) of the ladder's real maximum,
`sum(quote_per_level / price_i)` over the buy levels. It was previously
`max_quote_total / lower`, which is reachable only if every rung fills at the
bottom price — so it never engaged on any pair. A test now fails if it stops
engaging on a straight decline.

A **consistency check** computes ADF and the efficiency ratio on the short
window too, and rejects when the recent window flatly contradicts the long one:
a pair can be mean-reverting by character and be trending right now, and the
band is placed in the present. (ADX is reported for both windows but not gated
on across them — Wilder smoothing makes it a purely local statistic, identical
to the last digit on both windows, so the long-window ADX gate already covers
it.)

## Confidence, and what it is not

Structural gates stay hard and are never traded off: fee coverage, liquidity,
a stop, spot-only, the exposure cap, and the exchange's minimum order size.
Failing any one is still an outright rejection.

The *evidence* is graded 0-1 instead — ADF p-value, the position of the whole
Hurst interval, sub-window stability, the walk-forward result, ADX and the
efficiency ratio. The weighting lives in one place, `grid/confidence.py`, and
is documented there.

`config.yaml: grid.permissivity` decides what is shown: `strict` (>= 0.65,
default), `balanced` (>= 0.50), `exploratory` (>= 0.35). It never relaxes a
structural gate, and everything below the line is still logged with its score.

**The number is uncalibrated.** It is a weighted sum of measurements, not a
probability, and until enough grids have resolved to score it there is no
reason to believe 0.65 wins 65% of the time. The web UI says so on every page
and shows the Brier score once there is anything to score.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest                  # verify the foundation before adding to it
cp .env.example .env              # then fill in your keys
```

OKX public market data needs no API key, so stage 1 of the build plan is testable
the moment you start.
