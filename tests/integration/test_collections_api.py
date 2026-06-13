"""Collections API (FC-8) — exercised at the HTTP boundary.

CRUD + validation, the per-place tag-set (PUT) and bulk-add (POST) ops, GET
/saved hydration of collection_ids, and the cascade on delete. No scoring or
network needed — collections are pure user data (TLC-3)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import cairn.config as config_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        yield c
    config_module._settings = None


def _saved(client, name="Spur", kind="site", lat=47.5, lon=-94.2):
    return client.post("/saved", json={"kind": kind, "name": name, "lat": lat, "lon": lon}).json()


def test_collection_crud_and_validation(client):
    r = client.post("/collections", json={"name": "Scouted", "color": "#5b9c5a"})
    assert r.status_code == 201
    col = r.json()
    assert col["name"] == "Scouted" and col["color"] == "#5b9c5a" and col["count"] == 0

    body = client.get("/collections").json()
    assert any(c["id"] == col["id"] for c in body["collections"])
    assert "physical suitability only" in body["disclaimer"].lower()

    # Validation: empty name + bad color -> 422.
    assert client.post("/collections", json={"name": "  "}).status_code == 422
    assert client.post("/collections", json={"name": "X", "color": "red"}).status_code == 422

    upd = client.patch(f"/collections/{col['id']}", json={"name": "Scouted 26"})
    assert upd.status_code == 200 and upd.json()["name"] == "Scouted 26"
    assert client.patch("/collections/999999", json={"name": "x"}).status_code == 404

    assert client.delete(f"/collections/{col['id']}").json()["deleted"] is True


def test_set_place_collections_and_hydration(client):
    s = _saved(client)
    c1 = client.post("/collections", json={"name": "Lakeside"}).json()
    c2 = client.post("/collections", json={"name": "BWCA"}).json()

    r = client.put(f"/saved/{s['id']}/collections", json={"collection_ids": [c1["id"], c2["id"]]})
    assert r.status_code == 200
    assert sorted(r.json()["collection_ids"]) == sorted([c1["id"], c2["id"]])

    # GET /saved hydrates the membership.
    saved = client.get("/saved").json()["saved"]
    place = next(p for p in saved if p["id"] == s["id"])
    assert sorted(place["collection_ids"]) == sorted([c1["id"], c2["id"]])

    # PUT replaces; unknown ids ignored; unknown place -> 404.
    replaced = client.put(
        f"/saved/{s['id']}/collections", json={"collection_ids": [c2["id"], 999999]}
    )
    assert replaced.json()["collection_ids"] == [c2["id"]]
    assert client.put("/saved/999999/collections", json={"collection_ids": []}).status_code == 404


def test_bulk_add_places(client):
    s1, s2 = _saved(client, "A"), _saved(client, "B", lon=-94.3)
    c = client.post("/collections", json={"name": "Ideas"}).json()

    r = client.post(f"/collections/{c['id']}/places", json={"saved_ids": [s1["id"], s2["id"]]})
    assert r.status_code == 200 and r.json()["added"] == 2
    # Idempotent.
    again = client.post(f"/collections/{c['id']}/places", json={"saved_ids": [s1["id"], s2["id"]]})
    assert again.json()["added"] == 0
    # Unknown collection -> 404.
    miss = client.post("/collections/999999/places", json={"saved_ids": [s1["id"]]})
    assert miss.status_code == 404

    cols = client.get("/collections").json()["collections"]
    assert next(x for x in cols if x["id"] == c["id"])["count"] == 2


def test_delete_collection_cascades(client):
    s = _saved(client)
    c = client.post("/collections", json={"name": "Temp"}).json()
    client.put(f"/saved/{s['id']}/collections", json={"collection_ids": [c["id"]]})
    client.delete(f"/collections/{c['id']}")
    # The saved place survives; its membership is gone.
    place = next(p for p in client.get("/saved").json()["saved"] if p["id"] == s["id"])
    assert place["collection_ids"] == []
