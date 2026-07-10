"""The /field surface: region catalogue, archive delivery, and the PWA shell.

The offline field companion's whole premise is that a phone can fetch a region
archive once and then work with no signal. That puts two things under test here
that would otherwise only fail on a phone, in a forest: the archive must serve
**Range** requests (MapLibre reads tiles as byte slices before you download the
whole thing), and the shell must be served **uncacheable** with a service worker
whose bytes change per build (an installed PWA otherwise pins itself to the
first shell it ever saw).
"""

from __future__ import annotations

import struct

import pytest
from fastapi.testclient import TestClient

import cairn.config as config_module


def _archive(payload: bytes = b"tiledata" * 32) -> bytes:
    """A minimal valid PMTiles v3 archive: header + stand-in tile bytes."""
    h = bytearray(127)
    h[0:7] = b"PMTiles"
    h[7] = 3
    h[100], h[101] = 0, 14
    struct.pack_into("<iiii", h, 102, -929000000, 470000000, -899000000, 483000000)
    h[118] = 8
    struct.pack_into("<ii", h, 119, -914000000, 476500000)
    return bytes(h) + payload


@pytest.fixture
def client(tmp_path, monkeypatch):
    regions = tmp_path / "regions"
    regions.mkdir()
    (regions / "superior-nf.pmtiles").write_bytes(_archive())
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        yield c


def test_regions_lists_what_is_on_disk(client):
    body = client.get("/field/regions").json()

    assert len(body["regions"]) == 1
    region = body["regions"][0]
    assert region["id"] == "superior-nf"
    assert region["name"] == "Superior National Forest"
    assert region["url"] == "/field/regions/superior-nf.pmtiles"
    assert region["bbox"] == pytest.approx([-92.9, 47.0, -89.9, 48.3])
    assert region["size_bytes"] == 127 + 256
    # Display-only, like every other overlay (TLC-3).
    assert body["disclaimer"]


def test_regions_is_empty_without_a_regions_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))  # no regions/ inside
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        assert c.get("/field/regions").json()["regions"] == []


def test_archive_is_served_whole_as_an_octet_stream(client):
    res = client.get("/field/regions/superior-nf.pmtiles")

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/octet-stream"
    assert res.content == _archive()
    # A rebuilt region must not be masked by heuristic caching.
    assert res.headers["cache-control"] == "no-cache"


def test_archive_serves_range_requests(client):
    """MapLibre + pmtiles.js read the header, then tiles, as byte slices. Without
    a 206 the client silently gets whole-file 200s and every tile read is wrong."""
    full = _archive()
    head = client.get("/field/regions/superior-nf.pmtiles", headers={"Range": "bytes=0-126"})

    assert head.status_code == 206
    assert head.content == full[:127]
    assert head.headers["content-range"] == f"bytes 0-126/{len(full)}"

    tail = client.get("/field/regions/superior-nf.pmtiles", headers={"Range": "bytes=127-134"})
    assert tail.status_code == 206
    assert tail.content == full[127:135]


def test_archive_advertises_range_support(client):
    assert client.get("/field/regions/superior-nf.pmtiles").headers["accept-ranges"] == "bytes"


def test_unknown_region_is_404(client):
    assert client.get("/field/regions/nope.pmtiles").status_code == 404


def test_region_id_cannot_traverse_out_of_the_regions_dir(client):
    """The id comes straight off the URL path."""
    for path in ("/field/regions/..%2Fsecret.pmtiles", "/field/regions/Superior.pmtiles"):
        assert client.get(path).status_code == 404


def test_shell_is_served_uncacheable_with_versioned_assets(client):
    res = client.get("/field/")

    assert res.status_code == 200
    # iOS Safari will serve a stale entry HTML straight past no-cache revalidation.
    assert res.headers["cache-control"] == "no-store"
    assert "./field.js?v=" in res.text
    assert "./field.css?v=" in res.text


def test_service_worker_carries_the_build_token(client):
    """The token is the SW's cache name. If it never changed, an installed phone
    would keep serving the shell it precached on the day it was installed."""
    res = client.get("/field/sw.js")

    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    assert "__BUILD__" not in res.text
    assert "wander-field-shell-" in res.text


def test_service_worker_stays_out_of_region_data(client):
    """Region data belongs to the page, which caches and evicts it as one unit.

    The Cache API also cannot answer a 206: an intercepted Range request would
    hand MapLibre a whole-file 200 where it asked for a tile-sized slice. And a
    31 MB archive has no business in the shell cache, which every deploy wipes.
    """
    sw = client.get("/field/sw.js").text
    assert "url.pathname.startsWith('/field/regions/')" in sw
    assert "req.headers.has('range')" in sw


def test_field_app_keeps_one_source_of_truth_for_downloaded_regions():
    """The Cache API holds the archives, so it must also hold the index of them.

    Splitting the two costs an offline cold start: a headless run with the server
    stopped found the 31 MB archive intact but the localStorage receipt gone, so
    the app reported "no offline map" while holding one. They are separately
    evictable and iOS drops localStorage first. Regressing this is invisible
    until someone is in a forest with no signal.
    """
    from cairn.api import _FIELD_DIR

    source = (_FIELD_DIR / "field.js").read_text()
    code = "\n".join(ln for ln in source.splitlines() if not ln.strip().startswith("//"))
    assert "localStorage" not in code
    assert "sessionStorage" not in code
    assert "indexedDB" not in code


def test_service_worker_reloads_stale_pages_on_upgrade(client):
    """The worker upgrades the page; the page cannot upgrade itself.

    A page is served by the PREVIOUS worker, so it runs the previous build's JS.
    Update-handling code added to the page therefore only ships in the build the
    stale page isn't running — deploy, reload, and nothing changes. The worker is
    the only participant that is always current, so `activate` reloads the open
    windows, but only when it actually replaced an older cache (a first install
    must not reload itself).
    """
    sw = client.get("/field/sw.js").text

    assert "clients.matchAll" in sw and "navigate(" in sw
    assert "if (!stale.length) return;" in sw  # first install: nothing to replace


def test_field_map_layers_declare_their_geometry(client):
    """MapLibre does not filter a layer's bucket by geometry type.

    Protomaps packs lakes (polygons), rivers (lines) and label anchors (points)
    into one `water` source-layer. Handed a LineString, a `fill` layer earcuts it
    into a long sliver across the map; a `line` layer strokes every lake outline.
    The artifact only appears mid-zoom, so it survives any test that waits for the
    map to settle before asserting.
    """
    from cairn.api import _FIELD_DIR

    source = (_FIELD_DIR / "field.js").read_text()
    assert "['geometry-type'], 'Polygon'" in source
    assert "['geometry-type'], 'LineString'" in source
    # Every layer reading the mixed-geometry basemap source states what it wants.
    for layer in ("water", "earth", "landcover", "landuse"):
        block = source.split(f"id: '{layer}'", 1)[1][:400]
        assert "filter:" in block, f"{layer} fill layer has no geometry filter"


def test_manifest_is_served_as_a_manifest(client):
    """Python's mimetypes doesn't know .webmanifest; text/plain makes iOS ignore
    it, and the PWA never gets its own (durable) storage bucket."""
    res = client.get("/field/manifest.webmanifest")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/manifest+json")
    assert res.json()["scope"] == "/field/"


def test_field_does_not_shadow_the_main_app(client):
    assert client.get("/").status_code == 200
    assert "/app.js?v=" in client.get("/").text
