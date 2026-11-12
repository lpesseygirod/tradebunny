"""Random twins — the control group.

For every real prediction, generate a random one from the same universe with the
same horizon. Score both identically. If the reasoned predictions do not beat their
own coin flips, there is no edge, and you learn that in weeks for the cost of a few
lines of code instead of in years for the cost of real money.

This is the single most important honesty mechanism in the project.
"""

from __future__ import annotations

import random
from typing import Any, Optional


def make_twin(pred: dict[str, Any], universe: list[str],
              rng: Optional[random.Random] = None) -> dict[str, str]:
    """A random ticker and direction, matched to the real prediction's horizon.

    Excludes the real target so the twin is genuinely independent.
    """
    rng = rng or random.Random()
    pool = [t for t in universe
            if t.upper() != str(pred["ticker"]).upper()
            and t.upper() != str(pred["benchmark"]).upper()]
    if not pool:
        raise ValueError("universe too small to build an independent twin")
    return {
        "twin_ticker": rng.choice(pool),
        "twin_direction": rng.choice(("long", "short")),
    }
