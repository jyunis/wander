"""Build the MN UseDirect campground mapping (offline — Phase 2 / PR2).

Cairn's MN state-park campground markers are at campground-within-park
granularity ("Bear Paw Campground — Itasca State Park"), but the UseDirect
availability grid is keyed on a numeric FacilityId. This script resolves each
marker to its FacilityId ONCE, offline, and writes a committed JSON the serving
process loads at startup (src/cairn/data/usedirect_mn.json). Display-only data
(TLC-3); the serve path never reaches UseDirect to build this.

How: read the State-Park campground points from overlays.db, fetch the UseDirect
catalog (GET rdr/fd/places -> parks w/ PlaceId+coords; POST rdr/search/place per
park -> its Facilities w/ FacilityId+Name), then auto-match park-name -> Place
(name + coords sanity) and campground-name -> Facility (normalized name). A
hand-curated MANUAL_OVERRIDES table fills the fuzzy-match tail. Unmatched markers
are printed for review and simply stay link-out at serve time (degradable).

Re-runnable. Network via stdlib urllib (browser UA), same posture as
load_overlays.py. Run in the dev image:
  docker run --rm -v /extra1/docker/cairn:/app -w /app --entrypoint uv cairn:dev \
    run python scripts/build_usedirect_map.py
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.request

BASE = "https://mnrdr.usedirect.com/minnesotardr/rdr"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
OVERLAYS_DB = os.environ.get("OVERLAYS_DB", "/extra1/docker/cairn/data/overlays.db")
OUT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "cairn", "data", "usedirect_mn.json"
)
# A future in-season date just to satisfy the grid/place endpoints; the facility
# LIST they return is structural (date-independent).
PROBE_DATE = "2026-07-15"

# Hand-resolved markers the auto-matcher can't place (reviewed 2026-06-12).
# marker name (exact, from overlays.db) -> FacilityId, or a list of FacilityIds
# when a campground is split into loop-facilities.
MANUAL_OVERRIDES: dict[str, int | list[int]] = {
    "Hike-In Campground — Afton State Park": 778,  # UseDirect facility "Hike In"
    # Lake Bronson's drive-in camping is one UseDirect facility ("Two Rivers
    # Campground"); both our Lakeside markers point at it.
    "Lakeside Campground — Lake Bronson State Park": 842,
    "Lakeside Extension Campground — Lake Bronson State Park": 842,
    # Zippel Bay splits this one marker into two loop-facilities -> aggregate.
    "Ridge & Birch Campground — Zippel Bay State Park": [1098, 1099],
}

# Markers with NO UseDirect facility (canoe-in / equestrian / overflow sites MN
# doesn't list as separately reservable) -> correctly stay link-out. Listed so
# they don't reappear in the unmatched report. Verified 2026-06-12.
KNOWN_UNMAPPED: set[str] = {
    "Boat/Canoe Campground — Afton State Park",
    "Equestrian Campground — Camden State Park",
    "Lower Campground — Lac Qui Parle State Park",
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_PARK_SUFFIXES = (
    "state recreation area", "state park", "state forest", "recreation area",
)


def park_core(name: str) -> str:
    n = norm(name)
    for suf in _PARK_SUFFIXES:
        if n.endswith(suf):
            n = n[: -len(suf)].strip()
    return n


def cg_core(name: str) -> str:
    """Strip generic words so 'Lower Campground' ~ 'Lower Loop' style names
    compare on their distinguishing token(s)."""
    n = norm(name)
    n = re.sub(r"\b(campground|loop|area|sites?|camp)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def haversine_km(a_lat, a_lon, b_lat, b_lon) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _get(path: str) -> object:
    req = urllib.request.Request(f"{BASE}/{path}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _post(path: str, body: dict) -> object:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE}/{path}",
        data=data,
        headers={"User-Agent": UA, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def fetch_places() -> list[dict]:
    """All MN UseDirect places: [{PlaceId, Name, Latitude, Longitude}]."""
    return [
        {
            "PlaceId": p["PlaceId"],
            "Name": p.get("Name") or "",
            "lat": p.get("Latitude"),
            "lon": p.get("Longitude"),
        }
        for p in _get("fd/places")
    ]


def fetch_facilities(place_id: int) -> list[dict]:
    """A place's facilities: [{FacilityId, Name}] (cabins/lodges included —
    name-matching against a campground marker filters them out)."""
    body = {
        "PlaceId": place_id, "StartDate": PROBE_DATE, "Nights": 1,
        "UnitCategoryId": 0, "SleepingUnitId": 0, "MinVehicleLength": 0,
        "UnitTypesGroupIds": [], "InSeasonOnly": False, "WebOnly": True,
        "IsADA": False, "RestrictADA": False, "UnitSort": "orderby",
    }
    d = _post("search/place", body)
    place = d.get("SelectedPlace") or d.get("Place") or {}
    facs = place.get("Facilities") or {}
    if isinstance(facs, dict):
        facs = list(facs.values())
    return [{"FacilityId": f.get("FacilityId"), "Name": f.get("Name") or ""} for f in facs]


def read_markers(db: str) -> list[dict]:
    # Scope to MN state-PARK campgrounds (the dnr.state.mn.us markers) — those
    # are what UseDirect covers cleanly. WI parks (GoingToCamp) and the null-url
    # state-forest campgrounds stay link-out by design, so we don't map them.
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.enable_load_extension(True)
    con.load_extension("mod_spatialite")
    rows = con.execute(
        "SELECT name, X(geom), Y(geom) FROM overlay_points "
        "WHERE layer='campground' AND category='State Park' "
        "AND url LIKE '%dnr.state.mn.us%' ORDER BY name"
    ).fetchall()
    con.close()
    return [{"name": n, "lon": x, "lat": y} for (n, x, y) in rows]


def split_marker(name: str) -> tuple[str, str]:
    """'Bear Paw Campground — Itasca State Park' -> ('Bear Paw Campground',
    'Itasca State Park'). No em-dash -> (whole, whole)."""
    if "—" in name:
        cg, park = name.split("—", 1)
        return cg.strip(), park.strip()
    return name.strip(), name.strip()


def match_place(park_name: str, lat: float, lon: float, places: list[dict]) -> dict | None:
    core = park_core(park_name)
    by_name = [p for p in places if park_core(p["Name"]) == core]
    if len(by_name) == 1:
        return by_name[0]
    cands = by_name or places
    # Disambiguate / fall back by proximity (parks are large; 25 km is safe).
    best, best_d = None, 1e9
    for p in cands:
        if p["lat"] is None or p["lon"] is None:
            continue
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        if d < best_d:
            best, best_d = p, d
    return best if best and best_d < 25 else None


def match_facility(cg_name: str, facs: list[dict]) -> int | list[int] | None:
    """Resolve a campground marker to a FacilityId, or a LIST of FacilityIds
    when UseDirect splits one campground into loop-facilities (e.g. Itasca's
    "Pine Ridge Campground" -> Oak/Maple/Pine loops) — the serve path sums a
    list. Returns None when ambiguous (-> hand-resolve)."""
    core = cg_core(cg_name)
    camp_facs = [f for f in facs if re.search(r"\bcamp", norm(f["Name"]))]
    pool = camp_facs or facs
    # Exact distinguishing-token match (one facility) wins.
    exact = [f for f in pool if cg_core(f["Name"]) == core]
    if len(exact) == 1:
        return exact[0]["FacilityId"]
    # Facilities whose name contains the marker core. 1 -> that facility;
    # several -> a loop-split, aggregate them.
    if core:
        hits = [f for f in pool if core in cg_core(f["Name"])] or [
            f for f in pool if core in norm(f["Name"])
        ]
        if len(hits) == 1:
            return hits[0]["FacilityId"]
        if len(hits) > 1:
            return [h["FacilityId"] for h in hits]
    # A single campground facility in the park -> unambiguous.
    if len(camp_facs) == 1:
        return camp_facs[0]["FacilityId"]
    return None


def main() -> int:
    markers = read_markers(OVERLAYS_DB)
    print(f"[usedirect] {len(markers)} MN state-park markers", flush=True)
    places = fetch_places()
    print(f"[usedirect] {len(places)} UseDirect places", flush=True)

    fac_cache: dict[int, list[dict]] = {}
    mapping: dict[str, int] = {}
    unmatched: list[str] = []

    for m in markers:
        name = m["name"]
        if name in MANUAL_OVERRIDES:
            mapping[name] = MANUAL_OVERRIDES[name]
            continue
        if name in KNOWN_UNMAPPED:
            continue
        cg, park = split_marker(name)
        place = match_place(park, m["lat"], m["lon"], places)
        if place is None:
            unmatched.append(f"{name}  [no place for park {park!r}]")
            continue
        pid = place["PlaceId"]
        if pid not in fac_cache:
            try:
                fac_cache[pid] = fetch_facilities(pid)
            except Exception as exc:  # noqa: BLE001 - best-effort offline tool
                fac_cache[pid] = []
                print(f"[usedirect]   facilities fetch failed for {pid}: {exc}", flush=True)
        fid = match_facility(cg, fac_cache[pid])
        if fid is None:
            facnames = [f["Name"] for f in fac_cache[pid]]
            unmatched.append(f"{name}  -> {place['Name']} (PlaceId {pid}); facilities={facnames}")
            continue
        mapping[name] = fid

    out = {name: mapping[name] for name in sorted(mapping)}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\n[usedirect] mapped {len(out)}/{len(markers)} -> {OUT_PATH}", flush=True)
    if unmatched:
        print(f"\n[usedirect] {len(unmatched)} UNMATCHED (hand-resolve into MANUAL_OVERRIDES):")
        for u in unmatched:
            print("  -", u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
