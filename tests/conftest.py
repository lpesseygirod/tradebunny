"""Run every test against a throwaway LEDGER_HOME.

Paths like data/grid.db and data/execution.json are resolved from LEDGER_HOME
when grid modules are imported, so this must run before any test imports them.
Without it, the suite writes into the real data/ folder.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_HOME = Path(tempfile.mkdtemp(prefix="gridpilot-test-"))
shutil.copy(_REPO / "config.yaml", _HOME / "config.yaml")
(_HOME / "data").mkdir()
os.environ["LEDGER_HOME"] = str(_HOME)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
