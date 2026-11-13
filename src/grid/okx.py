"""OKX public market data — candles and spot tickers.

Public market data needs no API key and no signing. Only the private endpoints
(orders, balances) require credentials, and this module contains
none of those: it reads, it never trades.

Four things about this API are easy to get wrong, and each is handled below:

  1. **Candles come back NEWEST first.** Every metric in `grid/metrics.py` —
     Hurst, ADX, efficiency ratio, ATR — assumes chronological order. Feeding
     it a reversed series does not error, it silently measures the asset
     backwards. `get_candles` returns oldest-first.
  2. **Every field is a STRING**, including the timestamp. `"78422.3" < "9"`
     is True, so an unconverted series sorts and compares as nonsense.
  3. **The newest candle is still forming.** Its `confirm` flag is "0" and its
     close changes on every tick. Including it means the most recent bar of
     every backtest is a value that never actually existed at that time — a
     small, permanent lookahead bias. It is dropped.
  4. **`limit` is capped at 300 and the cap is silent.** Asking for 500 returns
     300 with `code: "0"` and no warning, so a caller believing it has 500 bars
     would compute statistics over a window 40% shorter than intended. Asking
     for more than 300 raises here instead.

Errors arrive as HTTP 200 with a non-zero `code` in the body, so
`raise_for_status()` alone catches nothing:

    unknown instrument  {"code":"51001","data":[],"msg":"Instrument ID ... doesn't exist."}
    bad bar interval    {"code":"51000","data":[],"msg":"Parameter bar error"}

Both would otherwise surface as a silent empty list, which the rubric would
report as "not enough bars", which points debugging at the wrong thing.
Every response passes through `_get`, which raises `OKXError` on either.

Rate limit for these two endpoints: 20 requests per 2 seconds, per IP.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
from dataclasses import dataclass

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

BASE = "https://www.okx.com/api/v5"

#: OKX truncates to this silently; this module refuses instead. See module docstring.
MAX_CANDLES = 300

#: `/market/history-candles` is documented at 100 per request. It is not: it
#: serves 300, and silently truncates anything larger exactly as
#: `/market/candles` does. Verified against the live endpoint — 100, 300, 301
#: and 400 return 100, 300, 300 and 300 rows. Paging at 300 rather than the
#: documented 100 is three times fewer requests against the same rate limit.
MAX_HISTORY = 300

#: Milliseconds per bar. Used to prove a fetched series is contiguous; a series
#: with a hole in it produces confident, wrong Hurst and ADX readings rather
#: than an error, which is the failure this whole module is arranged to avoid.
BAR_MS: dict[str, int] = {
    "1H": 3_600_000,
    "4H": 14_400_000,
    "1D": 86_400_000,
}

CANDLE_CACHE_DIR = Path(os.getenv("TRADEBUNNY_HOME", ".")).resolve() / "data" / "candles"

#: Documented limit for the public market-data endpoints: 20 requests / 2s per IP.
RATE_LIMIT = 20
RATE_WINDOW = 2.0

#: Candles per 24h for each supported interval.
#:
#: `rubric.assess` requires `bars_per_day` and gives it no default on purpose:
#: ATR is measured per bar, so assuming one bar per day understates the cycle
#: rate 24x on hourly candles and rejects every good intraday candidate. This
#: map is small on purpose: add an interval only after checking what
#: it does to that calculation.
BARS_PER_DAY: dict[str, float] = {
    "1H": 24.0,
    "4H": 6.0,
    "1D": 1.0,
}

_CALLS: deque[float] = deque()


class OKXError(RuntimeError):
    """A non-zero `code` in an OKX response body, or a malformed one.

    Carries the raw code so a caller can distinguish "that symbol
    does not exist" (51001) from a transient server fault.
    """

    def __init__(self, code: str, msg: str, endpoint: str) -> None:
        self.code = code
        self.msg = msg
        self.endpoint = endpoint
        super().__init__(f"OKX {endpoint} returned code {code!r}: {msg or '(no message)'}")


def _throttle() -> None:
    """Sliding window: never more than RATE_LIMIT calls in any RATE_WINDOW.

    A fixed inter-call gap would also work but wastes most of the allowance on
    a burst of one-off requests. This lets a scan of a few dozen symbols run at
    full speed and only blocks once the window is genuinely full.
    """
    now = time.monotonic()
    while _CALLS and now - _CALLS[0] >= RATE_WINDOW:
        _CALLS.popleft()
    if len(_CALLS) >= RATE_LIMIT:
        sleep_for = RATE_WINDOW - (now - _CALLS[0])
        if sleep_for > 0:
            time.sleep(sleep_for)
        now = time.monotonic()
        while _CALLS and now - _CALLS[0] >= RATE_WINDOW:
            _CALLS.popleft()
    _CALLS.append(time.monotonic())


def _get(path: str, allow_empty: bool = False, **params: Any) -> list[Any]:
    """One GET against the public API, returning the `data` array.

    Raises OKXError rather than returning empty data, so a typo in an
    instrument id fails loudly at the source instead of looking like a quiet
    market.

    `allow_empty` is for paging only. When walking backwards through history,
    an empty page is the correct answer — it means the exchange has no older
    data — and only the caller knows which of the two situations it is in.
    """
    _throttle()
    r = requests.get(f"{BASE}{path}", params=params or None,
                     headers={"Accept": "application/json"}, timeout=30)
    r.raise_for_status()

    try:
        payload = r.json()
    except ValueError as e:
        raise OKXError("?", f"response was not JSON: {e}", path) from e
    if not isinstance(payload, dict):
        raise OKXError("?", f"expected a JSON object, got {type(payload).__name__}", path)

    code = str(payload.get("code", ""))
    if code != "0":
        raise OKXError(code or "?", str(payload.get("msg", "")), path)

    data = payload.get("data")
    if not isinstance(data, list):
        raise OKXError(code, f"expected a data array, got {type(data).__name__}", path)
    if not data and not allow_empty:
        raise OKXError(code, "response carried code 0 but no data", path)
    return data


def _f(value: Any) -> float:
    """Parse one OKX string field.

    Every numeric field arrives as a string, and a few arrive as `""` — real
    listed instruments that have never printed a trade do this. Converting
    those to 0.0 would put a zero price into the metrics, so callers check for
    None and skip the row.
    """
    if value is None or value == "":
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _rows_to_candles(rows: list[Any], inst_id: str, endpoint: str
                     ) -> list[dict[str, float]]:
    """Shared parsing: drop the forming candle, convert, reverse to oldest-first."""
    out: list[dict[str, float]] = []
    for row in rows:
        # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            raise OKXError("0", f"malformed candle for {inst_id}: {row!r}", endpoint)
        if str(row[8]) == "0":
            continue                      # still forming; its close will change
        out.append({
            "ts": _f(row[0]), "open": _f(row[1]), "high": _f(row[2]),
            "low": _f(row[3]), "close": _f(row[4]), "volume": _f(row[5]),
        })
    out.reverse()                         # OKX sends newest first
    for i in range(1, len(out)):
        if out[i]["ts"] <= out[i - 1]["ts"]:
            raise OKXError("0",
                           f"candles for {inst_id} are not strictly oldest-first "
                           f"after reversing (index {i}: {out[i-1]['ts']:.0f} then "
                           f"{out[i]['ts']:.0f}) — the API's ordering may have changed",
                           endpoint)
    return out


def bar_ms(bar: str) -> int:
    """Milliseconds per bar. No default, for the same reason as bars_per_day."""
    try:
        return BAR_MS[bar]
    except KeyError:
        raise ValueError(
            f"unknown bar interval {bar!r}; known: {', '.join(sorted(BAR_MS))}"
        ) from None


def find_gaps(candles: list[dict[str, float]], bar: str
              ) -> list[tuple[float, float, int]]:
    """Every hole in a series, as (before_ts, after_ts, missing_bars).

    Empty means the series is contiguous. A gap is not necessarily a bug — an
    exchange outage or a listing halt makes a real one — but it must be visible,
    because every statistic downstream assumes evenly spaced observations.
    """
    step = bar_ms(bar)
    gaps = []
    for i in range(1, len(candles)):
        delta = candles[i]["ts"] - candles[i - 1]["ts"]
        if delta != step:
            gaps.append((candles[i - 1]["ts"], candles[i]["ts"],
                         int(delta / step) - 1))
    return gaps


def get_candles(inst_id: str, bar: str = "1H", limit: int = 300) -> list[dict[str, float]]:
    """OHLCV candles for one instrument, OLDEST FIRST, as floats.

        >>> get_candles("BTC-USDT", "1H", 300)[0]
        {'ts': 1787781600000.0, 'open': 78727.4, ..., 'volume': 136.89053875}

    `limit` is what is asked of OKX, not what comes back: the still-forming
    candle is dropped, so a request for 300 normally yields 299. `rubric.assess`
    needs 120 bars minimum — ask for at least 121.

    `ts` is milliseconds since the epoch, UTC, and marks the candle's OPEN.

    Raises OKXError for an unknown instrument or a bad interval, and ValueError
    for a limit above the API's silent 300 cap.
    """
    if limit < 1 or limit > MAX_CANDLES:
        raise ValueError(
            f"limit must be 1..{MAX_CANDLES} — OKX caps this endpoint at "
            f"{MAX_CANDLES} and truncates silently, so asking for {limit} would "
            f"hand you {MAX_CANDLES} bars while you believed you had {limit}. "
            f"Page with the `after` parameter if you need more history.")

    rows = _get("/market/candles", instId=inst_id, bar=bar, limit=limit)
    _stamp("candles")
    # Trust but verify the ordering: if OKX ever stopped sending newest-first,
    # reversing would hand metrics.py a backwards series — confident, wrong
    # numbers rather than an error. `_rows_to_candles` checks.
    return _rows_to_candles(rows, inst_id, "/market/candles")


def get_history_candles(inst_id: str, bar: str = "1H", limit: int = MAX_HISTORY,
                        after: Optional[float] = None) -> list[dict[str, float]]:
    """One page of `/market/history-candles`, oldest-first.

    `after` means "records older than this timestamp", which is how OKX pages
    BACKWARDS. It is the opposite of what the word suggests and is the single
    easiest thing to get wrong here.
    """
    if limit < 1 or limit > MAX_HISTORY:
        raise ValueError(f"limit must be 1..{MAX_HISTORY} for history-candles")
    params: dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": limit}
    if after is not None:
        params["after"] = int(after)
    # allow_empty: running out of history is how paging is meant to end.
    rows = _get("/market/history-candles", allow_empty=True, **params)
    return _rows_to_candles(rows, inst_id, "/market/history-candles")


def _cache_path(inst_id: str, bar: str, cache_dir: Path) -> Path:
    return Path(cache_dir) / f"{inst_id}_{bar}.json"


def _load_cache(path: Path) -> list[dict[str, float]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        out = data.get("candles", []) if isinstance(data, dict) else data
        return [c for c in out if isinstance(c, dict) and "ts" in c]
    except (ValueError, TypeError, json.JSONDecodeError):
        return []          # a corrupt cache is refetched, never trusted


def _save_cache(path: Path, inst_id: str, bar: str,
                candles: list[dict[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"inst_id": inst_id, "bar": bar, "n": len(candles),
               "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "candles": candles}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _merge(*series: list[dict[str, float]]) -> tuple[list[dict[str, float]], list[float]]:
    """Combine overlapping series into one oldest-first list.

    Returns (merged, conflicts). The two endpoints overlap by design — both
    start at "now" — so the overlap is the join's own integrity check: the same
    timestamp fetched from both must carry the same OHLCV. If it does not,
    something is wrong at the source and the caller is told rather than handed
    a series that is quietly half from each.
    """
    merged: dict[float, dict[str, float]] = {}
    conflicts: list[float] = []
    for s in series:
        for c in s:
            ts = c["ts"]
            prev = merged.get(ts)
            if prev is not None and any(prev[k] != c[k] for k in
                                        ("open", "high", "low", "close")):
                conflicts.append(ts)
            merged[ts] = c
    return [merged[k] for k in sorted(merged)], sorted(set(conflicts))


def get_history(inst_id: str, bar: str = "1H", start_ts: Optional[float] = None,
                *, cache_dir: Path = CANDLE_CACHE_DIR, use_cache: bool = True,
                allow_gaps: bool = False, max_pages: int = 2000
                ) -> list[dict[str, float]]:
    """Deep history as one continuous oldest-first series.

        >>> two_years = 2 * 365 * 24 * 3_600_000
        >>> get_history("BTC-USDT", "1H", start_ts=now_ms() - two_years)

    Uses `/market/candles` for the recent window and `/market/history-candles`
    beyond it, and stitches them. Each page is requested with one bar of
    deliberate overlap, because a join that merely abuts cannot be checked:
    contiguity would prove the timestamps line up while saying nothing about
    whether the two sources agree on the bar where they meet. A timestamp
    present in both must carry the same OHLC or this raises.

    Paging stops at `start_ts`, at an empty page, or at `max_pages`.

    GAPS. The result is proved contiguous before it is returned: every adjacent
    pair must be exactly one bar apart. A hole would not error downstream, it
    would silently shift every windowed statistic — `metrics.hurst` and
    `metrics.adx` both assume evenly spaced observations. Pass `allow_gaps=True`
    to accept a series with real holes in it (an exchange outage does make
    them); the gaps are then still discoverable with `find_gaps`.

    CACHE. Written to `data/candles/<inst>_<bar>.json` and topped up
    incrementally, so a second run fetches only what is new. Delete the file to
    force a full refetch.
    """
    step = bar_ms(bar)
    path = _cache_path(inst_id, bar, cache_dir)
    cached = _load_cache(path) if use_cache else []

    # Always refresh the recent end: the cache's newest bar is stale the moment
    # it is written, and the newest bar of any fetch may have been forming.
    recent = get_candles(inst_id, bar, limit=MAX_CANDLES)

    have, conflicts = _merge(cached, recent)
    if conflicts:
        raise OKXError("0",
                       f"{inst_id} {bar}: {len(conflicts)} timestamp(s) disagree "
                       f"between the cache and the live endpoint (first at "
                       f"{conflicts[0]:.0f}) — the cache is stale or corrupt; "
                       f"delete {path} and refetch", "/market/history-candles")

    oldest = have[0]["ts"] if have else None
    pages = 0
    while pages < max_pages:
        if start_ts is not None and oldest is not None and oldest <= start_ts:
            break
        # Ask for ONE BAR of deliberate overlap (`after` means "older than",
        # so oldest + one step re-includes the bar already held). Without
        # this the two endpoints abut exactly and the join is unverifiable —
        # contiguity would prove the timestamps line up, but nothing would
        # prove the two sources agree about the bar where they meet. One
        # redundant candle per page buys that check.
        after_param = None if oldest is None else oldest + step
        page = get_history_candles(inst_id, bar, limit=MAX_HISTORY,
                                   after=after_param)
        if not page:
            break                        # the exchange has no more history
        pages += 1
        have, conflicts = _merge(have, page)
        if conflicts:
            raise OKXError("0",
                           f"{inst_id} {bar}: {len(conflicts)} overlapping "
                           f"timestamp(s) disagree between /market/candles and "
                           f"/market/history-candles (first at {conflicts[0]:.0f}) "
                           f"— refusing to stitch an inconsistent series",
                           "/market/history-candles")
        new_oldest = have[0]["ts"]
        if new_oldest == oldest:
            break                        # no progress; the exchange is done
        oldest = new_oldest

    # CACHE EVERYTHING FETCHED, RETURN ONLY WHAT WAS ASKED FOR.
    #
    # These two must happen in this order. Trimming before the save would make
    # a narrow request DESTROY a wide cache: `get_history(start_ts=now - 365d)`
    # on a pair holding eight years of history would write back one year and
    # silently discard the rest. That happened here: the daily
    # scan asks for its 365-day statistics window, and it truncated twenty
    # pairs from 30-75k bars down to 8 759 before this was caught.
    #
    # The cache is a superset of any one caller's window, and no caller's
    # window should shrink it.
    if use_cache and have:
        _save_cache(path, inst_id, bar, have)

    if start_ts is not None:
        have = [c for c in have if c["ts"] >= start_ts]

    # --- the series must be contiguous and unique --------------------------
    for i in range(1, len(have)):
        if have[i]["ts"] == have[i - 1]["ts"]:
            raise OKXError("0", f"{inst_id} {bar}: duplicate timestamp "
                                f"{have[i]['ts']:.0f} survived the merge",
                           "/market/history-candles")
    gaps = find_gaps(have, bar)
    if gaps and not allow_gaps:
        first = gaps[0]
        raise OKXError("0",
                       f"{inst_id} {bar}: {len(gaps)} gap(s) in the stitched "
                       f"series, first between {first[0]:.0f} and {first[1]:.0f} "
                       f"({first[2]} bar(s) missing). Every windowed statistic "
                       f"assumes evenly spaced bars, so this is refused rather "
                       f"than returned. Pass allow_gaps=True to accept it.",
                       "/market/history-candles")
    return have


# ------------------------------------------------------- authenticated: fees

class MissingCredentials(RuntimeError):
    """No API key configured. Never a reason to assume a cheap fee tier."""


def _sign(timestamp: str, method: str, request_path: str, body: str,
          secret: str) -> str:
    """OKX request signature: base64(HMAC-SHA256(secret, prehash)).

    The prehash string is timestamp + METHOD + path + body, where `path`
    INCLUDES the query string. Signing the path without its query is the
    classic way to get a 401 that looks like a bad key.
    """
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _auth_headers(method: str, request_path: str, body: str = "") -> dict[str, str]:
    key = os.getenv("OKX_API_KEY", "")
    secret = os.getenv("OKX_API_SECRET", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    if not (key and secret and passphrase):
        raise MissingCredentials(
            "OKX_API_KEY, OKX_API_SECRET and OKX_PASSPHRASE must all be set to "
            "read your fee tier. A READ-ONLY key is enough — this never places "
            "an order.")
    # OKX wants milliseconds and a literal Z, which datetime.isoformat() does
    # not produce; strftime it explicitly.
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    headers = {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": _sign(ts, method, request_path, body, secret),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    if os.getenv("OKX_SIMULATED", "").strip() in ("1", "true", "True"):
        headers["x-simulated-trading"] = "1"
    return headers


def get_trade_fee(inst_id: str = "BTC-USDT",
                  inst_type: str = "SPOT") -> dict[str, Any]:
    """This account's ACTUAL maker and taker rates, as positive fractions.

        >>> get_trade_fee("BTC-USDT")
        {'maker': 0.0008, 'taker': 0.001, 'level': 'Lv1', 'inst_id': 'BTC-USDT', ...}

    Authenticated, but a READ-ONLY key is enough — this endpoint reads a rate
    and can never place an order. Set OKX_API_KEY, OKX_API_SECRET and
    OKX_PASSPHRASE.

    OKX returns these rates NEGATIVE ("-0.0008") because they are charges
    against the account; a rebate would be positive. Both are returned here as positive
    magnitudes, because `FeeModel` is documented in magnitudes and a sign error
    would turn a cost into income and make every grid look profitable.

    Why this exists: `OKX_SPOT_MAKER` is the base VIP tier on the global
    exchange. An EEA-regulated account pays 0.200%/0.350% — two and a half
    times as much — and at that tier the minimum viable grid spacing goes from
    0.64% to 1.62%. A system that assumes the cheap tier proposes grids that
    cannot pay for themselves.
    """
    path = f"/api/v5/account/trade-fee?instType={inst_type}&instId={inst_id}"
    _throttle()
    r = requests.get(f"https://www.okx.com{path}",
                     headers=_auth_headers("GET", path), timeout=30)
    r.raise_for_status()
    payload = r.json()
    code = str(payload.get("code", ""))
    if code != "0":
        raise OKXError(code or "?", str(payload.get("msg", "")),
                       "/account/trade-fee")
    data = payload.get("data") or []
    if not data:
        raise OKXError(code, "no fee data returned", "/account/trade-fee")

    row = data[0]
    maker, taker = abs(_f(row.get("maker"))), abs(_f(row.get("taker")))
    if maker != maker or taker != taker:          # NaN from a blank field
        raise OKXError(code, f"unparseable fee rates: {row!r}",
                       "/account/trade-fee")
    _stamp("fees")
    return {
        "maker": maker, "taker": taker,
        "level": str(row.get("level", "")),
        "inst_type": str(row.get("instType", inst_type)),
        "inst_id": inst_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@dataclass(frozen=True)
class Instrument:
    """The exchange's own limits on what constitutes a placeable order.

    `min_size` and `lot_size` are in BASE units (0.00001 BTC, 10 DOGE), not
    quote. Converting needs a price, which is why `sizing.py` does it rather
    than this module.
    """

    inst_id: str
    min_size: float          # minSz — smaller and the exchange rejects the order
    lot_size: float          # lotSz — size must be a multiple of this
    tick_size: float         # tickSz — price must be a multiple of this
    base: str = ""
    quote: str = ""
    state: str = ""

    @property
    def is_live(self) -> bool:
        return self.state == "live"

    @property
    def state_problem(self) -> Optional[str]:
        """Why this instrument cannot be traded, if it cannot.

        OKX reports `live`, `suspend`, `preopen`, `expired` and `test`. Only
        `live` is tradeable; everything else either rejects orders outright or
        — worse, in the pre-open case — accepts them into a book that is not
        yet matching.
        """
        if self.state == "live":
            return None
        reasons = {
            "suspend": "trading is suspended",
            "preopen": "the market has not opened yet — orders would rest in a "
                       "book that is not matching",
            "expired": "the instrument has expired",
            "test": "this is a test instrument, not a real market",
        }
        return (f"{self.inst_id} is not live on the exchange: "
                f"{reasons.get(self.state, f'state is {self.state!r}')}")


_INSTRUMENTS: dict[str, tuple[float, dict[str, Instrument]]] = {}
INSTRUMENT_TTL_SECONDS = 86_400

#: When each source was last successfully fetched, stamped AT THE FETCH.
#: Never derived from a file mtime — see freshness.py for why that matters.
_FETCHED_AT: dict[str, float] = {}


def fetched_at(source: str) -> Optional[float]:
    """Unix time of the last successful fetch of `source`, or None."""
    return _FETCHED_AT.get(source)


def _stamp(source: str, when: Optional[float] = None) -> None:
    _FETCHED_AT[source] = time.time() if when is None else when


def clear_fetch_stamps() -> None:
    _FETCHED_AT.clear()


def clear_instrument_cache() -> None:
    _INSTRUMENTS.clear()


def get_instruments(inst_type: str = "SPOT", *, use_cache: bool = True,
                    now: Optional[float] = None) -> dict[str, Instrument]:
    """Every instrument's minimum order size, lot step and tick step.

    Public endpoint, no key. Cached for a day: these change when OKX relists
    something, which is rare, but "rare" is not "never" and a stale minSz means
    every order on that instrument is rejected.

    This answers a question the system could not previously ask: is the
    order about to be placed large enough to exist? `MIN_QUOTE_PER_LEVEL`
    was a flat 10 USDT guess standing in for it. Mostly right, and wrong for
    ZEC-USDT, whose minimum order is 12.25 USDT.
    """
    now = time.time() if now is None else now
    hit = _INSTRUMENTS.get(inst_type)
    if use_cache and hit and (now - hit[0]) < INSTRUMENT_TTL_SECONDS:
        return hit[1]

    rows = _get("/public/instruments", instType=inst_type)
    out: dict[str, Instrument] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        inst_id = r.get("instId")
        if not inst_id:
            continue
        min_size, lot, tick = _f(r.get("minSz")), _f(r.get("lotSz")), _f(r.get("tickSz"))
        if min_size != min_size or min_size <= 0:
            continue                      # NaN or nonsense: unusable, so omitted
        out[inst_id] = Instrument(
            inst_id=inst_id, min_size=min_size,
            lot_size=lot if lot == lot and lot > 0 else min_size,
            tick_size=tick if tick == tick and tick > 0 else 0.0,
            base=str(r.get("baseCcy", "")), quote=str(r.get("quoteCcy", "")),
            state=str(r.get("state", "")))
    if out:
        _INSTRUMENTS[inst_type] = (now, out)
        _stamp("instruments", now)
    return out


def get_instrument(inst_id: str, inst_type: str = "SPOT") -> Optional[Instrument]:
    return get_instruments(inst_type).get(inst_id)


def get_spot_tickers() -> dict[str, dict[str, float]]:
    """Every SPOT instrument's last price and 24h volume, keyed by instId.

        >>> get_spot_tickers()["BTC-USDT"]
        {'last': 78782.8, 'quote_volume_24h': 254812345.6, 'base_volume_24h': 3234.5}

    `quote_volume_24h` is `volCcy24h` — turnover in the QUOTE currency, i.e.
    USDT for a -USDT pair. That is the one the rubric's liquidity gate wants;
    `vol24h` is denominated in the base coin, so it is not comparable across
    instruments (478m DOGE and 3k BTC are not two different liquidity tiers).

    Instruments with no last price — listed but never traded, and there are
    usually a couple — are omitted rather than reported at zero.
    """
    rows = _get("/market/tickers", instType="SPOT")

    out: dict[str, dict[str, float]] = {}
    for t in rows:
        if not isinstance(t, dict):
            continue
        inst_id = t.get("instId")
        if not inst_id:
            continue
        last = _f(t.get("last"))
        if last != last or last <= 0:     # NaN, blank, or nonsense
            continue
        out[inst_id] = {
            "last": last,
            "quote_volume_24h": max(0.0, _f(t.get("volCcy24h")) or 0.0),
            "base_volume_24h": max(0.0, _f(t.get("vol24h")) or 0.0),
        }
    _stamp("tickers")
    return out


def bars_per_day(bar: str) -> float:
    """Candles per 24h for `bar`. No default — an unknown interval raises.

    Guessing here would be worse than failing: `rubric.assess` multiplies this
    straight into the expected-cycles-per-day estimate that decides whether a
    grid is worth running.
    """
    try:
        return BARS_PER_DAY[bar]
    except KeyError:
        raise ValueError(
            f"unknown bar interval {bar!r} — no default is provided on purpose, "
            f"because a wrong bars-per-day silently mis-scores every candidate. "
            f"Known: {', '.join(sorted(BARS_PER_DAY))}. Add a new interval to "
            f"BARS_PER_DAY deliberately.") from None


def check() -> dict[str, Any]:
    """Smoke test used by `cli check`."""
    try:
        candles = get_candles("BTC-USDT", "1H", limit=5)
        tickers = get_spot_tickers()
        return {
            "ok": bool(candles) and bool(tickers),
            "n_candles": len(candles),
            "last_close": candles[-1]["close"] if candles else None,
            "n_spot_instruments": len(tickers),
            "btc_usdt_quote_vol_24h": tickers.get("BTC-USDT", {}).get("quote_volume_24h"),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
