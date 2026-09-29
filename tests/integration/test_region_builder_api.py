"""The "download this area" job: POST a drawn box, poll, then it's a region.

Runs against a FAKE `pmtiles` binary. The real one pulls tens of MB off
build.protomaps.com; what needs testing here is the contract around it — that a
half-written archive is never visible as a region, that a failed extract leaves
nothing behind, and that a finished one shows up under the name the user typed.
"""

from __future__ import annotations

import json
import struct
import time

import pytest
from fastapi.testclient import TestClient

import cairn.config as config_module

BOX = [-91.9, 47.85, -91.6, 48.05]  # a real trip-sized box: ~331 tiles, <1 MB


def _valid_archive_bytes(payload: bytes = b"tiles") -> bytes:
    header = bytearray(127)
    header[0:7] = b"PMTiles"
    header[7] = 3
    header[100], header[101] = 0, 14
    struct.pack_into("<iiii", header, 102, -919000000, 478500000, -916000000, 480500000)
    header[118] = 8
    struct.pack_into("<ii", header, 119, -917500000, 479500000)
    return bytes(header) + payload


def _fake_pmtiles(tmp_path, *, exit_code=0, stderr="", delay=0.0, dry_run_mb=2.2):
    """A stand-in for the CLI, covering both modes we drive:

        pmtiles extract <url> <out> --bbox=... --maxzoom=N [--dry-run]

    `--dry-run` must print the line the real tool prints, since we parse it.
    """
    script = tmp_path / "fake-pmtiles"
    archive = _valid_archive_bytes()
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time, pathlib\n"
        "if '--dry-run' in sys.argv:\n"
        "    print('Region tiles 331, result tile entries 331')\n"
        "    print('Extract transferred 2.3 MB (overfetch 0.05) "
        f"for an archive size of {dry_run_mb} MB')\n"
        "    sys.exit(0)\n"
        f"time.sleep({delay})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"if {exit_code} == 0:\n"
        "    pathlib.Path(sys.argv[3]).write_bytes(" + repr(archive) + ")\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


async def _roads_ok(self, bbox):  # noqa: ARG001 - stands in for RunService.ingest_roads
    return 42


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    # Exists before any test writes into it (e.g. to plant a stale partial).
    (tmp_path / "regions").mkdir()

    def _make(roads=None, **fake_kw):
        # A region build also ingests the box's roads via Overpass/MVUM; never
        # let a test reach the network for them. Patched on the class because
        # the builder binds the service's method at startup.
        monkeypatch.setattr("cairn.service.RunService.ingest_roads", roads or _roads_ok)
        monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("CAIRN_PMTILES_BIN", str(_fake_pmtiles(tmp_path, **fake_kw)))
        config_module._settings = None
        from cairn.api import app

        return TestClient(app)

    return _make


def _drain(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/field/jobs/{job_id}").json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished: {body}")


def test_drawn_box_becomes_a_region_under_its_own_name(client_factory, monkeypatch):
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory() as client:
        res = client.post("/field/regions", json={"name": "BWCA trip", "bbox": BOX, "maxzoom": 14})
        assert res.status_code == 202
        job = res.json()
        assert job["region_id"] == "bwca-trip"
        # The denominator of the progress bar is the dry run's answer, not a guess.
        assert job["estimated_bytes"] == 2_200_000

        done = _drain(client, job["job_id"])
        assert done["state"] == "done", done
        assert done["error"] is None

        regions = client.get("/field/regions").json()["regions"]
        assert [r["id"] for r in regions] == ["bwca-trip"]
        # The name the user typed survives, not a titleized slug.
        assert regions[0]["name"] == "BWCA trip"


def test_a_failed_extract_leaves_no_region_and_no_partial(client_factory, tmp_path, monkeypatch):
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory(exit_code=1, stderr="upstream 503\n") as client:
        job = client.post("/field/regions", json={"name": "Doomed", "bbox": BOX}).json()
        done = _drain(client, job["job_id"])

        assert done["state"] == "error"
        assert "503" in done["error"]
        assert client.get("/field/regions").json()["regions"] == []
        # Nothing left on disk — a truncated archive would be worse than none.
        assert list((tmp_path / "regions").glob("*")) == []


def test_partial_archives_are_never_listed_as_regions(client_factory, tmp_path, monkeypatch):
    """A killed run leaves *.pmtiles.partial; the registry must not see it."""
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    (tmp_path / "regions" / "halfway.pmtiles.partial").write_bytes(_valid_archive_bytes())
    with client_factory() as client:
        assert client.get("/field/regions").json()["regions"] == []


def test_boot_sweeps_stale_partials(client_factory, tmp_path, monkeypatch):
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    stale = tmp_path / "regions" / "killed.pmtiles.partial"
    stale.write_bytes(b"x" * 1000)
    with client_factory():
        assert not stale.exists()


def test_an_oversized_box_is_refused_before_any_work(client_factory, monkeypatch):
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory() as client:
        res = client.post(
            "/field/regions", json={"name": "Half of Canada", "bbox": [-120.0, 45.0, -80.0, 60.0]}
        )
        assert res.status_code == 422
        assert "too big" in json.dumps(res.json())


