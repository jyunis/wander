"""Wander Field — the offline field companion's region registry.

The field view (`web/field/`, served at `/field/`) is a separate, stripped-down
MapLibre PWA: **plan online, navigate offline**. The rich Leaflet app stays as-is
and connected; this half only has to keep working with no signal.

A **region** is one pre-baked vector basemap archive on disk under
`settings.regions_dir` (default `/data/regions`), built by
`scripts/build_region.py` from a Protomaps daily planet build. The client
downloads the whole archive once, over signal, into the Cache API; from then on
MapLibre reads tiles out of that local blob and never hits the network.

The PMTiles v3 header is **self-describing** — it carries the bbox, the zoom
range and a center — so this registry reads a region's metadata straight off
the file rather than from a sidecar manifest that could drift out of sync with
the bytes actually shipped. Only the display name is editorial (`REGION_NAMES`).

Like the overlays, this is display-only (TLC-3): a region is a basemap, never a
scoring input and never a claim that anywhere in it is legal to camp. The store
is inert when `regions_dir` is absent, so Cairn runs fine without any region
built.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path

# PMTiles v3 spec: a fixed 127-byte header, little-endian, magic "PMTiles" +
# version byte. https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md
_HEADER_LEN = 127
_MAGIC = b"PMTiles"
_VERSION = 3
# Byte offsets of the fields we surface. Coordinates are signed int32 in E7
# (degrees × 10^7); zooms are single bytes.
_OFF_MIN_ZOOM = 100
_OFF_MAX_ZOOM = 101
_OFF_BBOX = 102  # min_lon, min_lat, max_lon, max_lat — 4 × int32
_OFF_CENTER_ZOOM = 118
_OFF_CENTER = 119  # center_lon, center_lat — 2 × int32
_E7 = 1e7

# A region id is a filename stem. Constrained so it can never escape
# `regions_dir` when it arrives from a URL path (no dots, no separators).
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

SUFFIX = ".pmtiles"

# The real boundary of the thing a region is named after (e.g. the Superior NF
# proper), as tracked package data. A region's *downloaded area* is a rectangle
# of tiles — that rectangle is not the forest, and saying so on the map is the
# difference between "my map is broken" and "my download stops here". Optional:
# a region without a shape simply draws no outline.
_SHAPES_DIR = Path(__file__).parent / "data" / "region_shapes"
OUTLINE_SUFFIX = ".geojson"

# Editorial display names. An unlisted region falls back to its titleized id, so
# building a new region never requires a code change to show up in the UI.
REGION_NAMES: dict[str, str] = {
    "superior-nf": "Superior National Forest",
    "chippewa-nf": "Chippewa National Forest",
    "chequamegon-nicolet-nf": "Chequamegon-Nicolet National Forest",
}


class InvalidPMTiles(ValueError):
    """The file on disk isn't a PMTiles v3 archive we can read."""


@dataclass(frozen=True)
class Region:
    """One downloadable offline basemap, described by its own header bytes."""

    id: str
    name: str
    bbox: tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat
    center: tuple[float, float]  # lon, lat
    min_zoom: int
    max_zoom: int
    size_bytes: int
    built_at: float  # mtime, epoch seconds

    def as_dict(self) -> dict:
        """The wire shape for `GET /field/regions` (FC-9)."""
        payload = {
            "id": self.id,
            "name": self.name,
            "bbox": list(self.bbox),
            "center": list(self.center),
            "min_zoom": self.min_zoom,
            "max_zoom": self.max_zoom,
            "size_bytes": self.size_bytes,
            "built_at": self.built_at,
            "url": f"/field/regions/{self.id}{SUFFIX}",
            # Always present, unlike outline_url. Whether a region has any content
            # depends on a cairn.db read the registry deliberately doesn't do (it
            # reads only bytes off disk); the endpoint is the source of truth and
            # simply returns an empty FeatureCollection when nothing falls inside.
            "pins_url": f"/field/regions/{self.id}/pins{OUTLINE_SUFFIX}",
            # Same reasoning: always advertised, empty where nothing is ingested.
            "roads_url": f"/field/regions/{self.id}/roads{OUTLINE_SUFFIX}",
            "land_url": f"/field/regions/{self.id}/land{OUTLINE_SUFFIX}",
        }
        if outline_path(self.id) is not None:
            payload["outline_url"] = f"/field/regions/{self.id}/outline{OUTLINE_SUFFIX}"
        return payload


def valid_region_id(region_id: str) -> bool:
    """Whether `region_id` is safe to join onto `regions_dir`."""
    return bool(_ID_RE.match(region_id))


def outline_path(region_id: str) -> Path | None:
    """The region's real-world boundary polygon, if we ship one for it."""
    if not valid_region_id(region_id):
        return None
    path = _SHAPES_DIR / f"{region_id}{OUTLINE_SUFFIX}"
    return path if path.is_file() else None


