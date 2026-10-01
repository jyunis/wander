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


def test_upgrade_never_interrupts_a_download(client):
    """The worker asks before it reloads a page, and waits out a download.

    `activate` reloads open windows so a stale page picks up a deploy. Doing that
    mid-download throws away however much of a 31 MB archive the page had. A page
    on an older build cannot answer the question — silence times out as "not
    busy", which is right: rescuing it is the point, and it has no download we
    know of.
    """
    from cairn.api import _FIELD_DIR

    sw = client.get("/field/sw.js").text
    page = (_FIELD_DIR / "field.js").read_text()

    assert "wf-upgrade-check" in sw and "wf-upgrade-check" in page
    assert "if (await isBusy(client)) continue;" in sw
    assert "resolve(false)" in sw  # silence from an old build => proceed
    assert "upgradeDeferred" in page  # ...and the page reloads once it's safe


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


# --- Wander content on the offline map (#45 W1) ------------------------------
# The archive fixture's superior-nf bbox is (-92.9, 47.0, -89.9, 48.3).


def _save(client, **kw):
    body = {"kind": "site", "lat": 47.65, "lon": -91.4, "name": None,
            "payload": None, "note": None, "rating": None, **kw}
    res = client.post("/saved", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def test_pins_returns_saved_places_inside_the_region_bbox(client):
    """A saved site inside the region shows up as a pin, with its snapshotted
    payload inlined so the offline card needs no further fetch."""
    _save(client, name="Near Mallard Lake", lat=47.65, lon=-91.4,
          payload={"total": 0.72, "rank": 3}, rating=4)

    fc = client.get("/field/regions/superior-nf/pins.geojson").json()

    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1
    props = fc["features"][0]["properties"]
    assert props["name"] == "Near Mallard Lake"
    assert props["score"] == pytest.approx(0.72)  # lifted from payload.total
    assert props["rating"] == 4
    assert props["payload"]["rank"] == 3  # whole payload rides along for the card
    assert fc["features"][0]["geometry"]["coordinates"] == pytest.approx([-91.4, 47.65])


def test_pins_excludes_places_outside_the_bbox(client):
    """Bbox-scoped (W1): a place beyond the downloaded rectangle is not a pin."""
    _save(client, name="Inside", lat=47.65, lon=-91.4)
    _save(client, name="Ohio", lat=40.0, lon=-82.0)

    fc = client.get("/field/regions/superior-nf/pins.geojson").json()

    assert [f["properties"]["name"] for f in fc["features"]] == ["Inside"]


def test_pins_is_empty_when_nothing_is_saved_inside(client):
    fc = client.get("/field/regions/superior-nf/pins.geojson").json()
    assert fc == {"type": "FeatureCollection", "features": []}


def test_pins_is_served_as_cacheable_geojson(client):
    """Mirrors the outline sidecar: geo+json, revalidated so a rebuilt region's
    pins can't be pinned stale, and cached by the page beside the archive."""
    res = client.get("/field/regions/superior-nf/pins.geojson")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/geo+json")
    assert res.headers["cache-control"] == "no-cache"


def test_pins_for_unknown_region_is_404(client):
    assert client.get("/field/regions/nope/pins.geojson").status_code == 404


def test_field_pins_layer_declares_point_geometry(client):
    """Same MapLibre trap as the basemap layers: a source's bucket isn't filtered
    by geometry type, so the pins circle layer must say it wants Points."""
    from cairn.api import _FIELD_DIR

    source = (_FIELD_DIR / "field.js").read_text()
    assert "['geometry-type'], 'Point'" in source
    block = source.split("id: 'pins'", 1)[1][:400]
    assert "filter:" in block, "pins layer has no geometry filter"


# --- Roads on the offline map ------------------------------------------------


@pytest.fixture
def roads_client(tmp_path, monkeypatch):
    """A region over the fake pipeline's synthetic roads, and a service backed by
    the fake Overpass so scoring ingests them without touching the network."""
    from cairn.service import RunService
    from tests.fakes.pipeline import FakeOverpass, fake_terrain_factory, synthetic_elements

    regions = tmp_path / "regions"
    regions.mkdir()
    h = bytearray(_archive())
    # bbox (-0.01, -0.01, 0.03, 0.02): contains the fakes' TEST_BBOX.
    struct.pack_into("<iiii", h, 102, -100000, -100000, 300000, 200000)
    (regions / "synthetic.pmtiles").write_bytes(bytes(h))
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        app.state.service = RunService(
            app.state.store, app.state.settings,
            overpass=FakeOverpass(synthetic_elements()),
            terrain_factory=fake_terrain_factory(slope_deg=3.0),
        )
        yield c
    config_module._settings = None


def test_roads_serves_the_regions_ingested_roads_slimmed(roads_client):
    roads_client.post("/score/view", json={"bbox": [0.0, 0.0, 0.02, 0.012]})

    res = roads_client.get("/field/regions/synthetic/roads.geojson")

    assert res.status_code == 200
    fc = res.json()
    assert fc["type"] == "FeatureCollection"
    assert fc["truncated"] is False
    assert fc["features"]
    feat = fc["features"][0]
    assert feat["geometry"]["type"] == "LineString"
    assert {"osm_id", "style", "label"} <= feat["properties"].keys()
    # The phone copy never carries nulls.
    assert all(v is not None for f in fc["features"] for v in f["properties"].values())


def test_roads_is_empty_where_nothing_was_ingested(roads_client):
    fc = roads_client.get("/field/regions/synthetic/roads.geojson").json()
    assert fc == {"type": "FeatureCollection", "truncated": False, "features": []}


def test_roads_is_served_as_cacheable_geojson(roads_client):
    res = roads_client.get("/field/regions/synthetic/roads.geojson")
    assert res.headers["content-type"].startswith("application/geo+json")
    assert res.headers["cache-control"] == "no-cache"


def test_roads_for_unknown_region_is_404(client):
    assert client.get("/field/regions/nope/roads.geojson").status_code == 404


def test_field_road_layers_declare_line_geometry():
    """The roads source only holds LineStrings today, but the same MapLibre trap
    applies the day it doesn't: every layer states the geometry it wants."""
    from cairn.api import _FIELD_DIR

    source = (_FIELD_DIR / "field.js").read_text()
    for layer in ("wr-selected", "wr-plain", "wr-rough", "wr-private", "wr-hit"):
        block = source.split(f"id: '{layer}'", 1)[1][:300]
        assert "filter:" in block, f"{layer} has no geometry filter"


def test_field_app_caches_roads_beside_the_archive_and_deletes_them_with_it():
    """Roads are only useful offline if they're in the same cache as the tiles,
    and a deleted region must not leave its roads behind."""
    from cairn.api import _FIELD_DIR

    source = (_FIELD_DIR / "field.js").read_text()
    assert "loadSidecar(roadsUrl(region.id))" in source
    assert "cache.delete(roadsUrl(region.id))" in source


# --- updates: an installed PWA is resumed, never reloaded --------------------


def test_version_reports_the_build_the_shell_is_stamped_with(client):
    """The page compares this with the ?v= on its own script to offer an
    update, so the two must be the same token."""
    res = client.get("/field/version")

    assert res.status_code == 200
    build = res.json()["build"]
    assert f"./field.js?v={build}" in client.get("/field/").text
    # A cached answer would read "up to date" with no server reachable.
    assert res.headers["cache-control"] == "no-store"


def test_service_worker_never_answers_the_version_check(client):
    sw = client.get("/field/sw.js").text
    assert "if (url.pathname === '/field/version') return;" in sw
    # ...and it sits before any cache-first/network-first handling.
    assert sw.index("'/field/version'") < sw.index("req.mode === 'navigate'")


def test_field_app_offers_an_update_when_the_server_build_differs():
    from cairn.api import _FIELD_DIR

    source = (_FIELD_DIR / "field.js").read_text()
    assert "searchParams.get('v')" in source  # PAGE_BUILD from our own script url
    assert "fetch('/field/version'" in source
    assert "serverBuild !== PAGE_BUILD" in source
    assert "$('update-check').onclick = checkForUpdate" in source


def test_field_app_points_an_http_install_at_the_https_address():
    """An iPhone gives an http:// page no Cache API and no service worker, so a
    home-screen icon added from the http address silently can't hold a map (a
    real re-install did exactly that). The page must say so and link https."""
    from cairn.api import _FIELD_DIR

    source = (_FIELD_DIR / "field.js").read_text()
    assert "location.protocol === 'http:'" in source
    assert "`https://${location.host}${location.pathname}`" in source


def test_sheet_is_dragged_by_its_top_not_its_body():
    """The sheet's body scrolls; only the pinned top moves the sheet. If the
    drag zone lost touch-action:none, iOS would scroll the page instead of
    delivering the pointer moves, and the sheet would stop following a finger."""
    from cairn.api import _FIELD_DIR

    css = (_FIELD_DIR / "field.css").read_text()
    js = (_FIELD_DIR / "field.js").read_text()
    top = css.split(".sheet-top {", 1)[1].split("}", 1)[0]
    assert "touch-action: none" in top and "position: sticky" in top
    assert "sheetTop.addEventListener('pointerdown'" in js
    assert "e.target.closest('button')" in js  # the Update button stays a button


def test_sheet_swipes_like_an_ios_sheet():
    """Fingers go through touch events decided on the FIRST move: iOS cancels a
    pointer drag once it calls the gesture a scroll, and ignores preventDefault
    after the first touchmove. Swipe-to-close from the body needs both."""
    from cairn.api import _FIELD_DIR

    js = (_FIELD_DIR / "field.js").read_text()
    assert "sheet.addEventListener('touchmove'" in js
    assert "}, { passive: false });" in js  # else preventDefault is a no-op
    # Body drags only when the list can't use the gesture.
    assert "(dy > 0 && atTop) || (dy < 0 && notFull)" in js
    css = (_FIELD_DIR / "field.css").read_text()
    handle = css.split(".handle {", 1)[1].split("}", 1)[0]
    assert "rgba(231, 237, 243" in handle  # a light pill, visible on the dark panel


def test_road_labels_draw_offline_from_vendored_glyphs(client):
    """Labels need glyph PBFs; fetched from a CDN they'd vanish with no signal.
    The style must point at the vendored font, the worker must precache it at
    the exact URL MapLibre requests, and the server must serve it."""
    from cairn.api import _FIELD_DIR

    js = (_FIELD_DIR / "field.js").read_text()
    assert "glyphs: GLYPHS" in js
    assert "/field/vendor/fonts/{fontstack}/{range}.pbf" in js
    assert "LABEL_FONT = ['Noto Sans Medium']" in js
    sw = client.get("/field/sw.js").text
    assert "'./vendor/fonts/Noto%20Sans%20Medium/0-255.pbf'" in sw
    res = client.get("/field/vendor/fonts/Noto%20Sans%20Medium/0-255.pbf")
    assert res.status_code == 200 and len(res.content) > 10_000
