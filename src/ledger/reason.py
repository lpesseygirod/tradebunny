"""The AI step: turn today's news and filings into falsifiable theses.

This is where Claude does the thinking. The prompt is adversarial about its own
output, because the failure mode of an LLM here is not silence: it is a confident,
wrong causal chain that reads exactly like a correct one.

Nothing this module returns is trusted. Every candidate goes through rules.validate().
"""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic

MODEL = os.getenv("LEDGER_MODEL", "claude-sonnet-4-6")

SYSTEM = """You generate falsifiable market predictions for a research journal that \
scores you honestly. You are not giving investment advice and no money is at stake — \
your only goal is to be measurably right more often than a coin flip.

THE ONE RULE THAT MATTERS: never predict the subject of the article.

If Reuters reports that IKEA is expanding production, the market priced IKEA within \
seconds and you cannot beat it. Your only possible edge is the second- and third-order \
link — who supplies IKEA, who competes with that supplier, whose input costs move — \
because pricing those requires someone to actually reason, which takes days. If you \
cannot find a non-obvious downstream name, return nothing for that article. Returning \
nothing is a good outcome and costs you nothing.

Rules for every candidate you emit:
- The target must be a real, liquid, listed company in the provided universe.
- The causal chain needs at least three links and each link must be a real economic \
mechanism, not a vibe. "Sentiment improves" is not a mechanism. "Pulpwood is 40% of \
panel input cost" is.
- State the thesis as a claim that can be wrong. No hedging words.
- Give a falsifier: the specific thing that, if true, means you were wrong. Write the \
strongest one you can, not the weakest.
- Confidence between 0.50 and 0.90, and mean what you say — you are scored on \
calibration, so a 0.80 that fails hurts far more than a 0.60 that fails.
- Magnitude is EXCESS return versus the named benchmark, not absolute return.

Before emitting a candidate, ask yourself: would a competent analyst reading the same \
headline reach this within thirty seconds? If yes, it is already priced — discard it.

Return between 0 and 3 candidates. Fewer, better ones are strongly preferred. \
An empty list is a perfectly good answer on most days."""

TOOL = {
    "name": "emit_predictions",
    "description": "Return the surviving prediction candidates, or an empty list.",
    "input_schema": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string", "description": "Target ticker, from the universe"},
                        "company": {"type": "string"},
                        "direction": {"type": "string", "enum": ["long", "short"]},
                        "magnitude_pct": {"type": "number", "description": "Expected EXCESS return vs benchmark"},
                        "benchmark": {"type": "string", "description": "Benchmark ETF ticker"},
                        "horizon_days": {"type": "integer", "description": "Trading days, 3-60"},
                        "confidence": {"type": "number", "description": "0.50-0.90"},
                        "chain": {
                            "type": "array", "items": {"type": "string"},
                            "description": "At least 3 links, source event first, target last",
                        },
                        "thesis": {"type": "string", "description": "The mechanism, stated as a committed claim"},
                        "falsifier": {"type": "string", "description": "What would prove this wrong"},
                        "source_subjects": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Tickers the source article is ABOUT — used to reject first-order trades",
                        },
                        "source_url": {"type": "string"},
                        "source_title": {"type": "string"},
                        "source_published_at": {"type": "string"},
                    },
                    "required": ["ticker", "direction", "magnitude_pct", "benchmark",
                                 "horizon_days", "confidence", "chain", "thesis",
                                 "falsifier", "source_subjects"],
                },
            }
        },
        "required": ["candidates"],
    },
}


def _render(items: list[dict[str, Any]]) -> str:
    out = []
    for i, it in enumerate(items, 1):
        out.append(
            f"[{i}] {it.get('title','(untitled)')}\n"
            f"    published: {it.get('published_at','unknown')}\n"
            f"    subjects:  {', '.join(it.get('subjects', [])) or 'unknown'}\n"
            f"    url:       {it.get('url','')}\n"
            f"    summary:   {(it.get('summary') or '')[:900]}"
        )
    return "\n\n".join(out)


def generate(
    items: list[dict[str, Any]],
    *,
    universe: list[str],
    benchmarks: dict[str, str],
    client: anthropic.Anthropic | None = None,
    max_tokens: int = 4000,
) -> list[dict[str, Any]]:
    """Ask Claude for candidate predictions. Returns raw, unvalidated candidates."""
    if not items:
        return []
    client = client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = (
        f"Today's material ({len(items)} items):\n\n{_render(items)}\n\n"
        f"Tradeable universe:\n{', '.join(sorted(universe))}\n\n"
        f"Benchmarks by sector:\n{json.dumps(benchmarks, indent=2)}\n\n"
        "Emit only candidates that survive your own rules. Empty is fine."
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=SYSTEM,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "emit_predictions"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "emit_predictions":
            return block.input.get("candidates", [])
    return []


def critique(candidate: dict[str, Any], client: anthropic.Anthropic | None = None) -> dict[str, Any]:
    """Second pass: attack the candidate before it is recorded.

    A separate call with no attachment to the first answer catches a surprising
    amount of confident nonsense. Cheap insurance relative to a poisoned scorecard.
    """
    client = client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        system=(
            "You are the sceptic. Find the strongest reason this prediction is wrong "
            "or already priced in. Reply with JSON only: "
            '{"kill": true|false, "reason": "...", "confidence_adjustment": -0.2..0.0}. '
            "Set kill=true if the link is not a real economic mechanism, if the target "
            "is really the article's own subject, or if any competent analyst would "
            "reach the same conclusion in thirty seconds."
        ),
        messages=[{"role": "user", "content": json.dumps(candidate, indent=2)}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    try:
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        # An unparseable critique is not a reason to let the candidate through.
        return {"kill": True, "reason": "critique unparseable", "confidence_adjustment": 0.0}