def test_a_duplicate_name_is_refused(client_factory, monkeypatch):
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory() as client:
        job = client.post("/field/regions", json={"name": "BWCA trip", "bbox": BOX}).json()
        _drain(client, job["job_id"])
        res = client.post("/field/regions", json={"name": "BWCA trip", "bbox": BOX})
        assert res.status_code == 422
        assert "already exists" in json.dumps(res.json())


def test_regions_can_be_deleted(client_factory, monkeypatch):
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory() as client:
        job = client.post("/field/regions", json={"name": "Scratch", "bbox": BOX}).json()
        _drain(client, job["job_id"])
        assert client.delete("/field/regions/scratch").status_code == 200
        assert client.get("/field/regions").json()["regions"] == []
        assert client.delete("/field/regions/scratch").status_code == 404


def test_unknown_job_is_404(client_factory, monkeypatch):
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory() as client:
        assert client.get("/field/jobs/deadbeef").status_code == 404


def test_limits_are_served_so_the_picker_shares_our_constants(client_factory, monkeypatch):
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory() as client:
        limits = client.get("/field/regions").json()["limits"]
        assert limits["max_bytes"] == 150_000_000
        assert limits["max_tiles"] == 80_000
        assert limits["max_zoom"] == 14


def test_estimate_reports_the_dry_runs_answer(client_factory, monkeypatch):
    """Size comes from `pmtiles --dry-run`, not a bytes/tile constant — no such
    constant survives two bounding boxes (Superior 1939 B/tile, BWCA 6629)."""
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory() as client:
        body = client.get("/field/estimate", params={"bbox": ",".join(map(str, BOX))}).json()
        assert body["tiles"] == 331
        assert body["bytes"] == 2_200_000
        assert body["over_cap"] is False


def test_a_box_over_the_byte_cap_is_refused_with_a_real_number(client_factory, monkeypatch):
    """The tile pre-filter can't see this: a dense box is six times the bytes of
    a sparse one with the same tile count."""
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory(dry_run_mb=900.0) as client:
        res = client.post("/field/regions", json={"name": "Dense", "bbox": BOX})
        assert res.status_code == 422
        assert "900 MB" in json.dumps(res.json())
        assert client.get("/field/regions").json()["regions"] == []


def test_estimate_rejects_an_absurd_box_without_spawning_pmtiles(client_factory, monkeypatch):
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory() as client:
        res = client.get("/field/estimate", params={"bbox": "-120,45,-80,60"})
        assert res.status_code == 422
        assert "too big" in json.dumps(res.json())


def _make_stub_build_url():
    async def _stub(_settings):
        return "https://build.protomaps.example/20260710.pmtiles"

    return _stub


# --- the region's roads ride along with the extract --------------------------


def test_a_built_region_reports_its_roads(client_factory, monkeypatch):
    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory() as client:
        job = client.post("/field/regions", json={"name": "BWCA trip", "bbox": BOX}).json()
        done = _drain(client, job["job_id"])

        assert done["state"] == "done"
        assert done["roads"] == {"state": "done", "count": 42}


def test_a_roads_failure_still_lands_the_region(client_factory, monkeypatch):
    """Roads are best effort: a basemap without them still navigates. The job
    says so, so the UI can warn instead of pretending the area has no roads."""

    async def roads_fail(self, bbox):  # noqa: ARG001
        raise RuntimeError("overpass-api.de 504")

    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory(roads=roads_fail) as client:
        job = client.post("/field/regions", json={"name": "BWCA trip", "bbox": BOX}).json()
        done = _drain(client, job["job_id"])

        assert done["state"] == "done"
        assert done["roads"] == {"state": "error", "error": "overpass-api.de 504"}
        assert [r["id"] for r in client.get("/field/regions").json()["regions"]] == ["bwca-trip"]


def test_a_region_is_not_listed_until_its_roads_are_in(client_factory, monkeypatch):
    """The phone caches the roads sidecar the first time it shows a region. If
    the region were listed while roads were still ingesting, a phone could cache
    an empty sidecar and carry it into the woods."""
    import asyncio

    async def roads_slow(self, bbox):  # noqa: ARG001
        await asyncio.sleep(1.0)
        return 7

    monkeypatch.setattr("cairn.regionbuild.find_build_url", _make_stub_build_url())
    with client_factory(roads=roads_slow) as client:
        job = client.post("/field/regions", json={"name": "BWCA trip", "bbox": BOX}).json()
        time.sleep(0.4)  # the fake extract has finished; the roads haven't

        mid = client.get(f"/field/jobs/{job['job_id']}").json()
        assert mid["state"] == "running"
        assert mid["roads"] == {"state": "running"}
        assert client.get("/field/regions").json()["regions"] == []

        done = _drain(client, job["job_id"])
        assert done["roads"] == {"state": "done", "count": 7}
        assert [r["id"] for r in client.get("/field/regions").json()["regions"]] == ["bwca-trip"]
