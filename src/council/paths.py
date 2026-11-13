"""Where the council keeps its files.

Everything lives under TRADEBUNNY_HOME/data/council, which git ignores: the
personal mandate, the database and the price cache never reach the public repo.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def home() -> Path:
    return Path(os.getenv("TRADEBUNNY_HOME", ".")).resolve()


# Three books, same rules: "main" waits for your OK, "autopilot" approves itself,
# "rules" is the comparison: the committee's rules with no AI at all.
BOOKS = {"main": "council", "autopilot": "council-autopilot", "rules": "council-rules"}
BOOK_NAMES = {"main": "With you", "autopilot": "Autopilot", "rules": "Rules only"}
BOOK_URLS = {"main": "/", "autopilot": "/autopilot/", "rules": "/rules-only/"}


def book() -> str:
    b = os.getenv("COUNCIL_BOOK", "main")
    if b not in BOOKS:
        raise ValueError(f"unknown book {b!r}: use one of {', '.join(BOOKS)}")
    return b


def data_dir(name: str = "") -> Path:
    d = home() / "data" / BOOKS[name or book()]
    d.mkdir(parents=True, exist_ok=True)
    return d


def mandate_path(name: str = "") -> Path:
    return data_dir(name) / "mandate.yaml"


def db_path(name: str = "") -> Path:
    return data_dir(name) / "council.db"


EXAMPLE_MANDATE = REPO / "mandate.example.yaml"
SNAPSHOT = REPO / "examples" / "council" / "quotes_snapshot.json"


def load_env() -> None:
    """Read ANTHROPIC_API_KEY and friends from .env in the project (git-ignored).
    Values already set in the environment win."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for f in (REPO / ".env", home() / ".env"):
        if f.exists():
            load_dotenv(f, override=False)
