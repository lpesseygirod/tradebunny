"""Report delivery over Telegram.

Telegram is used rather than email because it supports inline buttons: the morning
report arrives with Bought/Skip attached to each prediction, so a decision is one tap
on my phone with no app to open. If deciding is inconvenient I skip it, and the
journal fills with unmarked entries.

Setup: message @BotFather, /newbot, take the token. Then message the new bot once
and read the chat id from https://api.telegram.org/bot<TOKEN>/getUpdates
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests

from .scoring import Scorecard, sample_size_warning


def _api(method: str, payload: dict[str, Any]) -> dict:
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                      json={"chat_id": chat, **payload}, timeout=20)
    r.raise_for_status()
    return r.json()


def _pct(x: Optional[float], digits: int = 2) -> str:
    return "—" if x is None else f"{x:+.{digits}f}%"


def morning_report(preds: list[dict], card: Scorecard, rejected: int = 0) -> str:
    if not preds:
        body = (f"<b>No signals today.</b>\nNothing cleared the bar"
                f"{f' — {rejected} candidates rejected' if rejected else ''}.")
    else:
        parts = []
        for p in preds:
            arrow = "▲" if p["direction"] == "long" else "▼"
            chain = " → ".join(p["chain"]) if isinstance(p["chain"], list) else p["chain"]
            parts.append(
                f"{arrow} <b>{p['ticker']}</b> {p.get('company','')}\n"
                f"<code>{p['magnitude_pct']:+.1f}% vs {p['benchmark']} · "
                f"{p['horizon_days']}d · conf {p['confidence']:.0%}</code>\n"
                f"<i>{chain}</i>\n\n{p['thesis']}\n\n"
                f"<b>Wrong if:</b> {p['falsifier']}"
            )
        body = "\n\n———\n\n".join(parts)

    return (f"📊 <b>MORNING BRIEF</b>\n\n{body}\n\n———\n{scorecard_block(card)}")


def evening_report(resolved: list[dict], card: Scorecard, locked: int = 0) -> str:
    if resolved:
        lines = []
        for r in resolved:
            mark = "✓" if r["hit"] else "✗"
            took = "you bought" if r.get("decision") == "bought" else "you skipped"
            lines.append(f"{mark} <b>{r['ticker']}</b> said {r['magnitude_pct']:+.1f}% → "
                         f"got {_pct(r['net_excess_pct'], 1)} · {took}")
        body = "\n".join(lines)
    else:
        body = "Nothing resolved today."
    tail = f"\n\n{locked} undecided entr{'y' if locked == 1 else 'ies'} locked as skipped." if locked else ""
    return f"🌙 <b>RECAP</b>\n\n{body}{tail}\n\n———\n{scorecard_block(card)}"


def scorecard_block(c: Scorecard) -> str:
    if c.n == 0:
        return "<i>No resolved predictions yet.</i>"
    verdict = ""
    if c.beats_twin is not None:
        verdict = ("\n<b>Beating the coin flip.</b>" if c.beats_twin
                   else "\n<b>Losing to the coin flip.</b>")
    filt = ""
    if c.filter_value is not None:
        filt = (f"\nYour picks: {c.taken}/{c.n}, "
                f"{_pct(c.taken_mean_net_excess)} vs bot {_pct(c.mean_net_excess)} "
                f"({_pct(c.filter_value)} from your filter)")
    warn = sample_size_warning(c.n)
    # Every one of these can legitimately be None — a twin whose price bars were
    # missing leaves twin_hit_rate unset, and formatting None with :.0% raises,
    # killing the evening report AFTER the outcomes were already written. The run
    # would then log "0 resolved" for a run that resolved several.
    def _rate(x: Optional[float]) -> str:
        return "—" if x is None else f"{x:.0%}"

    return (
        f"<b>Scorecard</b> ({c.n} resolved)\n"
        f"<code>hit rate    {_rate(c.hit_rate)}   twin {_rate(c.twin_hit_rate)}\n"
        f"mean excess {_pct(c.mean_net_excess)}   twin {_pct(c.twin_mean_net_excess)}\n"
        f"brier       {'—' if c.brier is None else f'{c.brier:.3f}'}   "
        f"(0.250 = useless)</code>"
        f"{verdict}{filt}"
        + (f"\n<i>{warn}</i>" if warn else "")
    )


def send(text: str, buttons: Optional[list[dict]] = None) -> dict:
    payload: dict[str, Any] = {"text": text, "parse_mode": "HTML",
                               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [
            [{"text": "✓ Bought", "callback_data": f"bought:{b['id']}"},
             {"text": "Skip", "callback_data": f"skipped:{b['id']}"}]
            for b in buttons
        ]}
    return _api("sendMessage", payload)


def check() -> dict:
    try:
        me = requests.get(
            f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/getMe",
            timeout=15).json()
        return {"ok": me.get("ok", False), "bot": me.get("result", {}).get("username")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
