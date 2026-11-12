"""Offline demo: run the real analyst on saved OKX candles, then open the app.

    python scripts/demo.py [--port 8080]

Nothing touches the network or an exchange. The candles in examples/candles/
are real OKX hourly data, replayed with their timestamps shifted to "now" so
the freshness gates accept them. The analyst scans them exactly as it would
live, writes its proposals, and the approval app starts on a throwaway data
folder in paper mode.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "examples" / "candles"
HOUR_MS = 3_600_000.0


class ReplaySource:
    """Stands in for grid.okx, serving saved candles instead of the API."""

    def __init__(self, directory: Path):
        self.series = {}
        for f in sorted(directory.glob("*_1H.json")):
            d = json.loads(f.read_text())
            self.series[d["inst_id"]] = d["candles"]

    @staticmethod
    def _stamp(source):
        from grid import okx
        okx._stamp(source)  # the app's freshness badges read these, as with live OKX

    def get_instruments(self, inst_type="SPOT"):
        from grid.okx import Instrument
        self._stamp("instruments")
        return {s: Instrument(inst_id=s, min_size=1e-6, lot_size=1e-6, tick_size=1e-8,
                              base=s.split("-")[0], quote="USDT", state="live")
                for s in self.series}

    def get_trade_fee(self, inst_id="BTC-USDT", inst_type="SPOT"):
        raise RuntimeError("no fee key in the demo")  # -> pessimistic fallback rates

    def bars_per_day(self, bar):
        return {"1H": 24.0, "4H": 6.0, "1D": 1.0}[bar]

    def get_spot_tickers(self):
        self._stamp("tickers")
        out = {}
        for s, c in self.series.items():
            day = c[-24:]
            out[s] = {"last": day[-1]["close"],
                      "quote_volume_24h": sum(x["volume"] * x["close"] for x in day)}
        return out

    def get_candles(self, inst_id, bar="1H", limit=300):
        self._stamp("candles")
        c = self.series[inst_id][-limit:]
        shift = (time.time() * 1000 - HOUR_MS) - c[-1]["ts"]
        return [dict(x, ts=x["ts"] + shift) for x in c]

    def fetched_at(self, source):
        return time.time()


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline Grid Pilot demo")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--capital", type=float, default=500.0)
    args = ap.parse_args()

    home = Path(tempfile.mkdtemp(prefix="gridpilot-demo-"))
    shutil.copy(ROOT / "config.yaml", home / "config.yaml")
    (home / "data" / "plans").mkdir(parents=True)
    os.environ["LEDGER_HOME"] = str(home)          # before any grid import
    os.environ.setdefault("GRID_WEB_TOKEN", "demo")
    sys.path.insert(0, str(ROOT / "src"))

    from grid import analyst, web
    from grid.analyst import ScanReport
    from grid.control import Control

    source = ReplaySource(SAMPLES)
    os.environ["GRID_UNIVERSE"] = ",".join(sorted(source.series))  # "Run scan" in the app
    control = Control()
    control.initialise()
    report = ScanReport()
    plans = analyst.scan(sorted(source.series), args.capital, control=control,
                         source=source, report=report)
    for p in plans:
        p.save(web.PLANS_DIR)
    print(report.explain())
    print(f"\n{len(plans)} proposal(s) written to {web.PLANS_DIR}")
    print(f"Open http://127.0.0.1:{args.port}/?token={os.environ['GRID_WEB_TOKEN']}")
    app = web.create_app(control, source=source)  # scans from the app replay too
    app.run(host="127.0.0.1", port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
