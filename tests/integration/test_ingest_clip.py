"""Ingest-time geometry clipping (perf, exact within the search radius).

An oversized area RELATION (the Lake-Superior pathology: one ~219k-vertex
relation whose MBR spans the whole region, so the R*Tree can't prune it and
every candidate runs ST_Distance against the full lake outline) has its far
member rings dropped at WKT build, bounded to the score bbox + buffer. The
contract under test: a distance computed within the feature's search radius is
UNCHANGED by clipping (buffer >= radius), while the stored geometry collapses.

Clipping happens on the raw coordinates, not via ST_Intersection, because the
member-assembled relation geometry OSM returns is topologically invalid (GEOS
ops return NULL on it).
"""

from __future__ import annotations

import pytest

from cairn.config import Settings
from cairn.ingest.osm import element_to_wkt, ingest_osm
from cairn.store import Store
from cairn.store.types import Bbox

BBOX = Bbox(-91.30, 47.70, -91.10, 47.90)
P_LON, P_LAT = -91.20, 47.80


def _way(lats_lons):
    return {"type": "way", "geometry": [{"lat": la, "lon": lo} for la, lo in lats_lons]}


def _lake_relation():
    """A 'lake' as an OSM water relation: a short local south-shore fragment
    ~560 m north of P (the only one near the bbox) plus a far north shore
    (~67 km away) sampled densely enough to blow past the clip threshold."""
    near = _way([(47.805, -91.25), (47.805, -91.20), (47.805, -91.15)])  # local shore
    n = 2600
    far = _way([(48.40, -95.0 + 6.0 * i / n) for i in range(n + 1)])     # far shore, dense
    return {
        "type": "relation", "id": 4039486, "tags": {"natural": "water", "name": "Big Lake"},
        "members": [{"type": "way", "ref": 1, **near}, {"type": "way", "ref": 2, **far}],
    }


async def _open(tmp_path, name) -> Store:
    d = tmp_path / name
    d.mkdir()
    return await Store.open(Settings(_env_file=None, data_dir=d))


async def _npoints(store: Store) -> int:
    cur = await store.db.execute(
        "SELECT SUM(ST_NPoints(geom)) FROM osm_features WHERE feature_class='water'"
    )
    return (await cur.fetchone())[0]


async def test_clip_preserves_in_radius_distance_and_shrinks_geometry(tmp_path):
    rel = _lake_relation()
    clipped = await _open(tmp_path, "clipped")
    unclipped = await _open(tmp_path, "unclipped")
    try:
        await ingest_osm(clipped, [rel], chunk_size=100, clip_bbox=BBOX,
                         clip_buffer_m=3500.0, clip_vertex_threshold=2000)
        await ingest_osm(unclipped, [rel], chunk_size=100)  # no clip — full geometry

        n_clip, n_full = await _npoints(clipped), await _npoints(unclipped)
        assert n_full > 2600                       # far shore retained in full
        assert n_clip < 50, f"far shore should be dropped, got {n_clip} verts"

        d_clip = await clipped.nearest_distance_m(P_LON, P_LAT, ("water",), 600.0)
        d_full = await unclipped.nearest_distance_m(P_LON, P_LAT, ("water",), 600.0)
        assert d_clip is not None and d_full is not None
        assert d_clip == pytest.approx(d_full, abs=0.5)   # identical nearest distance
        assert 400 < d_clip < 700                          # ~560 m, a real (unsaturated) read
    finally:
        await clipped.close()
        await unclipped.close()


def test_small_relation_not_clipped():
    # A modest relation (under the threshold) keeps all its rings even with a
    # clip box set — clipping only touches oversized geometries.
    small = {
        "type": "relation", "id": 7, "tags": {"natural": "water"},
        "members": [
            {"type": "way", "ref": 1,
             **_way([(48.40, -95.0), (48.40, -94.9), (48.41, -94.9), (48.40, -95.0)])},
        ],
    }
    box = (-91.34, 47.66, -91.06, 47.94)  # far from the ring, but under threshold
    wkt = element_to_wkt(small, "water", box, 2000)
    assert wkt.startswith("MULTIPOLYGON")
    assert "-95" in wkt  # the far ring is retained (not dropped)


def test_lines_and_ways_unaffected_by_clip():
    # A drivable LINESTRING way is never clipped (it's read unclipped for
    # terminus detection); clip args are simply ignored for non-relations.
    way = _way([(47.80, -91.20), (47.81, -91.19)])
    way["type"] = "way"
    wkt = element_to_wkt(way, "drivable", (-91.3, 47.7, -91.1, 47.9), 2000)
    assert wkt.startswith("LINESTRING")
