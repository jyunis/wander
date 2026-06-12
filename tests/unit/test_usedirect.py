"""MN UseDirect availability (Phase 2 / PR2, display-only — TLC-3).

The UseDirectClient grid parse/cache/error path (mocked POST — no live network)
and the trip-level usedirect branch + name->FacilityId mapping (incl.
loop-split aggregation and the all-nights count).
"""

from __future__ import annotations

import json

import httpx
import pytest

from cairn.availability import (
    is_mn_state_forest,
    trip_availability,
    usedirect_facility_ids,
)
from cairn.clients.usedirect import UseDirectClient
from cairn.config import Settings
from cairn.errors import AvailabilityBadResponse, AvailabilityUnreachable

# --- mapping helper ------------------------------------------------------

_MAP = {
    "Bear Paw Campground — Itasca State Park": 882,
    "Pine Ridge Campground — Itasca State Park": [839, 995, 994],
}


def test_usedirect_facility_ids_single_and_list():
    bear = {"name": "Bear Paw Campground — Itasca State Park"}
    pine = {"name": "Pine Ridge Campground — Itasca State Park"}
    assert usedirect_facility_ids(bear, _MAP) == [882]
    assert usedirect_facility_ids(pine, _MAP) == [839, 995, 994]


def test_usedirect_facility_ids_unmapped_or_no_map():
    assert usedirect_facility_ids({"name": "Nope"}, _MAP) == []
    assert usedirect_facility_ids({"name": "Bear Paw"}, None) == []
    assert usedirect_facility_ids(None, _MAP) == []


# --- UseDirectClient (mocked POST, real disk cache) ----------------------


def _grid(units):
    """A minimal grid payload: units -> {date: IsFree}."""
    return {"Facility": {"Units": {
        str(i): {"UnitId": i, "Slices": {
            d: {"Date": f"{d}T00:00:00", "IsFree": free} for d, free in slices.items()
        }}
        for i, slices in enumerate(units)
    }}}


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode() if payload is not None else b""

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, 0

    async def post_json(self, url, body, *, timeout=30.0):
        self.calls += 1
        self.last_body = body
        if self.exc is not None:
            raise self.exc
        return self.resp

    async def aclose(self):
        pass


def _client(tmp_path, http):
    c = UseDirectClient(Settings(_env_file=None, data_dir=tmp_path))
    c._http = http
    return c


async def test_facility_grid_parses_units_and_slices(tmp_path):
    payload = _grid([
        {"2026-07-04": True, "2026-07-05": True},
        {"2026-07-04": True, "2026-07-05": False},
    ])
    http = _FakeHttp(resp=_FakeResp(200, payload))
    c = _client(tmp_path, http)
    out = await c.facility_grid(882, "2026-07-04", 2)
    assert out == {
        "0": {"2026-07-04": True, "2026-07-05": True},
        "1": {"2026-07-04": True, "2026-07-05": False},
    }
    assert http.last_body["FacilityId"] == 882 and http.last_body["StartDate"] == "2026-07-04"


async def test_facility_grid_caches(tmp_path):
    http = _FakeHttp(resp=_FakeResp(200, _grid([{"2026-07-04": True}])))
    c = _client(tmp_path, http)
    await c.facility_grid(882, "2026-07-04", 1)
    await c.facility_grid(882, "2026-07-04", 1)
    assert http.calls == 1


async def test_facility_grid_errors(tmp_path):
    with pytest.raises(AvailabilityBadResponse):
        await _client(tmp_path, _FakeHttp(resp=_FakeResp(500))).facility_grid(1, "2026-07-04", 1)
    with pytest.raises(AvailabilityUnreachable):
        await _client(tmp_path, _FakeHttp(exc=httpx.ConnectError("x"))).facility_grid(
            1, "2026-07-04", 1
        )


# --- trip-level usedirect branch -----------------------------------------


class _FakeUD:
    def __init__(self, grids):
        self.grids = grids  # {facility_id: {unit_id: {date: isfree}}}
        self.raise_for = None

    async def facility_grid(self, facility_id, start_date, nights):
        if self.raise_for == facility_id:
            raise AvailabilityUnreachable("down")
        return self.grids.get(facility_id, {})


def _trip(members, start="2026-07-04", end="2026-07-06"):
    return {"start_date": start, "end_date": end, "members": members}


def _cg(saved_id, name):
    return {"saved_id": saved_id, "kind": "campground", "role": "fallback",
            "payload": {"category": "State Park", "name": name,
                        "url": "https://www.dnr.state.mn.us/state_parks/itasca/index.html"}}


