"""Phase 2 campground availability (display-only — TLC-3).

Pure helpers (fid parsing, nights, months, all-nights open count, date range),
the RecreationGovClient HTTP/cache/parse path (mocked — no live network), and
the trip-level orchestration with a fake client (federal ok / no-dates /
unsupported / source-error degrade paths).
"""

from __future__ import annotations

import json

import httpx
import pytest

from cairn.availability import (
    count_open_all_nights,
    federal_facility_id,
    format_range,
    months_spanning,
    nights_between,
    trip_availability,
)
from cairn.clients.availability import RecreationGovClient
from cairn.config import Settings
from cairn.errors import (
    AvailabilityBadResponse,
    AvailabilityNotReservable,
    AvailabilityUnreachable,
)

# --- pure helpers --------------------------------------------------------


def test_federal_facility_id_from_payload():
    fed = {"category": "Federal", "url": "https://www.recreation.gov/camping/campgrounds/231997"}
    assert federal_facility_id(fed) == "231997"
    # "National / Federal" also matches the federal category.
    assert federal_facility_id({"category": "National / Federal", "url": fed["url"]}) == "231997"


def test_federal_facility_id_rejects_non_federal_and_bad_url():
    state = {"category": "State Park", "url": "https://www.dnr.state.mn.us/x"}
    assert federal_facility_id(state) is None
    # Federal category but not a campground url -> no id -> degrades.
    bad_url = {"category": "Federal", "url": "https://www.recreation.gov/search"}
    assert federal_facility_id(bad_url) is None
    assert federal_facility_id({"category": "Federal", "url": None}) is None
    assert federal_facility_id(None) is None


def test_nights_between_excludes_checkout_day():
    # Jul 5-7 = two nights (5th, 6th); the 7th is checkout, not a night.
    assert nights_between("2026-07-05", "2026-07-07") == ["2026-07-05", "2026-07-06"]


def test_nights_between_same_or_inverted_day_is_one_night():
    assert nights_between("2026-07-05", "2026-07-05") == ["2026-07-05"]
    assert nights_between("2026-07-07", "2026-07-05") == ["2026-07-07"]


def test_months_spanning_dedupes_and_crosses_month():
    nights = nights_between("2026-07-30", "2026-08-02")  # 30,31,01
    assert nights == ["2026-07-30", "2026-07-31", "2026-08-01"]
    assert months_spanning(nights) == [(2026, 7), (2026, 8)]


def test_count_open_all_nights_requires_every_night_available():
    nights = ["2026-07-05", "2026-07-06"]
    month = {
        "A": {"2026-07-05": "Available", "2026-07-06": "Available"},   # both -> open
        "B": {"2026-07-05": "Available", "2026-07-06": "Reserved"},    # one night taken
        "C": {"2026-07-05": "Reserved", "2026-07-06": "Reserved"},     # full
        "D": {"2026-07-05": "Available"},                              # missing 6th -> not open
    }
    assert count_open_all_nights([month], nights) == (1, 4)  # only A; total counts all 4 sites


def test_count_open_all_nights_merges_months():
    nights = ["2026-07-31", "2026-08-01"]
    jul = {"A": {"2026-07-31": "Available"}, "B": {"2026-07-31": "Reserved"}}
    aug = {"A": {"2026-08-01": "Available"}, "B": {"2026-08-01": "Available"}}
    # A is available both nights across the month boundary; B fails Jul 31.
    assert count_open_all_nights([jul, aug], nights) == (1, 2)


def test_format_range():
    assert format_range("2026-07-05", "2026-07-07") == "Jul 5–7"
    assert format_range("2026-07-30", "2026-08-02") == "Jul 30 – Aug 2"
    assert format_range("2026-07-05", "2026-07-05") == "Jul 5"
    assert format_range("2026-12-31", "2027-01-01") == "Dec 31, 2026 – Jan 1, 2027"


# --- RecreationGovClient (mocked HTTP, real disk cache) ------------------


