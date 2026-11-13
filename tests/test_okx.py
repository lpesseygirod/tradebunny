"""Tests for the OKX public market-data client.

Nothing here touches the network. `tests/fixtures/okx_*.json` are real captured
responses — a 300-candle BTC-USDT 1H page (whose newest bar was still forming
when it was captured, which is the case that matters) and a trimmed slice of a
live SPOT tickers response, including one genuinely blank-priced instrument.

The transport is stubbed at `requests.get`, so what is under test is exactly
what runs in production: the same parsing, the same error handling.
"""

import json
import pathlib

import pytest

from grid import okx


FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CANDLES = json.loads((FIXTURES / "okx_candles_1h.json").read_text())
TICKERS = json.loads((FIXTURES / "okx_tickers_spot.json").read_text())


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not JSON")
        return self._payload


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any un-stubbed request is a bug in the test, not a slow test."""
    def boom(*a, **k):
        raise AssertionError("a test tried to reach the network")
    monkeypatch.setattr(okx.requests, "get", boom)
    okx._CALLS.clear()


def serve(monkeypatch, payload, spy=None):
    def fake_get(url, params=None, headers=None, timeout=None):
        if spy is not None:
            spy.append({"url": url, "params": params or {}})
        return FakeResponse(payload)
    monkeypatch.setattr(okx.requests, "get", fake_get)


# --------------------------------------------------------------- fixture sanity

class TestTheFixtureIsWhatWeThinkItIs:
    """If these fail, the tests below are testing nothing."""

    def test_it_is_a_real_success_envelope(self):
        assert CANDLES["code"] == "0"
        assert len(CANDLES["data"]) == 300

    def test_every_field_arrives_as_a_string(self):
        assert all(isinstance(v, str) for row in CANDLES["data"] for v in row)

    def test_it_contains_exactly_one_still_forming_candle(self):
        assert sum(1 for r in CANDLES["data"] if r[8] == "0") == 1

    def test_the_unconfirmed_candle_is_the_newest_one(self):
        assert CANDLES["data"][0][8] == "0"

    def test_the_raw_order_is_newest_first(self):
        ts = [int(r[0]) for r in CANDLES["data"]]
        assert ts == sorted(ts, reverse=True)


# ------------------------------------------------------------------- candles

class TestGetCandles:

    def test_strings_become_floats(self, monkeypatch):
        serve(monkeypatch, CANDLES)
        c = okx.get_candles("BTC-USDT")[0]
        assert set(c) == {"ts", "open", "high", "low", "close", "volume"}
        assert all(isinstance(v, float) for v in c.values())

    def test_values_match_the_raw_row(self, monkeypatch):
        serve(monkeypatch, CANDLES)
        oldest_raw = CANDLES["data"][-1]          # last of a newest-first page
        c = okx.get_candles("BTC-USDT")[0]        # first of an oldest-first list
        assert c["ts"] == float(oldest_raw[0])
        assert c["open"] == float(oldest_raw[1])
        assert c["high"] == float(oldest_raw[2])
        assert c["low"] == float(oldest_raw[3])
        assert c["close"] == float(oldest_raw[4])
        assert c["volume"] == float(oldest_raw[5])

    def test_result_is_oldest_first(self, monkeypatch):
        serve(monkeypatch, CANDLES)
        ts = [c["ts"] for c in okx.get_candles("BTC-USDT")]
        assert ts == sorted(ts)

    def test_ordering_is_strict_no_duplicate_timestamps(self, monkeypatch):
        serve(monkeypatch, CANDLES)
        ts = [c["ts"] for c in okx.get_candles("BTC-USDT")]
        assert len(set(ts)) == len(ts)

    def test_unconfirmed_candle_is_dropped(self, monkeypatch):
        serve(monkeypatch, CANDLES)
        got = okx.get_candles("BTC-USDT")
        assert len(got) == 299                     # 300 served, one still forming
        forming_ts = float(CANDLES["data"][0][0])
        assert forming_ts not in {c["ts"] for c in got}

    def test_the_last_bar_returned_is_the_newest_confirmed_one(self, monkeypatch):
        serve(monkeypatch, CANDLES)
        got = okx.get_candles("BTC-USDT")
        assert got[-1]["ts"] == float(CANDLES["data"][1][0])
        assert got[-1]["close"] == float(CANDLES["data"][1][4])

    def test_a_page_of_entirely_unconfirmed_candles_comes_back_empty(self, monkeypatch):
        """Filtering can legitimately empty a non-empty response.

        Only the API returning no data at all is an error (see
        test_code_zero_with_no_data_is_not_swallowed). Here OKX answered with
        rows and every one was still forming, so there is genuinely no settled
        history — `rubric.assess` rejects that at its "need 120+ bars" gate,
        which is a clearer message than anything this layer could raise.
        """
        rows = [list(r[:8]) + ["0"] for r in CANDLES["data"][:5]]
        serve(monkeypatch, {"code": "0", "msg": "", "data": rows})
        assert okx.get_candles("BTC-USDT", limit=5) == []

    def test_confirm_flag_is_read_positionally_not_by_truthiness(self, monkeypatch):
        """confirm is the string "1"/"0"; "0" is truthy in Python."""
        rows = [
            ["1700000007200000", "2", "3", "1", "2", "10", "0", "0", "0"],
            ["1700000003600000", "1", "2", "1", "1", "10", "0", "0", "1"],
        ]
        serve(monkeypatch, {"code": "0", "msg": "", "data": rows})
        got = okx.get_candles("X-USDT", limit=2)
        assert [c["ts"] for c in got] == [1700000003600000.0]

    def test_it_asks_okx_for_what_the_caller_requested(self, monkeypatch):
        spy = []
        serve(monkeypatch, CANDLES, spy)
        okx.get_candles("ETH-USDT", "4H", limit=200)
        assert spy[0]["url"] == "https://www.okx.com/api/v5/market/candles"
        assert spy[0]["params"] == {"instId": "ETH-USDT", "bar": "4H", "limit": 200}

    def test_bar_defaults_to_1h(self, monkeypatch):
        spy = []
        serve(monkeypatch, CANDLES, spy)
        okx.get_candles("BTC-USDT")
        assert spy[0]["params"]["bar"] == "1H"


class TestCandleLimitGuard:
    """OKX caps at 300 silently. A caller must never believe it got more."""

    def test_over_the_cap_is_refused(self):
        with pytest.raises(ValueError, match="300"):
            okx.get_candles("BTC-USDT", limit=500)

    def test_exactly_the_cap_is_allowed(self, monkeypatch):
        serve(monkeypatch, CANDLES)
        assert okx.get_candles("BTC-USDT", limit=300)

    def test_zero_and_negative_are_refused(self):
        for bad in (0, -1):
            with pytest.raises(ValueError):
                okx.get_candles("BTC-USDT", limit=bad)

    def test_the_guard_runs_before_any_request(self, monkeypatch):
        """no_network is active: reaching requests.get here would raise a
        different error than the expected ValueError."""
        with pytest.raises(ValueError):
            okx.get_candles("BTC-USDT", limit=9999)


class TestMalformedCandles:

    def test_a_short_row_raises_rather_than_being_padded(self, monkeypatch):
        serve(monkeypatch, {"code": "0", "msg": "", "data": [["1700000000000", "1"]]})
        with pytest.raises(okx.OKXError, match="malformed"):
            okx.get_candles("BTC-USDT", limit=1)

    def test_ordering_violation_is_caught(self, monkeypatch):
        """If OKX ever stops sending newest-first, reversing would silently
        hand metrics.py a backwards series."""
        rows = [
            ["1700000003600000", "1", "2", "1", "1", "10", "0", "0", "1"],
            ["1700000007200000", "2", "3", "1", "2", "10", "0", "0", "1"],
        ]                                          # oldest-first: wrong way round
        serve(monkeypatch, {"code": "0", "msg": "", "data": rows})
        with pytest.raises(okx.OKXError, match="oldest-first"):
            okx.get_candles("X-USDT", limit=2)


# ------------------------------------------------------------------- errors

class TestErrorEnvelopes:
    """OKX reports failures as HTTP 200 with a non-zero code."""

    def test_unknown_instrument_raises(self, monkeypatch):
        serve(monkeypatch, {"code": "51001", "data": [],
                            "msg": "Instrument ID, Instrument ID code, or Spread ID "
                                   "doesn't exist."})
        with pytest.raises(okx.OKXError) as e:
            okx.get_candles("NOTREAL-USDT")
        assert e.value.code == "51001"
        assert "doesn't exist" in str(e.value)

    def test_bad_bar_interval_raises(self, monkeypatch):
        serve(monkeypatch, {"code": "51000", "data": [], "msg": "Parameter bar error"})
        with pytest.raises(okx.OKXError) as e:
            okx.get_candles("BTC-USDT", "7H")
        assert e.value.code == "51000"

    def test_code_zero_with_no_data_is_not_swallowed(self, monkeypatch):
        serve(monkeypatch, {"code": "0", "msg": "", "data": []})
        with pytest.raises(okx.OKXError, match="no data"):
            okx.get_candles("BTC-USDT")

    def test_tickers_error_also_raises(self, monkeypatch):
        serve(monkeypatch, {"code": "50011", "data": [], "msg": "Requests too frequent"})
        with pytest.raises(okx.OKXError) as e:
            okx.get_spot_tickers()
        assert e.value.code == "50011"

    def test_non_json_body_raises_okx_error(self, monkeypatch):
        serve(monkeypatch, "<html>502 Bad Gateway</html>")
        with pytest.raises(okx.OKXError, match="not JSON"):
            okx.get_candles("BTC-USDT")

    def test_data_of_the_wrong_shape_raises(self, monkeypatch):
        serve(monkeypatch, {"code": "0", "msg": "", "data": {"nope": 1}})
        with pytest.raises(okx.OKXError, match="data array"):
            okx.get_candles("BTC-USDT")

    def test_the_error_names_the_endpoint(self, monkeypatch):
        serve(monkeypatch, {"code": "51001", "data": [], "msg": "nope"})
        with pytest.raises(okx.OKXError) as e:
            okx.get_candles("X-USDT")
        assert e.value.endpoint == "/market/candles"


# ------------------------------------------------------------------ tickers

class TestGetSpotTickers:

    def test_keyed_by_inst_id_with_float_values(self, monkeypatch):
        serve(monkeypatch, TICKERS)
        got = okx.get_spot_tickers()
        assert "BTC-USDT" in got
        assert all(isinstance(v, float) for v in got["BTC-USDT"].values())

    def test_last_price_matches_the_raw_string(self, monkeypatch):
        serve(monkeypatch, TICKERS)
        raw = {t["instId"]: t for t in TICKERS["data"]}["BTC-USDT"]
        assert okx.get_spot_tickers()["BTC-USDT"]["last"] == float(raw["last"])

    def test_quote_volume_comes_from_volccy24h_not_vol24h(self, monkeypatch):
        """vol24h is denominated in the base coin, so it is not comparable
        across instruments. The liquidity gate needs quote turnover."""
        serve(monkeypatch, TICKERS)
        raw = {t["instId"]: t for t in TICKERS["data"]}["DOGE-USDT"]
        got = okx.get_spot_tickers()["DOGE-USDT"]
        assert got["quote_volume_24h"] == float(raw["volCcy24h"])
        assert got["base_volume_24h"] == float(raw["vol24h"])
        assert got["quote_volume_24h"] != got["base_volume_24h"]

    def test_quote_volume_is_roughly_price_times_base_volume(self, monkeypatch):
        """A sanity check that the two volume fields have not been swapped."""
        serve(monkeypatch, TICKERS)
        t = okx.get_spot_tickers()["DOGE-USDT"]
        assert t["quote_volume_24h"] == pytest.approx(
            t["last"] * t["base_volume_24h"], rel=0.05)

    def test_blank_priced_instruments_are_omitted_not_zeroed(self, monkeypatch):
        """A real listed-but-never-traded pair. Reporting last=0.0 would put a
        zero price into the metrics."""
        serve(monkeypatch, TICKERS)
        assert any(t["instId"] == "XINTW-USDT" and t["last"] == ""
                   for t in TICKERS["data"])
        assert "XINTW-USDT" not in okx.get_spot_tickers()

    def test_every_returned_price_is_positive(self, monkeypatch):
        serve(monkeypatch, TICKERS)
        assert all(v["last"] > 0 for v in okx.get_spot_tickers().values())

    def test_it_requests_spot_only(self, monkeypatch):
        spy = []
        serve(monkeypatch, TICKERS, spy)
        okx.get_spot_tickers()
        assert spy[0]["url"] == "https://www.okx.com/api/v5/market/tickers"
        assert spy[0]["params"] == {"instType": "SPOT"}

    def test_non_usdt_quotes_survive(self, monkeypatch):
        serve(monkeypatch, TICKERS)
        assert "BTC-USDC" in okx.get_spot_tickers()


# --------------------------------------------------------------- bars_per_day

class TestBarsPerDay:

    @pytest.mark.parametrize("bar,expected", [("1H", 24.0), ("4H", 6.0), ("1D", 1.0)])
    def test_known_intervals(self, bar, expected):
        assert okx.bars_per_day(bar) == expected

    def test_returns_a_float(self):
        assert isinstance(okx.bars_per_day("1D"), float)

    def test_unknown_interval_raises_rather_than_defaulting(self):
        """rubric.assess has no default for bars_per_day on purpose; this must
        not quietly reintroduce one."""
        with pytest.raises(ValueError, match="no default"):
            okx.bars_per_day("15m")

    def test_it_is_case_sensitive_like_the_api(self):
        with pytest.raises(ValueError):
            okx.bars_per_day("1h")

    def test_each_interval_is_consistent_with_the_next(self):
        assert okx.bars_per_day("1H") == 4 * okx.bars_per_day("4H")
        assert okx.bars_per_day("4H") == 6 * okx.bars_per_day("1D")


# ------------------------------------------------------------------ throttle

class TestRateLimit:

    def test_a_burst_within_the_allowance_does_not_sleep(self, monkeypatch):
        slept = []
        monkeypatch.setattr(okx.time, "sleep", lambda s: slept.append(s))
        for _ in range(okx.RATE_LIMIT):
            okx._throttle()
        assert slept == []

    def test_exceeding_the_allowance_sleeps(self, monkeypatch):
        slept = []
        monkeypatch.setattr(okx.time, "sleep", lambda s: slept.append(s))
        for _ in range(okx.RATE_LIMIT + 1):
            okx._throttle()
        assert len(slept) == 1
        assert 0 < slept[0] <= okx.RATE_WINDOW

    def test_the_documented_allowance_is_enforced(self):
        assert (okx.RATE_LIMIT, okx.RATE_WINDOW) == (20, 2.0)


# --------------------------------------------------------------------- check

class TestCheck:

    def test_reports_ok_when_both_endpoints_answer(self, monkeypatch):
        def fake_get(url, params=None, headers=None, timeout=None):
            return FakeResponse(CANDLES if "candles" in url else TICKERS)
        monkeypatch.setattr(okx.requests, "get", fake_get)
        got = okx.check()
        assert got["ok"] is True
        assert got["n_spot_instruments"] == 8      # 9 served, one blank-priced
        assert got["last_close"] > 0
        assert got["checked_at"]

    def test_failure_is_reported_not_raised(self, monkeypatch):
        serve(monkeypatch, {"code": "51001", "data": [], "msg": "nope"})
        got = okx.check()
        assert got["ok"] is False
        assert "OKXError" in got["error"]


# --------------------------------------------------------------- deep history

HOUR = 3_600_000


def synth(n, start_ts=1_700_000_000_000, step=HOUR, price=100.0, confirm="1"):
    """Newest-first rows, the way OKX sends them."""
    rows = []
    for i in range(n):
        ts = start_ts + i * step
        rows.append([str(ts), str(price), str(price + 1), str(price - 1),
                     str(price), "10", "0", "0", confirm])
    return list(reversed(rows))


class StubOKX:
    """Serves /market/candles and /market/history-candles from one synthetic
    series, honouring `after` (older than) exactly as OKX does."""

    def __init__(self, n_total=1000, recent=300, page=300, start_ts=1_700_000_000_000):
        self.all = sorted(synth(n_total, start_ts), key=lambda r: int(r[0]))
        self.recent, self.page = recent, page
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        self.calls.append((url.rsplit("/", 1)[-1], params.get("after")))
        after = params.get("after")
        pool = self.all if "history" in url else self.all[-self.recent:]
        if after is not None:
            pool = [r for r in pool if int(r[0]) < int(after)]
        limit = int(params.get("limit", 100))
        rows = list(reversed(pool))[:limit]
        return FakeResponse({"code": "0", "msg": "", "data": rows})


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "candles"


class TestHistoryCandles:

    def test_it_pages_older_with_after(self, monkeypatch):
        stub = StubOKX(n_total=1000)
        monkeypatch.setattr(okx.requests, "get", stub)
        first = okx.get_history_candles("BTC-USDT", "1H", limit=300)
        older = okx.get_history_candles("BTC-USDT", "1H", limit=300,
                                        after=first[0]["ts"])
        assert older[-1]["ts"] < first[0]["ts"]

    def test_the_documented_limit_is_300_not_100(self):
        assert okx.MAX_HISTORY == 300

    def test_it_refuses_above_the_cap(self):
        with pytest.raises(ValueError, match="300"):
            okx.get_history_candles("BTC-USDT", "1H", limit=500)

    def test_results_are_oldest_first(self, monkeypatch):
        monkeypatch.setattr(okx.requests, "get", StubOKX())
        got = okx.get_history_candles("BTC-USDT", "1H", limit=100)
        assert [c["ts"] for c in got] == sorted(c["ts"] for c in got)


class TestBarMs:

    @pytest.mark.parametrize("bar,ms", [("1H", 3_600_000), ("4H", 14_400_000),
                                        ("1D", 86_400_000)])
    def test_known_intervals(self, bar, ms):
        assert okx.bar_ms(bar) == ms

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            okx.bar_ms("15m")

    def test_it_agrees_with_bars_per_day(self):
        for bar in ("1H", "4H", "1D"):
            assert okx.bar_ms(bar) * okx.bars_per_day(bar) == 86_400_000


class TestFindGaps:

    def test_a_contiguous_series_has_none(self):
        c = [{"ts": float(1_700_000_000_000 + i * HOUR)} for i in range(10)]
        assert okx.find_gaps(c, "1H") == []

    def test_a_hole_is_found_and_measured(self):
        c = [{"ts": float(1_700_000_000_000 + i * HOUR)} for i in range(10)]
        del c[5]
        gaps = okx.find_gaps(c, "1H")
        assert len(gaps) == 1 and gaps[0][2] == 1

    def test_a_wider_hole_counts_more_bars(self):
        c = [{"ts": float(1_700_000_000_000 + i * HOUR)} for i in range(10)]
        del c[4:7]
        assert okx.find_gaps(c, "1H")[0][2] == 3


class TestGetHistory:

    def test_it_stitches_both_endpoints(self, monkeypatch, cache_dir):
        stub = StubOKX(n_total=1000, recent=300)
        monkeypatch.setattr(okx.requests, "get", stub)
        got = okx.get_history("BTC-USDT", "1H", cache_dir=cache_dir)
        assert len(got) == 1000
        assert {c for c, _ in stub.calls} == {"candles", "history-candles"}

    def test_the_result_is_contiguous(self, monkeypatch, cache_dir):
        monkeypatch.setattr(okx.requests, "get", StubOKX(n_total=1000))
        got = okx.get_history("BTC-USDT", "1H", cache_dir=cache_dir)
        assert okx.find_gaps(got, "1H") == []

    def test_no_duplicate_timestamps_at_the_join(self, monkeypatch, cache_dir):
        """The two endpoints overlap by design; the overlap must collapse."""
        monkeypatch.setattr(okx.requests, "get", StubOKX(n_total=1000))
        got = okx.get_history("BTC-USDT", "1H", cache_dir=cache_dir)
        ts = [c["ts"] for c in got]
        assert len(ts) == len(set(ts))

    def test_it_stops_at_start_ts(self, monkeypatch, cache_dir):
        stub = StubOKX(n_total=1000)
        monkeypatch.setattr(okx.requests, "get", stub)
        newest = int(stub.all[-1][0])
        got = okx.get_history("BTC-USDT", "1H", start_ts=newest - 100 * HOUR,
                              cache_dir=cache_dir)
        assert 100 <= len(got) <= 102
        assert min(c["ts"] for c in got) >= newest - 100 * HOUR

    def test_it_stops_when_the_exchange_runs_out(self, monkeypatch, cache_dir):
        monkeypatch.setattr(okx.requests, "get", StubOKX(n_total=400))
        got = okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)
        assert len(got) == 400

    def test_a_disagreeing_overlap_is_refused(self, monkeypatch, cache_dir):
        """If the same timestamp differs between endpoints, the stitch is a lie."""
        stub = StubOKX(n_total=600)

        def poisoned(url, params=None, headers=None, timeout=None):
            resp = stub(url, params, headers, timeout)
            if "history" in url:
                rows = resp._payload["data"]
                if rows:
                    rows[0] = list(rows[0])
                    rows[0][4] = "999999"       # different close, same ts
            return resp

        monkeypatch.setattr(okx.requests, "get", poisoned)
        with pytest.raises(okx.OKXError, match="disagree"):
            okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)

    def test_a_gap_is_refused_by_default(self, monkeypatch, cache_dir):
        stub = StubOKX(n_total=600)
        hole = stub.all[300][0]
        stub.all = [r for r in stub.all if r[0] != hole]
        monkeypatch.setattr(okx.requests, "get", stub)
        with pytest.raises(okx.OKXError, match="gap"):
            okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)

    def test_a_gap_can_be_accepted_knowingly(self, monkeypatch, cache_dir):
        stub = StubOKX(n_total=600)
        hole = stub.all[300][0]
        stub.all = [r for r in stub.all if r[0] != hole]
        monkeypatch.setattr(okx.requests, "get", stub)
        got = okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir,
                              allow_gaps=True)
        assert len(okx.find_gaps(got, "1H")) == 1

    def test_the_forming_candle_is_still_dropped(self, monkeypatch, cache_dir):
        stub = StubOKX(n_total=400)
        stub.all[-1] = list(stub.all[-1][:8]) + ["0"]
        newest_ts = float(stub.all[-1][0])
        monkeypatch.setattr(okx.requests, "get", stub)
        got = okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)
        assert newest_ts not in {c["ts"] for c in got}


class TestHistoryCache:

    def test_it_writes_a_cache_file(self, monkeypatch, cache_dir):
        monkeypatch.setattr(okx.requests, "get", StubOKX(n_total=600))
        okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)
        assert (cache_dir / "BTC-USDT_1H.json").exists()

    def test_a_second_run_refetches_far_less(self, monkeypatch, cache_dir):
        stub = StubOKX(n_total=2000)
        monkeypatch.setattr(okx.requests, "get", stub)
        okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)
        first = len(stub.calls)
        stub.calls.clear()
        okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)
        assert len(stub.calls) < first

    def test_the_cached_result_is_identical(self, monkeypatch, cache_dir):
        monkeypatch.setattr(okx.requests, "get", StubOKX(n_total=800))
        a = okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)
        b = okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)
        assert a == b

    def test_a_corrupt_cache_is_refetched_not_trusted(self, monkeypatch,
                                                      cache_dir):
        monkeypatch.setattr(okx.requests, "get", StubOKX(n_total=600))
        okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)
        (cache_dir / "BTC-USDT_1H.json").write_text("{not json")
        got = okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)
        assert len(got) == 600

    def test_use_cache_false_ignores_the_file(self, monkeypatch, cache_dir):
        monkeypatch.setattr(okx.requests, "get", StubOKX(n_total=600))
        okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)
        (cache_dir / "BTC-USDT_1H.json").write_text("{not json")
        got = okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir,
                              use_cache=False)
        assert len(got) == 600

    def test_a_stale_cache_that_disagrees_is_refused(self, monkeypatch,
                                                     cache_dir):
        """A cached bar whose close differs from the live one means the cache
        is wrong; silently preferring either would corrupt the series."""
        import json as _j
        monkeypatch.setattr(okx.requests, "get", StubOKX(n_total=600))
        okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)
        path = cache_dir / "BTC-USDT_1H.json"
        raw = _j.loads(path.read_text())
        raw["candles"][-1]["close"] = 123456.0
        path.write_text(_j.dumps(raw))
        with pytest.raises(okx.OKXError, match="disagree"):
            okx.get_history("BTC-USDT", "1H", start_ts=0, cache_dir=cache_dir)


# ------------------------------------------------------------ trade fee (auth)

class TestSigning:

    def test_the_prehash_is_timestamp_method_path_body(self, monkeypatch):
        """Signing the path WITHOUT its query string is the classic 401."""
        import base64, hashlib, hmac
        ts, path = "2020-12-08T09:08:57.715Z", "/api/v5/account/trade-fee?instType=SPOT"
        expected = base64.b64encode(hmac.new(
            b"sec", f"{ts}GET{path}".encode(), hashlib.sha256).digest()).decode()
        assert okx._sign(ts, "GET", path, "", "sec") == expected

    def test_the_method_is_upper_cased(self):
        assert okx._sign("t", "get", "/p", "", "s") == okx._sign("t", "GET", "/p", "", "s")

    def test_a_different_secret_gives_a_different_signature(self):
        assert okx._sign("t", "GET", "/p", "", "a") != okx._sign("t", "GET", "/p", "", "b")


class TestTradeFeeCredentials:

    def test_missing_credentials_raise_rather_than_assume(self, monkeypatch):
        """An unreadable fee tier must never be silently taken as the cheap one."""
        for var in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(okx.MissingCredentials):
            okx.get_trade_fee("BTC-USDT")

    def test_a_partial_credential_set_is_refused(self, monkeypatch):
        monkeypatch.setenv("OKX_API_KEY", "k")
        monkeypatch.delenv("OKX_API_SECRET", raising=False)
        monkeypatch.delenv("OKX_PASSPHRASE", raising=False)
        with pytest.raises(okx.MissingCredentials):
            okx.get_trade_fee("BTC-USDT")

    def test_the_headers_carry_every_required_field(self, monkeypatch):
        monkeypatch.setenv("OKX_API_KEY", "k")
        monkeypatch.setenv("OKX_API_SECRET", "s")
        monkeypatch.setenv("OKX_PASSPHRASE", "p")
        h = okx._auth_headers("GET", "/api/v5/account/trade-fee")
        assert {"OK-ACCESS-KEY", "OK-ACCESS-SIGN", "OK-ACCESS-TIMESTAMP",
                "OK-ACCESS-PASSPHRASE"} <= set(h)
        assert h["OK-ACCESS-TIMESTAMP"].endswith("Z")

    def test_simulated_trading_header_is_opt_in(self, monkeypatch):
        monkeypatch.setenv("OKX_API_KEY", "k")
        monkeypatch.setenv("OKX_API_SECRET", "s")
        monkeypatch.setenv("OKX_PASSPHRASE", "p")
        monkeypatch.delenv("OKX_SIMULATED", raising=False)
        assert "x-simulated-trading" not in okx._auth_headers("GET", "/p")
        monkeypatch.setenv("OKX_SIMULATED", "1")
        assert okx._auth_headers("GET", "/p")["x-simulated-trading"] == "1"


class TestTradeFee:

    @pytest.fixture(autouse=True)
    def creds(self, monkeypatch):
        monkeypatch.setenv("OKX_API_KEY", "k")
        monkeypatch.setenv("OKX_API_SECRET", "s")
        monkeypatch.setenv("OKX_PASSPHRASE", "p")

    def fee_payload(self, maker="-0.0008", taker="-0.001", level="Lv1"):
        return {"code": "0", "msg": "", "data": [
            {"category": "1", "instType": "SPOT", "level": level,
             "maker": maker, "taker": taker, "makerU": "", "takerU": "",
             "ts": "1700000000000"}]}

    def test_negative_rates_come_back_as_positive_magnitudes(self, monkeypatch):
        """OKX signs these negative because they are charges. A sign error here
        would turn a cost into income and make every grid look profitable."""
        serve(monkeypatch, self.fee_payload())
        got = okx.get_trade_fee("BTC-USDT")
        assert got["maker"] == 0.0008 and got["taker"] == 0.001

    def test_it_reports_the_tier(self, monkeypatch):
        serve(monkeypatch, self.fee_payload(level="VIP2"))
        assert okx.get_trade_fee("BTC-USDT")["level"] == "VIP2"

    def test_eea_rates_parse(self, monkeypatch):
        serve(monkeypatch, self.fee_payload(maker="-0.002", taker="-0.0035"))
        got = okx.get_trade_fee("BTC-USDT")
        assert (got["maker"], got["taker"]) == (0.002, 0.0035)

    def test_it_signs_the_path_with_its_query(self, monkeypatch):
        spy = []
        serve(monkeypatch, self.fee_payload(), spy)
        okx.get_trade_fee("ETH-USDT")
        assert "instId=ETH-USDT" in spy[0]["url"]

    def test_an_error_code_raises(self, monkeypatch):
        serve(monkeypatch, {"code": "50111", "msg": "Invalid API key", "data": []})
        with pytest.raises(okx.OKXError) as e:
            okx.get_trade_fee("BTC-USDT")
        assert e.value.code == "50111"

    def test_empty_data_raises(self, monkeypatch):
        serve(monkeypatch, {"code": "0", "msg": "", "data": []})
        with pytest.raises(okx.OKXError, match="no fee data"):
            okx.get_trade_fee("BTC-USDT")

    def test_blank_rates_raise_rather_than_becoming_zero(self, monkeypatch):
        """A zero fee is the most optimistic possible answer."""
        serve(monkeypatch, self.fee_payload(maker="", taker=""))
        with pytest.raises(okx.OKXError, match="unparseable"):
            okx.get_trade_fee("BTC-USDT")


def test_a_narrow_get_history_must_not_shrink_a_wide_cache(tmp_path, monkeypatch):
    """A 365-day request must not discard eight years of cached candles.

    The daily scan asks for its statistics window every run. Before this was
    fixed, `start_ts` was applied BEFORE the cache write, so each scan wrote
    back only the window it asked for — twenty pairs were truncated from
    30-75k bars to 8 759 that way, and nothing reported it. The loss is silent
    and only shows up much later, when a backtest wants a window that no longer
    exists.
    """
    step = okx.bar_ms("1H")
    newest = 1_700_000_000_000

    # A wide cache already on disk: 5 000 bars.
    wide = [{"ts": newest - (4999 - i) * step, "open": 10.0, "high": 10.0,
             "low": 10.0, "close": 10.0, "volume": 1.0} for i in range(5000)]
    cache_dir = tmp_path / "candles"
    cache_dir.mkdir()
    okx._save_cache(okx._cache_path("T-USDT", "1H", cache_dir),
                    "T-USDT", "1H", wide)

    # The live endpoints return only the recent end; no deeper history exists.
    monkeypatch.setattr(okx, "get_candles",
                        lambda *a, **k: [dict(c) for c in wide[-300:]])
    monkeypatch.setattr(okx, "get_history_candles", lambda *a, **k: [])

    # Ask for a NARROW window: the most recent 100 bars.
    narrow_start = newest - 99 * step
    got = okx.get_history("T-USDT", "1H", start_ts=narrow_start,
                          cache_dir=cache_dir, allow_gaps=True)

    # The caller gets what it asked for...
    assert len(got) == 100
    assert got[0]["ts"] == narrow_start

    # ...and the cache still holds everything it held before.
    on_disk = okx._load_cache(okx._cache_path("T-USDT", "1H", cache_dir))
    assert len(on_disk) == 5000, (
        f"a 100-bar request shrank the cache to {len(on_disk)} bars")
    assert on_disk[0]["ts"] == wide[0]["ts"]
