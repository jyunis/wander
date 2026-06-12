"""GET /trips/{id}/availability (FC-8, Phase 2, display-only — TLC-3).

Exercised at the HTTP boundary with a fake recreation.gov client injected into
app.state (no live network). Asserts the per-member badge map, the dates block,
the disclaimer, the non-federal degrade, and the 404 for an unknown trip.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import cairn.config as config_module
from cairn.errors import AvailabilityUnreachable


class _FakeRecGov:
    def __init__(self):
        self.months = {}
        self.raise_for = None

    async def campground_month(self, fid, y, m):
        if self.raise_for == fid:
            raise AvailabilityUnreachable("down")
        return self.months.get((fid, y, m), {})

    async def aclose(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        c._fake_avail = _FakeRecGov()
        app.state.availability = c._fake_avail
        yield c
    config_module._settings = None


def _make_trip_with_federal(client, start="2026-07-05", end="2026-07-07"):
    trip = client.post("/trips", json={"name": "T", "start_date": start, "end_date": end}).json()
    saved = client.post("/saved", json={
        "kind": "campground", "name": "Deer Lake", "lat": 47.6, "lon": -94.0,
        "payload": {"category": "Federal",
                    "url": "https://www.recreation.gov/camping/campgrounds/231997"},
    }).json()
    client.post(f"/trips/{trip['id']}/members", json={"saved_id": saved["id"], "role": "fallback"})
    return trip, saved


def test_availability_federal_badge(client):
    client._fake_avail.months[("231997", 2026, 7)] = {
        "A": {"2026-07-05": "Available", "2026-07-06": "Available"},
        "B": {"2026-07-05": "Reserved", "2026-07-06": "Available"},
    }
    trip, saved = _make_trip_with_federal(client)
    r = client.get(f"/trips/{trip['id']}/availability")
    assert r.status_code == 200
    body = r.json()
    assert body["dates"] == {"start": "2026-07-05", "end": "2026-07-07", "nights": 2}
    badge = body["members"][str(saved["id"])]
    assert badge["status"] == "ok" and badge["open_sites"] == 1 and badge["total_sites"] == 2
    assert badge["summary"] == "1 of 2 sites open Jul 5–7"
    assert "physical suitability only" in body["disclaimer"].lower()


def test_availability_unsupported_state_park(client):
    trip = client.post("/trips", json={"name": "T", "start_date": "2026-07-05",
                                       "end_date": "2026-07-07"}).json()
    saved = client.post("/saved", json={
        "kind": "campground", "name": "Itasca", "lat": 47.2, "lon": -95.2,
        "payload": {"category": "State Park", "url": "https://www.dnr.state.mn.us/x"},
    }).json()
    client.post(f"/trips/{trip['id']}/members", json={"saved_id": saved["id"], "role": "fallback"})
    body = client.get(f"/trips/{trip['id']}/availability").json()
    assert body["members"][str(saved["id"])] == {"system": None, "status": "unsupported"}


def test_availability_first_come_federal(client):
    # A federal campground flagged non-reservable in RIDB meta -> first-come,
    # no fetch attempted (the fake would 404, but we short-circuit on meta).
    client._fake_avail.raise_for = "249512"  # would error if fetched
    trip = client.post("/trips", json={"name": "T", "start_date": "2026-07-05",
                                       "end_date": "2026-07-07"}).json()
    saved = client.post("/saved", json={
        "kind": "campground", "name": "Walker Area", "lat": 47.1, "lon": -94.6,
        "payload": {"category": "Federal", "meta": {"reservable": False},
                    "url": "https://www.recreation.gov/camping/campgrounds/249512"},
    }).json()
    client.post(f"/trips/{trip['id']}/members", json={"saved_id": saved["id"], "role": "primary"})
    body = client.get(f"/trips/{trip['id']}/availability").json()
    assert body["members"][str(saved["id"])] == {"system": "recreation.gov", "status": "first_come"}


def test_availability_no_dates(client):
    trip = client.post("/trips", json={"name": "T"}).json()
    saved = client.post("/saved", json={
        "kind": "campground", "name": "Deer Lake", "lat": 47.6, "lon": -94.0,
        "payload": {"category": "Federal",
                    "url": "https://www.recreation.gov/camping/campgrounds/231997"},
    }).json()
    client.post(f"/trips/{trip['id']}/members", json={"saved_id": saved["id"], "role": "fallback"})
    body = client.get(f"/trips/{trip['id']}/availability").json()
    assert body["dates"]["nights"] == 0
    assert body["members"][str(saved["id"])]["status"] == "no_dates"


def test_availability_source_error_degrades(client):
    client._fake_avail.raise_for = "231997"
    trip, saved = _make_trip_with_federal(client)
    body = client.get(f"/trips/{trip['id']}/availability").json()
    assert body["members"][str(saved["id"])]["status"] == "error"


def test_availability_unknown_trip_404(client):
    assert client.get("/trips/999999/availability").status_code == 404