def read_header(path: Path) -> dict:
    """Parse the 127-byte PMTiles v3 header. Raises `InvalidPMTiles`."""
    with path.open("rb") as fh:
        head = fh.read(_HEADER_LEN)
    if len(head) < _HEADER_LEN:
        raise InvalidPMTiles(f"{path.name}: truncated header ({len(head)}B)")
    if head[:7] != _MAGIC:
        raise InvalidPMTiles(f"{path.name}: bad magic {head[:7]!r}")
    if head[7] != _VERSION:
        raise InvalidPMTiles(f"{path.name}: unsupported PMTiles version {head[7]}")

    min_lon, min_lat, max_lon, max_lat = struct.unpack_from("<iiii", head, _OFF_BBOX)
    center_lon, center_lat = struct.unpack_from("<ii", head, _OFF_CENTER)
    return {
        "bbox": (min_lon / _E7, min_lat / _E7, max_lon / _E7, max_lat / _E7),
        "center": (center_lon / _E7, center_lat / _E7),
        "min_zoom": head[_OFF_MIN_ZOOM],
        "max_zoom": head[_OFF_MAX_ZOOM],
        "center_zoom": head[_OFF_CENTER_ZOOM],
    }


class RegionStore:
    """Lists the offline basemaps present on disk.

    Re-reads the directory on every call (like `OverlayStore`'s `mode=ro`
    reopen), so a region built while the server is up appears without a restart.
    Corrupt or half-written archives are skipped rather than failing the listing
    — a partial download shouldn't take the whole field view down.
    """

    def __init__(self, regions_dir: Path) -> None:
        self._dir = regions_dir

    def _name_for(self, region_id: str) -> str:
        """A user-drawn region carries the name its author typed, in a sidecar
        written by the builder. Otherwise the curated title, otherwise the id."""
        sidecar = self._dir / f"{region_id}.json"
        try:
            name = json.loads(sidecar.read_text()).get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except (OSError, ValueError):
            pass
        return REGION_NAMES.get(region_id, region_id.replace("-", " ").title())

    def _load(self, path: Path) -> Region | None:
        region_id = path.name[: -len(SUFFIX)]
        if not valid_region_id(region_id):
            return None
        try:
            head = read_header(path)
            stat = path.stat()
        except (InvalidPMTiles, OSError):
            return None
        return Region(
            id=region_id,
            name=self._name_for(region_id),
            bbox=head["bbox"],
            center=head["center"],
            min_zoom=head["min_zoom"],
            max_zoom=head["max_zoom"],
            size_bytes=stat.st_size,
            built_at=stat.st_mtime,
        )

    def list(self) -> list[Region]:
        """Every readable region, alphabetical by display name."""
        if not self._dir.is_dir():
            return []
        regions = [
            region
            for path in self._dir.glob(f"*{SUFFIX}")
            if (region := self._load(path)) is not None
        ]
        return sorted(regions, key=lambda r: r.name)

    def get(self, region_id: str) -> Region | None:
        """One region by id, or None if unknown/unreadable/unsafe."""
        if not valid_region_id(region_id):
            return None
        path = self._dir / f"{region_id}{SUFFIX}"
        if not path.is_file():
            return None
        return self._load(path)

    def path_for(self, region_id: str) -> Path | None:
        """The on-disk archive for `region_id`, if it exists and is readable."""
        if self.get(region_id) is None:
            return None
        return self._dir / f"{region_id}{SUFFIX}"


# --- Wander content on the offline map (#45 W1 — display-only, TLC-3) --------
# The region's own saved sites/campgrounds, served beside the archive as a
# GeoJSON sidecar (`pins.geojson`) that the field page caches next to the bytes
# so it survives offline — exactly like the `outline` sidecar. Each pin carries
# its whole `payload` (the score breakdown / campground meta already snapshotted
# in `saved_places.payload_json`), so the offline pin card needs zero network.


def pin_feature(place: dict) -> dict:
    """One saved place → a GeoJSON point Feature for the field map.

    `place` is a `Store._saved_row` dict: id/kind/name/lat/lon/payload/note/
    rating. A scored site's `payload` carries `total` (its absolute score); we
    surface that as `score` so the client can color the pin on the same ramp the
    online app uses. Campgrounds and dropped pins have no score.
    """
    payload = place.get("payload")
    score = payload.get("total") if isinstance(payload, dict) else None
    props = {
        "id": place.get("id"),
        "kind": place.get("kind"),
        "name": place.get("name"),
        "rating": place.get("rating"),
        "note": place.get("note"),
        "payload": payload,
    }
    if isinstance(score, (int, float)):
        props["score"] = score
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [place.get("lon"), place.get("lat")]},
        "properties": props,
    }


