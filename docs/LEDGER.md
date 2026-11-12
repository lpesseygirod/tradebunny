# Prediction Ledger — rules and design

The older half of this repo: a prediction journal that tests whether reasoning about
the news has any edge before money is involved. Many parts are restrictive on purpose;
this document explains why.

## Two systems live here

- **`src/grid/` — Grid Pilot.** A spot grid-trading system. **It contains NO AI at
  runtime**: every parameter is measured by `metrics.py`, gated by `rubric.py`, and
  the approval note is a template. No API key is required to run it. Its rules are
  documented in `README.md`, and `rubric.ANALYST_BRIEF` is kept as documentation of
  what the gates are for — nothing sends it anywhere. This is the half that can
  touch real money.
- **`src/ledger/` — the prediction journal** described below. This one still has a
  model call (`ledger/reason.py`) and needs `ANTHROPIC_API_KEY`. It never trades.

The rules below govern the LEDGER. Grid Pilot's rules are in `README.md`.

## What this is

A **prediction journal** for market forecasts. It is NOT a trading bot and must not
become one without an explicit decision by the owner. Twice a day it:

1. (morning) gathers news and SEC filings, reasons about second-order effects,
   and writes falsifiable predictions to an append-only ledger,
2. (evening) resolves predictions whose horizon has expired, scores them against
   a benchmark, and updates the scorecard.

The point is to find out whether the reasoning has any edge **before** any money
is involved. Everything restrictive in this codebase serves that goal.

## Non-negotiable rules

These are enforced in `rules.py` and `store.py`. They are not relaxed to make a test
pass or to get more signals per day.

1. **Write-before-outcome.** A prediction is hashed at creation (`content_hash`)
   and the row is immutable afterwards. The only mutable columns are the decision
   fields and the outcome fields, and each may be written exactly once.
2. **No first-order predictions.** If the source article's subject *is* the ticker
   being predicted, reject it. The edge, if any, is in second-order links that take
   days to price in — not in racing HFT to the obvious trade.
3. **Falsifiable or rejected.** Every prediction needs ticker, direction, magnitude,
   benchmark, horizon, confidence, and a stated falsifier. Missing any → rejected.
4. **Relative, not absolute.** Outcomes are scored as
   `excess = asset_return - benchmark_return`. A stock that rose less than its
   benchmark is a MISS. Never score against zero.
5. **Fixed universe.** Only tickers in `config.yaml: universe` are eligible. This
   stops the model from "discovering" illiquid microcaps that moved on no volume.
6. **Costs subtracted.** `cost_bps` is deducted from every excess return. An edge
   smaller than the cost of trading is not an edge.
7. **Every prediction gets a random twin.** Same universe, same horizon, random
   ticker and direction. The twin is scored identically. If the real predictions do
   not beat their twins, there is no edge — this is the primary control.
8. **Decisions lock.** The owner may mark bought/skipped until `lock_at`. After
   that the decision is frozen as `skipped`. No retroactive edits, ever.
9. **Log the silence.** A run that produces no predictions writes a `run` row with
   `n_predictions = 0`. An empty day and a crashed scraper must be distinguishable.
10. **Point-in-time.** Use the article's original `published_at`, never fetch time.
    News APIs backfill and revise; using fetch time silently creates lookahead bias.

## Kill criteria

Written in `config.yaml`. Set in advance, honoured without renegotiation:
after 100 resolved predictions, an engine whose mean net excess is below
`min_mean_excess_pct` or whose Brier score is worse than 0.25 is retired.

## Architecture

```
cli.py          entry points: morning, evening, score, check, decide
store.py        SQLite ledger. Append-only. Owns all schema and invariants.
rules.py        Validation gate. Every candidate passes through validate().
scoring.py      Pure functions: excess return, hit, Brier, aggregate scorecard.
twin.py         Random-twin generation.
sources/
  edgar.py      SEC EDGAR: Form 4, Schedule 13D/G. Free, no key, needs User-Agent.
  prices.py     Alpaca market data. Used only for resolution, never for signals.
  news.py       News ingestion. Pluggable provider.
reason.py       The LLM step. Produces candidates; rules.py decides what survives.
notify.py       Telegram delivery.
```

`scoring.py` and `rules.py` are pure and fully unit-tested. No network, no clock,
no I/O: `now` is passed in as a parameter.

## Data source notes

- **SEC EDGAR** is free and needs no API key, but it **requires** a descriptive
  `User-Agent` header containing a contact email, and rate-limits at 10 req/sec.
  Requests without that header get 403. Endpoints used:
  - `https://data.sec.gov/submissions/CIK{cik:010d}.json` — a filer's recent filings
  - `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&output=atom`
  - `https://efts.sec.gov/LATEST/search-index?q=...&forms=SC+13D` — full-text search
- **Alpaca** paper keys work for market data. Base URL for data is
  `https://data.alpaca.markets`. Paper trading is `https://paper-api.alpaca.markets`.
- Filing timeliness, which is why we prefer Form 4 and 13D over 13F:
  Form 4 = 2 business days · Schedule 13D = 5 business days ·
  13G passive = 5 business days · 13F = 45 days after quarter end (too stale to trade).

## Current status

Core is written and unit-tested. The network-facing modules (`sources/`, `reason.py`,
`notify.py`) follow the documented APIs but have not been verified end to end against
sec.gov and Alpaca yet. `python -m ledger.cli check` exercises each source and reports
what actually works.