class _FakeResp:
    def __init__(self, status_code, payload=None, *, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class _FakeHttp:
    """Stands in for CourtesyHttp: records calls, returns a queued response."""

    def __init__(self, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.calls = 0

    async def get(self, url, *, params=None, timeout=30.0):
        self.calls += 1
        self.last_url = url
        self.last_params = params
        if self.exc is not None:
            raise self.exc
        return self.resp

    async def aclose(self):
        pass


def _client(tmp_path, http):
    s = Settings(_env_file=None, data_dir=tmp_path)
    c = RecreationGovClient(s)
    c._http = http
    return c


_MONTH = {
    "campsites": {
        "100": {"availabilities": {"2026-07-05T00:00:00Z": "Available",
                                   "2026-07-06T00:00:00Z": "Reserved"}},
        "101": {"availabilities": {"2026-07-05T00:00:00Z": "Available",
                                   "2026-07-06T00:00:00Z": "Available"}},
    }
}


async def test_campground_month_parses_and_percent_encodes(tmp_path):
    http = _FakeHttp(resp=_FakeResp(200, _MONTH))
    c = _client(tmp_path, http)
    out = await c.campground_month("231997", 2026, 7)
    assert out == {
        "100": {"2026-07-05": "Available", "2026-07-06": "Reserved"},
        "101": {"2026-07-05": "Available", "2026-07-06": "Available"},
    }
    # 1st-of-month ISO with time -> httpx will percent-encode the params.
    assert http.last_params == {"start_date": "2026-07-01T00:00:00.000Z"}
    assert http.last_url.endswith("/231997/month")


async def test_campground_month_caches_within_ttl(tmp_path):
    http = _FakeHttp(resp=_FakeResp(200, _MONTH))
    c = _client(tmp_path, http)
    await c.campground_month("231997", 2026, 7)
    await c.campground_month("231997", 2026, 7)  # served from disk cache
    assert http.calls == 1


async def test_campground_month_404_is_not_reservable(tmp_path):
    # A 404 means the facility has no reservable grid (first-come) — a distinct,
    # non-error signal the caller surfaces as "first-come."
    c = _client(tmp_path, _FakeHttp(resp=_FakeResp(404)))
    with pytest.raises(AvailabilityNotReservable):
        await c.campground_month("249512", 2026, 7)


async def test_campground_month_bad_status_raises(tmp_path):
    c = _client(tmp_path, _FakeHttp(resp=_FakeResp(429)))
    with pytest.raises(AvailabilityBadResponse):
        await c.campground_month("231997", 2026, 7)


async def test_campground_month_unreachable_raises(tmp_path):
    c = _client(tmp_path, _FakeHttp(exc=httpx.ConnectError("boom")))
    with pytest.raises(AvailabilityUnreachable):
        await c.campground_month("231997", 2026, 7)


# --- trip-level orchestration (fake client) ------------------------------


class _FakeClient:
    def __init__(self, months):
        self.months = months  # {(fid,y,m): parsed_month}
        self.raise_for = None
        self.raise_exc = AvailabilityUnreachable("down")

    async def campground_month(self, fid, y, m):
        if self.raise_for == fid:
            raise self.raise_exc
        return self.months.get((fid, y, m), {})


def _member(saved_id, kind, payload, role="fallback"):
    return {"saved_id": saved_id, "kind": kind, "role": role, "payload": payload}


_FED = {"category": "Federal", "url": "https://www.recreation.gov/camping/campgrounds/231997"}
_STATE = {"category": "State Park", "url": "https://www.dnr.state.mn.us/x"}


async def test_trip_availability_federal_ok():
    trip = {
        "start_date": "2026-07-05", "end_date": "2026-07-07",
        "members": [
            _member(1, "site", {"total": 0.7}, role="primary"),  # dispersed -> ignored
            _member(2, "campground", _FED),
        ],
    }
    client = _FakeClient({("231997", 2026, 7): {
        "A": {"2026-07-05": "Available", "2026-07-06": "Available"},
        "B": {"2026-07-05": "Available", "2026-07-06": "Reserved"},
    }})
    out = await trip_availability(trip, client)
    assert out["dates"] == {"start": "2026-07-05", "end": "2026-07-07", "nights": 2}
    assert "1" not in out["members"]  # the dispersed primary gets no badge
    badge = out["members"]["2"]
    assert badge["status"] == "ok" and badge["system"] == "recreation.gov"
    assert (badge["open_sites"], badge["total_sites"]) == (1, 2)
    assert badge["summary"] == "1 of 2 sites open Jul 5–7"


async def test_trip_availability_no_dates():
    trip = {"start_date": None, "end_date": None,
            "members": [_member(2, "campground", _FED)]}
    out = await trip_availability(trip, _FakeClient({}))
    assert out["dates"]["nights"] == 0
    assert out["members"]["2"] == {"system": "recreation.gov", "status": "no_dates"}


async def test_trip_availability_unsupported_state_park():
    trip = {"start_date": "2026-07-05", "end_date": "2026-07-07",
            "members": [_member(3, "campground", _STATE)]}
    out = await trip_availability(trip, _FakeClient({}))
    assert out["members"]["3"] == {"system": None, "status": "unsupported"}


async def test_trip_availability_first_come_from_meta():
    # RIDB flags the campground non-reservable -> first-come, no fetch attempted.
    fed_fcfs = {**_FED, "meta": {"reservable": False}}
    trip = {"start_date": "2026-07-05", "end_date": "2026-07-07",
            "members": [_member(2, "campground", fed_fcfs)]}
    client = _FakeClient({})
    client.raise_for = "231997"  # would raise if called — proves we short-circuit
    out = await trip_availability(trip, client)
    assert out["members"]["2"] == {"system": "recreation.gov", "status": "first_come"}


async def test_trip_availability_first_come_from_404():
    # No RIDB hint, but recreation.gov 404s the grid -> still first-come.
    trip = {"start_date": "2026-07-05", "end_date": "2026-07-07",
            "members": [_member(2, "campground", _FED)]}
    client = _FakeClient({})
    client.raise_for = "231997"
    client.raise_exc = AvailabilityNotReservable("404")
    out = await trip_availability(trip, client)
    assert out["members"]["2"] == {"system": "recreation.gov", "status": "first_come"}


async def test_trip_availability_first_come_even_without_dates():
    fed_fcfs = {**_FED, "meta": {"reservable": False}}
    trip = {"start_date": None, "end_date": None,
            "members": [_member(2, "campground", fed_fcfs)]}
    out = await trip_availability(trip, _FakeClient({}))
    assert out["members"]["2"]["status"] == "first_come"  # date-independent


async def test_trip_availability_source_error_degrades():
    trip = {"start_date": "2026-07-05", "end_date": "2026-07-07",
            "members": [_member(2, "campground", _FED)]}
    client = _FakeClient({})
    client.raise_for = "231997"
    out = await trip_availability(trip, client)
    assert out["members"]["2"] == {"system": "recreation.gov", "status": "error"}


async def test_trip_availability_no_client_degrades():
    trip = {"start_date": "2026-07-05", "end_date": "2026-07-07",
            "members": [_member(2, "campground", _FED)]}
    out = await trip_availability(trip, None)
    assert out["members"]["2"]["status"] == "error"
