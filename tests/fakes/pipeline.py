"""Fakes for end-to-end pipeline tests: a canned Overpass + a fake terrain."""

from __future__ import annotations

from cairn.clients.overpass import QUERY_VERSION, OverpassResult
from cairn.store.types import Bbox


class FakeOverpass:
    """Returns a fixed element set; records that fetch was called."""

    def __init__(self, elements: list[dict]) -> None:
        self._elements = elements
        self.calls = 0

    async def fetch(self, bbox: Bbox) -> OverpassResult:
        self.calls += 1
        return OverpassResult(
            elements=self._elements,
            cache_key=f"fake/v{QUERY_VERSION}",
            query_version=QUERY_VERSION,
            fetched_at="2026-06-09T00:00:00+00:00",
            from_cache=False,
        )

    async def aclose(self) -> None:
        pass


class FakeTerrain:
    """Constant-slope sampler. Matches the TerrainSampler interface used by
    the service (slope_deg / aclose / tiles_used)."""

    def __init__(self, slope_deg: float = 3.0) -> None:
        self._slope = slope_deg
        self.tiles_used: set = set()

    async def slope_deg(self, lon: float, lat: float) -> float:
        self.tiles_used.add((12, int(lon * 1000), int(lat * 1000)))
        return self._slope

    async def aclose(self) -> None:
        pass


def fake_terrain_factory(slope_deg: float = 3.0):
    def factory(settings, zoom):  # noqa: ARG001 - signature parity with TerrainSampler
        return FakeTerrain(slope_deg)

    return factory


# A small synthetic OSM extract near (lon 0.01, lat 0.01):
#  - a drivable track running W->E that passes THROUGH the forest and near water
#  - a water polygon, a forest polygon, a major highway (south), a village node
def synthetic_elements(include_water: bool = True) -> list[dict]:
    track = {
        "type": "way",
        "id": 100,
        "tags": {"highway": "track"},
        "geometry": [
            {"lon": 0.002, "lat": 0.010},
            {"lon": 0.006, "lat": 0.010},
            {"lon": 0.010, "lat": 0.010},
            {"lon": 0.014, "lat": 0.010},
            {"lon": 0.018, "lat": 0.010},
        ],
    }
    forest = {
        "type": "way",
        "id": 200,
        "tags": {"landuse": "forest"},
        "geometry": [
            {"lon": 0.0145, "lat": 0.0098},
            {"lon": 0.0155, "lat": 0.0098},
            {"lon": 0.0155, "lat": 0.0102},
            {"lon": 0.0145, "lat": 0.0102},
            {"lon": 0.0145, "lat": 0.0098},
        ],
    }
    highway = {
        "type": "way",
        "id": 300,
        "tags": {"highway": "primary"},
        "geometry": [
            {"lon": 0.002, "lat": 0.000},
            {"lon": 0.018, "lat": 0.000},
        ],
    }
    village = {
        "type": "node",
        "id": 400,
        "lon": 0.019,
        "lat": 0.000,
        "tags": {"place": "village"},
    }
    elements = [track, forest, highway, village]
    if include_water:
        water = {
            "type": "way",
            "id": 500,
            "tags": {"natural": "water"},
            "geometry": [
                {"lon": 0.0040, "lat": 0.0100},
                {"lon": 0.0050, "lat": 0.0100},
                {"lon": 0.0050, "lat": 0.0104},
                {"lon": 0.0040, "lat": 0.0104},
                {"lon": 0.0040, "lat": 0.0100},
            ],
        }
        elements.append(water)
    return elements


TEST_BBOX = Bbox(0.0, 0.0, 0.02, 0.012)
