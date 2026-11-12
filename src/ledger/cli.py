"""Entry points. Run with:  python -m ledger.cli <command>

    check     verify every external service works    ← run this first
    morning   gather, reason, validate, record, notify
    evening   resolve what is due, lock decisions, notify
    score     print the scorecard
    decide    mark a prediction bought/skipped from the terminal
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from . import reason, rules, scoring, twin
from .notify import evening_report, morning_report, scorecard_block, send
from .sources import edgar, news, prices
from .store import Ledger

load_dotenv()
ROOT = Path(__file__).resolve().parents[2]


def cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


# ------------------------------------------------------------------ check

def cmd_check(_args) -> int:
    c = cfg()
    print("Checking external services.\n")
    results = {}

    edgar.configure(os.getenv("SEC_CONTACT_EMAIL", ""))
    results["SEC EDGAR"] = edgar.check()
    results["Alpaca prices"] = prices.check()
    try:
        from .notify import check as notify_check
        results["Telegram"] = notify_check()
    except Exception as e:
        results["Telegram"] = {"ok": False, "error": str(e)}
    results["Anthropic API"] = (
        {"ok": True, "note": "key present"} if os.getenv("ANTHROPIC_API_KEY")
        else {"ok": False, "error": "ANTHROPIC_API_KEY not set"})

    width = max(len(k) for k in results)
    for name, r in results.items():
        mark = "✓" if r.get("ok") else "✗"
        detail = "" if r.get("ok") else f"  {r.get('error','')}"
        print(f"  {mark} {name.ljust(width)}{detail}")

    led = Ledger(c["ledger_path"])
    bad = led.verify_integrity()
    print(f"\n  {'✓' if not bad else '✗'} Ledger integrity"
          f"{'' if not bad else f'  tampered rows: {bad}'}")
    print(f"    {len(led.open_predictions())} open, {len(led.resolved())} resolved")
    led.close()

    return 0 if all(r.get("ok") for r in results.values()) and not bad else 1


# ------------------------------------------------------------------ morning

def cmd_morning(args) -> int:
    c = cfg()
    led = Ledger(c["ledger_path"])
    now = datetime.now(timezone.utc)
    universe = c["universe"]
    benchmarks = c["benchmarks"]
    rejections: list[str] = []

    try:
        edgar.configure(os.environ["SEC_CONTACT_EMAIL"])
        items: list[dict] = []

        for cl in edgar.cluster_buys(min_insiders=c["signals"]["min_insiders"],
                                     min_total_usd=c["signals"]["min_cluster_usd"])[:5]:
            # The NEWEST transaction date, never fetch time. Stamping "now" meant a
            # cluster assembled from weeks-old filings always passed the 72-hour
            # freshness gate — exactly the lookahead bias rule 10 exists to prevent.
            dates = [t.get("date") for t in cl["transactions"] if t.get("date")]
            if not dates:
                rejections.append(f"{cl['ticker']}: cluster has no transaction dates")
                continue
            items.append({
                "title": f"{cl['n_insiders']} insiders bought {cl['ticker']} "
                         f"(${cl['total_usd']:,.0f})",
                "summary": json.dumps(cl["transactions"][:6]),
                "published_at": max(dates),
                "subjects": [cl["ticker"]],
                "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                       f"&company={cl['ticker']}&type=4",
            })
        items.extend(list(edgar.activist_stakes(count=25))[:5])

        # General market news. Not restricted to the universe on purpose: the edge is
        # in reasoning from a story about company A to a consequence for company B.
        try:
            items.extend(news.gather(hours_back=c["signals"]["max_source_age_hours"],
                                     max_items=c["signals"].get("max_news_items", 12)))
        except Exception as e:
            print(f"news source unavailable, continuing on filings only: {e}",
                  file=sys.stderr)

        candidates = reason.generate(items, universe=universe, benchmarks=benchmarks)

        recorded = []
        open_now = led.open_tickers()
        for cand in candidates:
            v = rules.validate(cand, universe=set(universe),
                               benchmarks=set(benchmarks.values()),
                               open_tickers=open_now, now=now)
            if not v.ok:
                rejections.append(f"{cand.get('ticker','?')}: {'; '.join(v.reasons)}")
                continue

            crit = reason.critique(cand)
            if crit.get("kill"):
                rejections.append(f"{cand['ticker']}: critique killed — {crit.get('reason','')}")
                continue
            # The critique may only ever LOWER confidence, and the result stays
            # inside the range validate() just enforced. Without both clamps a
            # returned +0.15 on a 0.85 candidate recorded 1.0 — past the cap, and
            # the worst possible Brier contribution when it misses.
            adj = min(0.0, float(crit.get("confidence_adjustment", 0.0) or 0.0))
            cand["confidence"] = min(0.9, max(0.5, cand["confidence"] + adj))

            cand.update(twin.make_twin(cand, universe))
            cand["engine"] = "equity"
            cand["lock_at"] = rules.lock_time(now).isoformat(timespec="seconds")
            cand["resolves_on"] = rules.resolves_on(now, cand["horizon_days"])
            cand.pop("source_subjects", None)

            pid = led.record(cand)
            cand["id"] = pid
            recorded.append(cand)
            open_now.add(cand["ticker"].upper())

        card = scoring.build_scorecard(led.resolved())
        if not args.dry_run:
            send(morning_report(recorded, card, rejected=len(rejections)),
                 buttons=[{"id": p["id"]} for p in recorded])
        else:
            print(morning_report(recorded, card, rejected=len(rejections)))

        led.log_run("morning", n_candidates=len(candidates),
                    n_predictions=len(recorded), rejections=rejections)
        print(f"\n{len(recorded)} recorded, {len(rejections)} rejected.")
        for r in rejections:
            print(f"  rejected · {r}")
        return 0

    except Exception as e:
        led.log_run("morning", error=f"{type(e).__name__}: {e}", rejections=rejections)
        print(f"morning run failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        led.close()


# ------------------------------------------------------------------ evening

def cmd_evening(args) -> int:
    c = cfg()
    led = Ledger(c["ledger_path"])
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    try:
        locked = led.lock_expired(now)
        resolved_rows = []

        for row in led.due(today):
            legs = prices.pair_prices(row["ticker"], row["benchmark"],
                                      row["created_at"][:10], row["resolves_on"])
            if legs is None:
                continue                      # leave open rather than guess

            out = scoring.excess_return(
                legs["entry"], legs["exit"], legs["bench_entry"], legs["bench_exit"],
                row["direction"], cost_bps=c["cost_bps"])

            twin_fields = {}
            if row["twin_ticker"]:
                tlegs = prices.pair_prices(row["twin_ticker"], row["benchmark"],
                                           row["created_at"][:10], row["resolves_on"])
                if tlegs:
                    t = scoring.excess_return(
                        tlegs["entry"], tlegs["exit"], tlegs["bench_entry"],
                        tlegs["bench_exit"], row["twin_direction"], cost_bps=c["cost_bps"])
                    twin_fields = {"twin_net_excess_pct": t.net_excess_pct,
                                   "twin_hit": int(t.hit)}

            led.resolve(row["id"], entry_price=legs["entry"], exit_price=legs["exit"],
                        bench_entry=legs["bench_entry"], bench_exit=legs["bench_exit"],
                        net_excess_pct=out.net_excess_pct, hit=int(out.hit), **twin_fields)
            resolved_rows.append({**dict(row), "net_excess_pct": out.net_excess_pct,
                                  "hit": out.hit})

        card = scoring.build_scorecard(led.resolved())
        if not args.dry_run:
            send(evening_report(resolved_rows, card, locked=locked))
        else:
            print(evening_report(resolved_rows, card, locked=locked))

        led.log_run("evening", n_resolved=len(resolved_rows))
        print(f"{len(resolved_rows)} resolved, {locked} locked.")
        return 0

    except Exception as e:
        led.log_run("evening", error=f"{type(e).__name__}: {e}")
        print(f"evening run failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        led.close()


# ------------------------------------------------------------------ score / decide

def cmd_score(_args) -> int:
    c = cfg()
    led = Ledger(c["ledger_path"])
    card = scoring.build_scorecard(led.resolved())
    print(scorecard_block(card).replace("<b>", "").replace("</b>", "")
          .replace("<code>", "").replace("</code>", "")
          .replace("<i>", "").replace("</i>", ""))
    led.close()
    return 0


def cmd_decide(args) -> int:
    c = cfg()
    led = Ledger(c["ledger_path"])
    ok = led.decide(args.id, args.decision, size_eur=args.size)
    print("recorded" if ok else "refused — already decided, or past the lock time")
    led.close()
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="ledger")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("score").set_defaults(fn=cmd_score)

    for name, fn in (("morning", cmd_morning), ("evening", cmd_evening)):
        sp = sub.add_parser(name)
        sp.add_argument("--dry-run", action="store_true",
                        help="print the report instead of sending it")
        sp.set_defaults(fn=fn)

    d = sub.add_parser("decide")
    d.add_argument("id")
    d.add_argument("decision", choices=["bought", "skipped"])
    d.add_argument("--size", type=float, default=None)
    d.set_defaults(fn=cmd_decide)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