def pins_feature_collection(places: list[dict]) -> dict:
    """A FeatureCollection of a region's Wander content (#45 W1). Pure over the
    saved-place rows, so it unit-tests without a map or a DB."""
    return {
        "type": "FeatureCollection",
        "features": [pin_feature(p) for p in places],
    }


# --- Roads on the offline map (display-only, TLC-3) ---------------------------
# The basemap already draws OSM roads, but only as lines: no MVUM forest roads
# OSM is missing, no state-forest roads, and nothing to tap. This sidecar
# (`roads.geojson`) is Wander's own drivable-roads overlay (OSM ∪ MVUM ∪ MN DNR,
# the same set GET /roads serves), slimmed for a phone and cached beside the
# archive so "which spur do I take" still has an answer with no signal.
#
# Everything the card needs is decided HERE, not in field.js: the page stays a
# dumb renderer, and the MVUM wording is unit-tested instead of living in two
# copies of a regex.

# ~1.1 m at the equator, less further north: finer than a phone GPS fix, and it
# cuts the sidecar by roughly a third versus the store's full precision.
_COORD_DP = 5


def mvum_info(access: str | None) -> dict | None:
    """The MVUM access string (e.g. "Roads open to all Vehicles, Seasonal") as
    a plain-language class + season. Mirrors the online app's `mvumInfo`."""
    if not access:
        return None
    low = access.lower()
    season = "Seasonal" if "seasonal" in low else "Yearlong" if "yearlong" in low else None
    special = False
    if "all vehicles" in low:
        klass = "Open to all vehicles"
    elif "highway legal" in low:
        klass = "Highway-legal vehicles only"
    elif "special" in low:
        klass, special = "Special designation", True
    else:
        klass = access
    return {"class": klass, "season": season, "special": special}


def road_style(props: dict) -> str:
    """Which line the field map draws: private beats everything (you may not be
    allowed on it), then MVUM special designation, then forest road, then any
    other drivable way. Same precedence as the online overlay's colors."""
    if props.get("private"):
        return "private"
    mv = mvum_info(props.get("mvum_access"))
    if mv and mv["special"]:
        return "special"
    if props.get("forest_road"):
        return "forest"
    return "road"


def road_feature(feature: dict) -> dict | None:
    """One `/roads` feature → the slim field version, or None if it collapses
    to a point once rounded. Null properties are dropped (they're most of them)."""
    coords: list[list[float]] = []
    for lon, lat in feature["geometry"]["coordinates"]:
        pt = [round(lon, _COORD_DP), round(lat, _COORD_DP)]
        if not coords or pt != coords[-1]:
            coords.append(pt)
    if len(coords) < 2:
        return None

    p = feature.get("properties") or {}
    ref, name = p.get("ref"), p.get("name")
    mv = mvum_info(p.get("mvum_access"))
    # What the card is titled: a forest road is known by its number.
    label = f"FR {ref}" if ref else (name or "Road")
    props = {
        "osm_id": p.get("osm_id"),
        "style": road_style(p),
        "label": label,
        # Only a name that says something the label doesn't ("413" / "FR 413").
        "name": name if name and name not in (ref, label) else None,
        "highway": p.get("highway"),
        "mvum_class": mv["class"] if mv else None,
        "season": mv["season"] if mv else None,
        "surface": p.get("surface"),
        "mud_tier": p.get("mud_tier"),
        "rough": bool(p.get("rough")) or None,
        "private": bool(p.get("private")) or None,
        # The raw OSM value only when it says more than "private" does.
        "access": p.get("access") if p.get("private") and p.get("access") != "private" else None,
    }
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {k: v for k, v in props.items() if v is not None},
    }


def roads_feature_collection(roads: dict) -> dict:
    """The field sidecar for a `/roads`-shaped FeatureCollection. `truncated`
    rides along so the page can say the list is incomplete rather than let a
    missing spur read as "there is no road here"."""
    features = [f for f in (road_feature(x) for x in roads.get("features", [])) if f]
    return {
        "type": "FeatureCollection",
        "truncated": bool(roads.get("truncated")),
        "features": features,
    }


# --- Land on the offline map (display-only, TLC-3) ----------------------------
# The offline answer to "whose land am I standing on" — the question onX is paid
# for. A `land.geojson` sidecar per region: public-ownership parcels plus the
# forest/park/wilderness polygons, dissolved and clipped server-side
# (`OverlayStore.dissolved`), classified HERE so the page only paints and
# point-tests.
#
# The distinction that matters: a PARCEL says who owns the ground; a BOUNDARY
# (national/state forest, park) is a line drawn around land that still contains
# private inholdings. Only a parcel may ever read as "public". An absent parcel
# means "no public-land record" — private, or outside the data we hold — never
# "public because it's inside the forest".

