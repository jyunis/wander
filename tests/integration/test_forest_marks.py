"""Forest marks API (doc #34 #3) — the Explore tab's pinned + recent forests,
persisted server-side so they follow the user across devices. UI prefs only,
never scoring (TLC-3). Exercised at the HTTP boundary; no overlays/network."""

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


def test_marks_default_empty(client):
    r = client.get("/forests/marks")
    assert r.status_code == 200
    assert r.json() == {"pinned": [], "recent": []}


def test_marks_put_roundtrips_and_preserves_order(client):
    body = {
        "pinned": ["national_forest/Chippewa", "state_forest/Bear Island"],
        "recent": ["state_forest/Bear Island", "national_forest/Superior"],
    }
    r = client.put("/forests/marks", json=body)
    assert r.status_code == 200
    assert r.json() == body
    # Persisted: a fresh GET returns the same ordered lists.
    assert client.get("/forests/marks").json() == body


def test_marks_put_replaces_wholesale_and_dedupes(client):
    client.put("/forests/marks", json={"pinned": ["a/x"], "recent": ["a/y"]})
    # A second PUT fully replaces the prior state; within-list dupes collapse
    # (order-preserving).
    r = client.put(
        "/forests/marks",
        json={"pinned": ["b/z", "b/z", "c/w"], "recent": []},
    )
    assert r.json() == {"pinned": ["b/z", "c/w"], "recent": []}
    assert client.get("/forests/marks").json() == {"pinned": ["b/z", "c/w"], "recent": []}
