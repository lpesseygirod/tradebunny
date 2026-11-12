# Investment council — build spec

Status: spec only, nothing built yet (12 Nov 2026). Sources: the TraderBunny
claude.ai project ("Trader committee team" chat, "Portfolio analysis" chat,
the Investment Committee Report, and the Sept/Oct review docs). The latest
review (6 Oct 2026, "Supply Chain Market Review") wins where docs disagree.

## Decisions already made

- TradeBunny becomes the investment council. The crypto grid stays in
  `src/grid/` as chapter one: tested, then retired by its own rule.
- First build: Phase 1 (research memos, decisions recorded) plus a **paper
  portfolio** that "buys" approved ideas at real prices and is compared with
  the index. No broker yet.
- Paper start: the committee report's model portfolio, notional €10,000,
  bought in three tranches.
- AI model: Claude API (`claude-opus-5-5`, adaptive thinking, structured
  output via `client.messages.parse`). Red team on a different Claude model
  from the other roles.
- The repo is public: real holdings, amounts and the personal mandate live in
  a local, git-ignored file; only `mandate.example.yaml` is committed.

## Organisation (from the "Trader committee team" chat)

Two chambers, blind research, plain-code backbone.

- **Layer 0, the principal (Lea).** Owns the mandate file. Only she changes
  it, at the quarterly review. Agents may only propose a rule change memo.
- **Layer 1, code (no AI):** mandate gate (universe + rules, runs first);
  data steward (every number has a source and a date, stale data rejected,
  agents may only cite its fact sheet); risk engine (the only veto); sizing
  engine (the AI never outputs an amount); ledger and scorekeeper (every memo,
  vote and outcome, compared with simply holding the index); order guard later.
- **Layer 2, research desk, blind then one cross-examination:** macro
  strategist (monthly regime memo, updates scenario probabilities, never names
  a stock); thesis analyst; valuation analyst (fact-sheet numbers only); red
  team (pre-mortem written before reading the thesis, then its top 3
  objections; the thesis analyst gets one rebuttal).
- **Layer 3, committee:** risk officer (advisory memo, can only be stricter);
  CIO (approve / reject / more research + conviction none/starter/core; must
  answer each ranked objection; "no action" is the default).
- **Layer 4, stewardship:** thesis-breaker triggers; reducing risk has a fast
  path; a price drop alone never triggers a sale.
- Meetings: idea meeting (max 4 a month), monthly portfolio review, trigger
  meeting.

## How a stock is judged (from the 6 Oct review)

1. **Business test, five points:** few real suppliers (monopoly or 3–4);
   customers need years to switch; small cost to the customer but critical;
   the theme is a material share of revenue ("purity"); growth paid from
   cash flow. "Whoever wins, they must buy from this company" (Nvidia is a
   customer of the chokepoints, not one).
2. **Price test:** forward P/E against the company's own 5-year history and
   its peers; how it does if rates stay high.
3. **Bull case and devil's advocate** as separate roles, plus "what would
   prove the bear right".
4. **Verdict schema:** grade + action + buy trigger (a date or a price) +
   "sell if" tied to the thesis, not the price.
   - A, buy in stages: passes all five points, fairly priced.
   - B, buy on trigger / hold small / provisional: one open issue.
   - C, watch: cheap but an unresolved business problem.
   - D, avoid: fails the business test (theme under 1% of sales, negative
     cash flow, no earnings, price war, extreme multiple).
   - Not graded: too little data.
5. **Only evidence from this round counts.** A grade that cannot be
   reconfirmed is downgraded (the 6 Oct review cut nine A grades to one).
6. **Separate fact-check pass** on key claims (6 Oct: 22 checked, 5 corrected).
7. **Scenario matrix** per holding: peace, stagflation, AI spending bust,
   rate cuts, recession (wins / neutral / loses / not tested).

## Hard rules for the risk engine (code, not AI)

- Index core is the base; single stocks are a satellite (7 now, max 10–12).
- Single stock about 3–5%, never above 5%. TSMC (Taiwan) max 4%.
- AI-linked single stocks below half of the single-stock part (latest rule;
  the older rule was under 20% / 25% of the total). Trim one AI name before
  adding another.
- Two risk settings: Conservative (falls of ~20% would make you sell) and
  Growth-tilted (10-year horizon, can hold through ~35%). Amount, horizon and
  maximum loss must be written down before any money moves.
- Model allocations (Conservative / Growth): MSCI World 25/35%, Europe ETF
  15/20%, short euro bonds 15/8%, CME 5/5, Prysmian 5/5, Vulcan 4/4,
  MetLife 4/3, Allianz 4/3 (after valuation check), gold 4/3, Ajinomoto 3/4,
  watch-list reserve 11/7%, cash 5/3%.
- Watch-list reserve has a deadline (31 Mar 2027); unused cash goes to the
  index ETFs.
- Limit orders only, three tranches. Prefer euro listings.
- Review quarterly; rebalance only if a bucket drifts more than 5 points.
- 5-year positions: never sell on a 20% drawdown unless a thesis breaker fires.

### More rules from the Sept–Oct portfolio reviews (2 Oct Full Review is latest for the portfolio)

- No position below 3%; single stock max 6% in these docs vs about 5% in the
  6 Oct review. Open question for Lea: which cap wins (default: 5%).
- Max 10 single stocks; adding a name means selling one of the ten first.
- AI-sensitive names (Prysmian, Vulcan, Ajinomoto, ASML, TSMC): Conservative
  18% (cap 20%), Growth 20% (cap 25%).
- New names are funded from the index ETFs and the reserve, so total risk
  stays the same. Size by how much a stock can lose, not by conviction.
- Dollar exposure near one-third; half of MSCI World in a EUR-hedged class.
- If the written max loss is hit: move 10 points from stocks to euro bonds.
- Benchmark MSCI World in euros; if the portfolio lags it for two years,
  simplify to the index.
- Watch-list triggers checked weekly. Rate switches: 10-year above 5.25% →
  cut growth to the low end; below 4.5% → shift back toward growth.
- Case files: the verdict follows the side with the stronger evidence; a
  strong business with a strong price critique goes to the watch list.
- Scenario exposure %: sum the weights rated "gains" and, separately,
  "loses" per scenario (flat holdings and cash excluded).

## Thesis breakers to encode

CME: long calm period cuts volumes, or a clearing rival. Vulcan: aggregates
pricing turns negative. MetLife/Allianz: fast rate cuts. Gold: central-bank
buying stops two quarters running. Euro bonds: rapid ECB cuts (extend
maturity, don't sell).

TSMC: monthly revenue growth under 20% two months running, or military
escalation / quarantine around Taiwan (cut to half). ASML: orders below sales
or new export bans. Ajinomoto: a second ABF supplier qualifies. Cameco:
Westinghouse listing withdrawn. Prysmian, Xylem: guidance cut. Siemens
Energy: orders below sales. Shin-Etsu: wafer price rise reversed. Air
Liquide: margin targets dropped. Sector: 2027 capex guidance flat or cut.

## Build order

1. `src/council/`: mandate loader + `mandate.example.yaml`; data steward
   (yfinance prices and forward P/E, stamped with source and date); risk
   engine with tests for every rule above.
2. Paper portfolio (reuse the grid's lessons: real prices, fees, three
   tranches, compare with the index), ledger in SQLite.
3. Agents with structured output (verdict schema above), blind round, one
   cross-examination, CIO; refusal handling; cost cap per meeting.
4. CLI (`council meet TICKER`, `council review`), then the README rewrite
   with the grid as chapter one.