# Every polygon layer the field map carries, in draw order (bottom first).
LAND_LAYERS: tuple[str, ...] = (
    "national_forest", "state_forest", "national_park", "state_park",
    "county_forest", "ownership", "wilderness",
)

# Dissolve simplification, degrees: ~3 m — under a phone GPS fix, and it strips
# most of the parcel-survey vertices a phone would otherwise download.
LAND_SIMPLIFY_DEG = 0.00003

_BOUNDARY_WHAT = {
    "national_forest": "National forest boundary",
    "state_forest": "State forest boundary",
    "national_park": "National Park Service unit",
    "state_park": "State park boundary",
}

# ownership category -> (cls, public, what). `public` is True (public land),
# False (NOT open without permission) or None (access varies).
_OWNERSHIP = {
    "Federal": ("federal", True, "Federal land"),
    "State": ("state", True, "State land"),
    "County": ("county", True, "County land"),
    "County Fee": ("county", True, "County land"),
    "Tax Forfeit": ("tax_forfeit", True, "Tax-forfeit land (county-managed)"),
    "Tribal": ("tribal", False, "Tribal land — not public without permission"),
    "NGO": ("ngo", None, "Conservation land (NGO) — access varies"),
}


def land_class(layer: str, category: str | None) -> dict:
    """What one land polygon means on the field map:
    `kind` parcel | boundary | wilderness, the paint `cls`, `public`
    (True / False / None = varies or not a claim), and a plain `what`."""
    if layer == "ownership":
        cls, public, what = _OWNERSHIP.get(category or "", ("other", None, category or "Land"))
        return {"kind": "parcel", "cls": cls, "public": public, "what": what}
    if layer == "county_forest":
        # WI county forests: the layer IS the county-owned land, not a boundary.
        return {"kind": "parcel", "cls": "county_forest", "public": True,
                "what": "County forest (county-owned)"}
    if layer == "wilderness":
        return {"kind": "wilderness", "cls": "wilderness", "public": None,
                "what": "Designated wilderness — no motor vehicles"}
    return {"kind": "boundary", "cls": layer, "public": None,
            "what": _BOUNDARY_WHAT.get(layer, layer.replace("_", " ").capitalize())}


def _round_ring(ring: list) -> list[list[float]] | None:
    out: list[list[float]] = []
    for lon, lat, *_ in ring:
        pt = [round(lon, _COORD_DP), round(lat, _COORD_DP)]
        if not out or pt != out[-1]:
            out.append(pt)
    if len(out) < 4:  # a closed ring needs 3 distinct corners + the closure
        return None
    if out[0] != out[-1]:
        out.append(out[0])
    return out


def _round_polygons(geom: dict) -> list[list[list[list[float]]]]:
    """Polygon/MultiPolygon → rounded polygons (outer ring first). A polygon
    whose outer ring collapses is dropped; a collapsed hole is just omitted."""
    if geom.get("type") == "Polygon":
        polys = [geom.get("coordinates") or []]
    elif geom.get("type") == "MultiPolygon":
        polys = geom.get("coordinates") or []
    else:
        return []
    out = []
    for rings in polys:
        if not rings:
            continue
        outer = _round_ring(rings[0])
        if outer is None:
            continue
        out.append([outer, *(r for r in map(_round_ring, rings[1:]) if r)])
    return out


def land_feature(layer: str, category: str | None, name: str | None, geom: dict) -> dict | None:
    """One dissolved land row → a field Feature, or None if nothing survives
    rounding. `name` is whatever the source calls it — a unit ("Bayfield County
    Forest") or, for MN parcels, just the county ("Itasca") — so the card shows
    it beside `what`, never instead of it."""
    polys = _round_polygons(geom)
    if not polys:
        return None
    props = {"layer": layer, **land_class(layer, category), "name": name, "category": category}
    geometry = (
        {"type": "Polygon", "coordinates": polys[0]}
        if len(polys) == 1
        else {"type": "MultiPolygon", "coordinates": polys}
    )
    return {"type": "Feature", "geometry": geometry,
            "properties": {k: v for k, v in props.items() if v is not None}}


def land_feature_collection(rows: list[tuple]) -> dict:
    """The field land sidecar from `OverlayStore.dissolved` rows, in LAND_LAYERS
    draw order so parcels paint over the boundaries they sit in."""
    order = {layer: i for i, layer in enumerate(LAND_LAYERS)}
    features = []
    for layer, category, name, gj in sorted(rows, key=lambda r: order.get(r[0], len(order))):
        feat = land_feature(layer, category, name, json.loads(gj) if isinstance(gj, str) else gj)
        if feat:
            features.append(feat)
    return {"type": "FeatureCollection", "features": features}