async def test_trip_usedirect_ok_counts_all_nights():
    trip = _trip([_cg(7, "Bear Paw Campground — Itasca State Park")])
    ud = _FakeUD({882: {
        "u1": {"2026-07-04": True, "2026-07-05": True},   # open both nights
        "u2": {"2026-07-04": True, "2026-07-05": False},  # one night taken
        "u3": {"2026-07-04": False, "2026-07-05": False}, # full
    }})
    out = await trip_availability(trip, None, usedirect_client=ud, usedirect_map=_MAP)
    badge = out["members"]["7"]
    assert badge["system"] == "usedirect" and badge["status"] == "ok"
    assert (badge["open_sites"], badge["total_sites"]) == (1, 3)
    assert badge["summary"] == "1 of 3 sites open Jul 4–6"


async def test_trip_usedirect_loop_split_merges_facilities():
    trip = _trip([_cg(8, "Pine Ridge Campground — Itasca State Park")])
    ud = _FakeUD({
        839: {"a": {"2026-07-04": True, "2026-07-05": True}},
        995: {"a": {"2026-07-04": True, "2026-07-05": False}},  # same unit-id, diff facility
        994: {"a": {"2026-07-04": True, "2026-07-05": True}},
    })
    out = await trip_availability(trip, None, usedirect_client=ud, usedirect_map=_MAP)
    badge = out["members"]["8"]
    # Units namespaced by facility -> 3 distinct units, 2 open both nights.
    assert (badge["open_sites"], badge["total_sites"]) == (2, 3)


async def test_trip_usedirect_no_dates_and_error_and_unsupported():
    # no dates
    t1 = _trip([_cg(7, "Bear Paw Campground — Itasca State Park")], start=None, end=None)
    o1 = await trip_availability(t1, None, usedirect_client=_FakeUD({}), usedirect_map=_MAP)
    assert o1["members"]["7"] == {"system": "usedirect", "status": "no_dates"}
    # source error
    ud = _FakeUD({})
    ud.raise_for = 882
    t2 = _trip([_cg(7, "Bear Paw Campground — Itasca State Park")])
    o2 = await trip_availability(t2, None, usedirect_client=ud, usedirect_map=_MAP)
    assert o2["members"]["7"]["status"] == "error"
    # unmapped MN campground -> unsupported (link out)
    t3 = _trip([_cg(9, "Some Unmapped Campground — Nowhere State Park")])
    o3 = await trip_availability(t3, None, usedirect_client=_FakeUD({}), usedirect_map=_MAP)
    assert o3["members"]["9"] == {"system": None, "status": "unsupported"}


def test_is_mn_state_forest():
    # MN state forest (category) -> first-come.
    assert is_mn_state_forest({"category": "State Forest", "url": None}) is True
    # MN forest mislabeled "State Park" but named a forest -> still first-come.
    forest_named = {"category": "State Park",
                    "name": "Isinours — Richard J. Dorer Memorial Hardwood State Forest"}
    assert is_mn_state_forest(forest_named) is True
    # WI forest (GoingToCamp) -> NOT first-come (link out).
    assert is_mn_state_forest(
        {"category": "State Forest", "url": "https://dnr.wisconsin.gov/topic/StateForests/x"}
    ) is False
    # A reservable state park -> not a forest.
    assert is_mn_state_forest({"category": "State Park", "name": "Bear Paw Campground"}) is False


async def test_trip_mn_state_forest_is_first_come():
    forest = {"saved_id": 51, "kind": "campground", "role": "primary",
              "payload": {"category": "State Forest", "name": "Shell City Horse Campground"}}
    out = await trip_availability(
        _trip([forest]), None, usedirect_client=_FakeUD({}), usedirect_map=_MAP
    )
    assert out["members"]["51"] == {"system": "usedirect", "status": "first_come"}
    # Holds even with no trip dates (first-come is date-independent).
    out2 = await trip_availability(
        {"start_date": None, "end_date": None, "members": [forest]},
        None, usedirect_client=_FakeUD({}), usedirect_map=_MAP,
    )
    assert out2["members"]["51"]["status"] == "first_come"


async def test_trip_usedirect_no_client_degrades():
    trip = _trip([_cg(7, "Bear Paw Campground — Itasca State Park")])
    out = await trip_availability(trip, None, usedirect_client=None, usedirect_map=_MAP)
    assert out["members"]["7"]["status"] == "error"
