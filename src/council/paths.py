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


def data_dir() -> Path:
    d = home() / "data" / "council"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mandate_path() -> Path:
    return data_dir() / "mandate.yaml"


def db_path() -> Path:
    return data_dir() / "council.db"


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
