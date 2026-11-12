# TradeBunny

[![tests](https://github.com/lpesseygirod/tradebunny/actions/workflows/ci.yml/badge.svg)](https://github.com/lpesseygirod/tradebunny/actions/workflows/ci.yml)

TradeBunny is a crypto grid-trading system. A grid puts buy orders below the price and sell orders above it, so it makes small profits when the price goes up and down without really going anywhere. The hard part is finding markets where that actually happens, and not losing everything when it doesn't.

So most of this project is about **measuring before trading**: it rejects almost every market, and it tells you exactly why.

![An offline scan: every coin rejected, each with its reasons](docs/media/scan.png)

> Status: the analyst, backtester, executor, risk rules and app are built, with 1,279 tests. It trades on **paper**: a simulated exchange that fills orders at live OKX prices. It has not traded real money, and the client that would send real orders to OKX is not built.

**Try it offline** (no API key, no exchange, real OKX candles saved in `examples/`):

```bash
pip install -r requirements.txt
python scripts/demo.py           # runs the analyst, then opens the app
```

---

## How it works

```mermaid
flowchart LR
    C[OKX market data] --> A[Analyst<br/>proposes]
    A --> Y{I approve<br/>or reject}
    Y --> X[Executor<br/>follows the plan]
    X --> P[Paper exchange<br/>live OKX prices]
```

It has two parts that are kept separate:

- **The analyst** ([`analyst.py`](src/grid/analyst.py), [`rubric.py`](src/grid/rubric.py)) looks at market data and proposes grids. It never trades.
- **The executor** ([`executor.py`](src/grid/executor.py)) follows a plan I've approved, and nothing else. It places its orders on a paper exchange that reads the real OKX price every minute and fills an order when the price reaches it. Each approved grid gets exactly the amount I approved.

OKX is used only for public market data (candles and prices) and, with a read-only key, my fee tier. No order is ever sent to OKX: the live order client ([`LiveExchange`](src/grid/executor.py)) is a stub that refuses every call.

There's no AI inside. At first an AI wrote a short summary of each plan, but I removed it: I'd rather decide by looking at the numbers than at a nice paragraph.

## Risk rules built into the code

- Spot only, no margin or futures ([`plan.validate()`](src/grid/plan.py) re-checks this on every executor pass)
- Limit orders only. If it can't read my real fees, it assumes the expensive ones
- A minimum gap between levels, so fees can't eat the profit
- Every plan has a stop loss
- Position size assumes the worst case. I found that my first sizing made a "2% risk" really 4.25%, and fixed it ([`sizing.py`](src/grid/sizing.py))
- It only picks markets that move a lot but don't trend (Hurst exponent, ADX, efficiency ratio and an ADF test, in [`metrics.py`](src/grid/metrics.py))
- If something can't be read, everything stops: three kill switches that fail closed ([`control.py`](src/grid/control.py))
- Caps on how many grids run at once and how much each one may commit. The executor re-checks them on every pass and holds back any grid over a cap (`admit` in [`executor.py`](src/grid/executor.py))
- Paper only for now. Live trading would also need an extra confirmation phrase

## Testing a strategy honestly

A grid that looks great on the data it was fitted on often stops working right after. So every candidate is tested **walk-forward**: the grid is fitted on the first part of the data and judged only on the part it has never seen ([`backtest.py`](src/grid/backtest.py)).

Here it is on real ETH data. Fitted on 900 hours, the grid made +4.98%. On the next 600 hours it made zero trades, because the price had left its range. So it's rejected:

![Walk-forward backtest rejecting an overfitted grid](docs/media/backtest-rejected.png)

<details>
<summary>On a shorter window the same check passes (+0.53% fitted, +0.76% on unseen data)</summary>

![Walk-forward backtest that generalises](docs/media/backtest-passes.png)

</details>

I backtest every candidate, not only the ones that look good, on 29 crypto pairs with OKX hourly data ([`docs/backtest_results.md`](docs/backtest_results.md)). Each result shows how much data it's based on, because some pairs have eight years of history and others only a few months.

## The app

A simple web page that works on my phone. Paper and real money are shown in clearly different sections. Each grid shows every trade like a bank statement, and compares the result with just buying and holding.

| Paper and live kept apart | Why each market was rejected |
|---|---|
| ![App overview](docs/media/app-overview.png) | ![Rejection reasons in the app](docs/media/app-rejections.png) |

## Run it

Python 3.12 or newer (tested on 3.13 and 3.14).

```bash
pip install -r requirements.txt
python -m pytest                                    # 1,279 tests, offline
python scripts/demo.py                              # offline demo: scan + app
PYTHONPATH=src python -m grid.cli backtest ETH-USDT --candles examples/candles/ETH-USDT_1H.json
```

With network access, the real commands are in the [`Makefile`](Makefile): `make scan`, `make web`, `make run` (the executor, trading on paper at live prices), `make check` (tests + a live OKX connectivity check). OKX public market data needs no key. Copy `.env.example` to `.env` to add a **read-only** OKX key (only used to read your fee tier) and the app token.

## Project layout

```
src/grid/         the trading system: analyst, backtest, executor, risk rules, app
src/ledger/       an earlier experiment: a news-prediction journal (docs/LEDGER.md)
tests/            1,279 offline tests
examples/candles/ real OKX hourly candles for the offline demo
docs/             design notes (DESIGN.md), the maths behind sizing (THEORY.md), results
```

## How I built it

I designed the strategy, the risk rules and the testing approach, and built it with Claude Code as my pair programmer, one module at a time with tests for each. The detailed design notes, including the variants I tried and retired, are in [`docs/DESIGN.md`](docs/DESIGN.md).

## Built with

Python, OKX market data API, NumPy, statsmodels, Flask, SQLite, pytest

[Lea Pessey Girod](https://github.com/lpesseygirod) · MIT licensed
