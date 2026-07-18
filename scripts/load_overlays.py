#!/usr/bin/env python3
"""Load static land-overlay reference layers into ``/data/overlays.db``.

These are **display-only** layers — public land ownership (incl. tax-forfeited),
forest/park boundaries, and campgrounds. Per TLC-3 they are NEVER scored or used
to assert a site is legal; Cairn renders them as visual context only. The data
is public (per-state DNR downloads + the national USFS / Recreation.gov services).

Sources live in the ``STATES`` registry (near the bottom): adding a state is
adding a block there. National feeds (USFS national forests, RIDB federal
campgrounds) cover every configured state automatically; state park/forest
boundaries and DNR campgrounds are bespoke per state. Every source for a layer
unions under one shared layer id, so the store/API/UI stay state-blind.

The overlay store is a separate SpatiaLite file from ``cairn.db`` so the scoring
DB stays lean, and it is fully re-derivable — re-running this script rebuilds each
layer across all configured states. It IS kept in the nightly backup anyway (a
consistent sqlite3 ``.backup`` snapshot under ``dump/``), since a rebuild depends
on the upstream DNR/USFS/RIDB sources still being live.

Run in-image with network access (pyshp + shapely added on the fly):

    docker run --rm -v /extra1/docker/cairn:/app -w /app \
      --entrypoint uv cairn:dev \
      run --with pyshp --with shapely python scripts/load_overlays.py

Env: OVERLAYS_DB (default /data/overlays.db).
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

# Only the SHP/ArcGIS polygon loaders need these; the OSM (campground/dispersed)
# path and the pure tag filters don't. Guarded so the module imports for tests
# even where pyshp/shapely aren't installed (run loaders with `--with pyshp shapely`).
try:
    import shapefile  # pyshp
    from shapely.geometry import shape
except ModuleNotFoundError:  # pragma: no cover
    shapefile = None
    shape = None

# `h3` is only needed by the cell-coverage loader (FCC BDC mobile hexes). Guarded
# like pyshp/shapely so the module still imports for tests where it's absent (run
# that loader with `--with h3`).
try:
    import h3
except ModuleNotFoundError:  # pragma: no cover
    h3 = None

DB = os.environ.get("OVERLAYS_DB", "/data/overlays.db")
UA = {"User-Agent": "cairn-overlays-loader (+http://cairn.home.arpa)"}
GDRS = "https://resources.gisdata.mn.gov/pub/gdrs/data/pub"

# USFS national-forest administrative boundaries — the one live ArcGIS source
# (keyless). Queried per a lon/lat envelope and returned in EPSG:4326. The
# per-state SHP boundary sources live in the STATES registry near the bottom.
USFS_URL = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/"
    "EDW_ForestSystemBoundaries_01/MapServer/0/query"
)


def _usfs_query(bbox: tuple[float, float, float, float]) -> str:
    minlon, minlat, maxlon, maxlat = bbox
    return (
        f"{USFS_URL}?where=1%3D1&geometry={minlon},{minlat},{maxlon},{maxlat}"
        "&geometryType=esriGeometryEnvelope&inSR=4326&outSR=4326"
        "&spatialRel=esriSpatialRelIntersects&outFields=FORESTNAME"
        "&returnGeometry=true&f=geojson"
    )


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.enable_load_extension(True)
    con.load_extension("mod_spatialite")
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    if not cur.execute(
        "SELECT name FROM sqlite_master WHERE name='spatial_ref_sys'"
    ).fetchone():
        cur.execute("SELECT InitSpatialMetaData(1)")  # loads full EPSG set

    def exists(t):
        return cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()

    # Polygon layers (ownership + boundaries).
    if not exists("overlay_features"):
        cur.execute(
            "CREATE TABLE overlay_features ("
            "  fid INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  layer TEXT NOT NULL, category TEXT, name TEXT)"
        )
        cur.execute("SELECT AddGeometryColumn('overlay_features','geom',4326,'MULTIPOLYGON','XY')")
        cur.execute("SELECT CreateSpatialIndex('overlay_features','geom')")
        cur.execute("CREATE INDEX idx_overlay_layer ON overlay_features(layer)")
    # Point layers (campgrounds; `url` = reservation link, `meta` = JSON detail).
    if not exists("overlay_points"):
        cur.execute(
            "CREATE TABLE overlay_points ("
            "  fid INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  layer TEXT NOT NULL, category TEXT, name TEXT, url TEXT, meta TEXT)"
        )
        cur.execute("SELECT AddGeometryColumn('overlay_points','geom',4326,'POINT','XY')")
        cur.execute("SELECT CreateSpatialIndex('overlay_points','geom')")
        cur.execute("CREATE INDEX idx_overlay_pts_layer ON overlay_points(layer)")
    else:  # migrate: add the meta column to a pre-existing table
        cols = [r[1] for r in cur.execute("PRAGMA table_info(overlay_points)").fetchall()]
        if "meta" not in cols:
            cur.execute("ALTER TABLE overlay_points ADD COLUMN meta TEXT")
    con.commit()


def clear_layer(con: sqlite3.Connection, layer: str) -> None:
    con.execute("DELETE FROM overlay_features WHERE layer=?", (layer,))
    con.commit()


def _insert(con, layer, rows_wkt_srid):
    """rows_wkt_srid: iterable of (category, name, wkt, srid)."""
    con.executemany(
        "INSERT INTO overlay_features(layer, category, name, geom) "
        "VALUES (?, ?, ?, CastToMulti(Transform(GeomFromText(?, ?), 4326)))",
        [(layer, c, n, wkt, srid) for (c, n, wkt, srid) in rows_wkt_srid],
    )
    con.commit()


CAMPGROUND_LAYER = "campground"


def _insert_points(con, layer, rows):
    """rows: iterable of (category, name, url, meta_dict|None, wkt, srid)."""
    con.executemany(
        "INSERT INTO overlay_points(layer, category, name, url, meta, geom) "
        "VALUES (?, ?, ?, ?, ?, Transform(GeomFromText(?, ?), 4326))",
        [
            (layer, c, n, u, json.dumps(m) if m else None, wkt, srid)
            for (c, n, u, m, wkt, srid) in rows
        ],
    )
    con.commit()


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("y", "yes", "1", "true", "t") if v is not None else False


def _compact(d: dict) -> dict:
    """Drop empty/None/0-ish values so sparse sources only emit what they have."""
    return {k: v for k, v in d.items() if v not in (None, "", 0, 0.0, [], {}, False)}


def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())


def load_state_forest_campgrounds(con) -> int:
    """MN DNR state-forest campgrounds (points). Day-use-only sites skipped."""
    url = (
        f"{GDRS}/us_mn_state_dnr/struc_state_forest_campgrounds/"
        "shp_struc_state_forest_campgrounds.zip"
    )
    print("[campground] state-forest …", flush=True)
    with tempfile.TemporaryDirectory() as td:
        zp = os.path.join(td, "s.zip")
        _download(url, zp)
        zipfile.ZipFile(zp).extractall(td)
        rdr = shapefile.Reader(os.path.join(td, "state_forest_campgrounds_and_day_use_areas"))
        flds = [f[0] for f in rdr.fields[1:]]
        ni, ti = flds.index("FACILITY_N"), flds.index("SITE_TYPE")
        rows = []
        for sr in rdr.iterShapeRecords():
            geom = sr.shape.__geo_interface__
            if not geom or not geom.get("coordinates"):
                continue
            site_type = str(sr.record[ti] or "")
            if "camp" not in site_type.lower():  # skip day-use / picnic
                continue
            rows.append(
                ("State Forest", str(sr.record[ni] or ""), None, None, shape(geom).wkt, 26915)
            )
        _insert_points(con, CAMPGROUND_LAYER, rows)
    print(f"[campground]   state-forest: {len(rows)}", flush=True)
    return len(rows)


def _dnr_slug_candidates(park: str) -> list[str]:
    """Candidate DNR state-park page slugs for a unit name, best-guess first.
    Covers apostrophes (O'Brien -> obrien), middle initials (Judge C.R. Magney
    -> judge_magney via first+last), and hyphens (Lake Vermilion-Soudan ->
    lake_vermilion)."""
    base = re.sub(r"\s+(State Park|State Recreation Area|State Wayside)$", "", park, flags=re.I)
    base = base.replace("'", "").replace("’", "")

    def sl(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

    cands = [sl(base)]
    words = re.sub(r"[^A-Za-z0-9 ]", " ", base).split()
    if len(words) >= 2:
        cands.append(sl(f"{words[0]} {words[-1]}"))  # first + last (drops initials)
    if "-" in base:
        cands.append(sl(base.split("-")[0]))  # before the hyphen
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _resolve_dnr_park_url(park: str | None) -> str | None:
    """Infer + VERIFY a DNR state-park info page; return it only if it resolves
    (so state-forest units in this dataset, and any bad slug, get no link)."""
    if not park:
        return None
    for slug in _dnr_slug_candidates(park):
        page = f"https://www.dnr.state.mn.us/state_parks/{slug}/index.html"
        try:
            req = urllib.request.Request(page, method="HEAD", headers=UA)
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    return page
        except Exception:
            pass
        time.sleep(0.15)  # courtesy between probes
    return None


def load_state_park_campgrounds(con) -> int:
    """MN DNR state-park camping units (GeoPackage) aggregated to one point per
    campground — 5k+ individual sites collapse to a clean campground layer. Each
    campground links to its DNR park page where the slug verifiably resolves."""
    url = (
        f"{GDRS}/us_mn_state_dnr/struc_parks_and_trails_campsites/"
        "gpkg_struc_parks_and_trails_campsites.zip"
    )
    print("[campground] state-park …", flush=True)
    with tempfile.TemporaryDirectory() as td:
        zp = os.path.join(td, "s.zip")
        _download(url, zp)
        zipfile.ZipFile(zp).extractall(td)
        gpkg = os.path.join(td, "struc_parks_and_trails_campsites.gpkg")
        g = connect(gpkg)  # SpatiaLite reads GPKG blobs via GeomFromGPB
        groups: dict[str, dict] = {}  # one entry per campground, aggregating its sites
        for cg, park, ctype, a30, a50, pad, ada, pets, wf, srid, wkt in g.execute(
            "SELECT CAMPGROUND_NAME, CONTAINING_MGMT_UNIT_NAME, CAMPSITE_TYPE_NAME, "
            "  AMPS_30_FLAG, AMPS_50_FLAG, SITE_PAD_LENGTH_FT, ADA_ACCESSIBLE_FLAG, "
            "  PETS_ALLOWED_FLAG, WATER_FRONT_FLAG, "
            "  ST_Srid(GeomFromGPB(Shape)), AsText(GeomFromGPB(Shape)) "
            "FROM struc_parks_and_trails_campsites WHERE Shape IS NOT NULL"
        ):
            if not cg or not wkt:
                continue
            key = f"{park}|{cg}"
            grp = groups.get(key)
            if grp is None:  # first site's location represents the campground
                name = f"{cg}" + (f" — {park}" if park and park not in cg else "")
                grp = groups[key] = {
                    "park": park, "name": name, "wkt": wkt, "srid": srid,
                    "sites": 0, "electric": False, "max_rv_ft": 0, "ada": False,
                    "pets": False, "water_front": False, "types": set(),
                }
            grp["sites"] += 1
            if _truthy(a30) or _truthy(a50):
                grp["electric"] = True
            for flag, k in ((ada, "ada"), (pets, "pets"), (wf, "water_front")):
                if _truthy(flag):
                    grp[k] = True
            if ctype:
                grp["types"].add(str(ctype))
            try:
                grp["max_rv_ft"] = max(grp["max_rv_ft"], int(float(pad)))
            except (TypeError, ValueError):
                pass
        g.close()
        # Resolve each distinct park's page once (verified), then build rows.
        park_url = {p: _resolve_dnr_park_url(p) for p in {grp["park"] for grp in groups.values()}}
        rows = []
        for grp in groups.values():
            meta = _compact({
                "sites": grp["sites"], "electric": grp["electric"],
                "max_rv_ft": grp["max_rv_ft"], "ada": grp["ada"], "pets": grp["pets"],
                "water_front": grp["water_front"], "site_types": sorted(grp["types"]),
            })
            rows.append(("State Park", grp["name"], park_url.get(grp["park"]), meta,
                         grp["wkt"], grp["srid"]))
        _insert_points(con, CAMPGROUND_LAYER, rows)
    linked = sum(1 for r in rows if r[2])
    print(f"[campground]   state-park: {len(rows)} ({linked} linked)", flush=True)
    return len(rows)


def load_campgrounds(con, states) -> int:
    """The unified `campground` point layer (national/state-park/state-forest),
    across all `states`. Cleared once, then appended from each best-effort
    source: federal RIDB (national, looped over states) + each state's bespoke
    DNR loaders."""
    con.execute("DELETE FROM overlay_points WHERE layer=?", (CAMPGROUND_LAYER,))
    con.commit()
    total = 0

    def run(fn, *a):
        nonlocal total
        try:
            total += fn(*a)
        except Exception as exc:  # best-effort per source
            print(f"[campground]   {fn.__name__} SKIPPED: {exc}", flush=True)

    run(load_ridb_campgrounds, con, states)  # federal, all states
    run(load_curated_campgrounds, con)  # hand-curated gap-fill (all states)
    for st in states:
        for fn in STATES[st]["campground_loaders"]:  # bespoke per state DNR
            run(fn, con)
    print(f"[campground] total: {total}", flush=True)
    return total


def _ridb_get(url: str, key: str) -> dict:
    req = urllib.request.Request(url, headers={**UA, "apikey": key})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _ridb_campsite_meta(fid: str, key: str) -> dict:
    """Aggregate a facility's campsites: usable site count, types, electric,
    max RV length, pets. Pages through all sites (campgrounds are small)."""
    types, max_rv, electric, pets, sites = set(), 0, False, False, 0
    offset = 0
    while offset < 1000:  # safety cap
        d = _ridb_get(
            "https://ridb.recreation.gov/api/v1/facilities/"
            f"{fid}/campsites?limit=50&offset={offset}",
            key,
        )
        recs = d.get("RECDATA", [])
        for c in recs:
            if (c.get("TypeOfUse") or "").lower() != "overnight":
                continue  # skip day-use / management sites
            sites += 1
            t = (c.get("CampsiteType") or "").strip()
            if t:
                types.add(t.title())
            tu = t.upper().replace("-", "").replace(" ", "")
            if "ELECTRIC" in tu and "NONELECTRIC" not in tu:  # avoid "Standard Nonelectric"
                electric = True
            for e in c.get("PERMITTEDEQUIPMENT", []) or []:
                if (e.get("EquipmentName") or "").upper() in ("RV", "TRAILER", "FIFTH WHEEL"):
                    try:
                        max_rv = max(max_rv, int(float(e.get("MaxLength") or 0)))
                    except (TypeError, ValueError):
                        pass
            for a in c.get("ATTRIBUTES", []) or []:
                if a.get("AttributeName") == "Pets Allowed" and _truthy(a.get("AttributeValue")):
                    pets = True
        offset += len(recs)
        if not recs or offset >= d.get("METADATA", {}).get("RESULTS", {}).get("TOTAL_COUNT", 0):
            break
        time.sleep(0.1)
    return _compact({
        "sites": sites, "site_types": sorted(types), "electric": electric,
        "max_rv_ft": max_rv, "pets": pets,
    })


def _in_any_bbox(lat, lon, bboxes) -> bool:
    """True if (lat, lon) falls inside any (w, s, e, n) envelope."""
    return any(w <= lon <= e and s <= lat <= n for (w, s, e, n) in bboxes)


def _ridb_facility_row(f, key):
    """Build the campground insert-row for one RIDB facility record (enriched with
    a campsite aggregate), or None if it has no usable coordinates."""
    lat, lon = f.get("FacilityLatitude"), f.get("FacilityLongitude")
    fid = str(f.get("FacilityID", ""))
    if not lat or not lon or not fid:
        return None
    blurb = re.sub(r"<[^>]+>", " ", f.get("FacilityDescription") or "")
    blurb = re.sub(r"\s+", " ", blurb).strip()
    meta = {
        "reservable": bool(f.get("Reservable")),
        "fee": (f.get("FacilityUseFeeDescription") or "").strip(),
        "ada": (f.get("FacilityAdaAccess") or "").upper() == "Y",
        "phone": (f.get("FacilityPhone") or "").strip(),
        "activities": [
            (a.get("ActivityName") or "").title() for a in (f.get("ACTIVITY") or [])
        ][:6],
        "blurb": (blurb[:220] + "…") if len(blurb) > 220 else blurb,
    }
    try:
        meta.update(_ridb_campsite_meta(fid, key))
    except Exception as exc:  # campsites best-effort; keep facility fields
        print(f"[campground]     {fid} campsites skipped: {exc}", flush=True)
    resv = f"https://www.recreation.gov/camping/campgrounds/{fid}"
    m = _compact(meta)
    m["reservable"] = bool(f.get("Reservable"))  # keep even when False (first-come)
    return ("National / Federal", f.get("FacilityName") or "", resv, m,
            f"POINT({lon} {lat})", 4326)


def load_ridb_campgrounds(con, states) -> int:
    """Recreation.gov RIDB federal campgrounds (national forest/NPS/BLM/Corps).
    Keyed: needs RIDB_API_KEY in the env. Skipped (returns 0) without it, so the
    state layers still load — same best-effort posture as MVUM/structures. Each
    facility is enriched with reservable/fee/ADA/phone/activities + a campsite
    aggregate (count/types/electric/max-RV).

    Facilities are gathered by GEOGRAPHY, not RIDB's `state=` filter: that filter
    matches a facility's parent RecArea/org linkage, NOT its address, so it
    silently drops real campgrounds (e.g. Sitting Bull CG in Bighorn NF — a
    reservable WY campground whose `state=WY` linkage is absent). We page the
    national CAMPING list and keep every facility whose COORDINATES fall in a
    configured state's bbox, then UNION the per-state `state=` results (deduped by
    FacilityID) so a rare mis-geocoded-but-state-linked facility isn't lost."""
    key = os.environ.get("RIDB_API_KEY")
    if not key:
        print("[campground]   RIDB skipped (no RIDB_API_KEY)", flush=True)
        return 0
    bboxes = [STATES[s]["bbox"] for s in states]
    facilities: dict[str, dict] = {}  # FacilityID -> record (first wins)

    def collect(params: str, label: str, geofilter: bool) -> None:
        offset, added = 0, 0
        while True:
            data = _ridb_get(
                "https://ridb.recreation.gov/api/v1/facilities?"
                f"{params}&limit=50&offset={offset}&full=true",
                key,
            )
            recs = data.get("RECDATA", [])
            for f in recs:
                fid = str(f.get("FacilityID", ""))
                if not fid or fid in facilities:
                    continue
                if geofilter:
                    lat, lon = f.get("FacilityLatitude"), f.get("FacilityLongitude")
                    if not lat or not lon or not _in_any_bbox(lat, lon, bboxes):
                        continue
                facilities[fid] = f
                added += 1
            total_count = data.get("METADATA", {}).get("RESULTS", {}).get("TOTAL_COUNT", 0)
            offset += len(recs)
            if not recs or offset >= total_count:
                break
        print(f"[campground]   RIDB {label}: +{added} facilities", flush=True)

    # National geographic sweep (catches the state=-filter misses), then union the
    # per-state state= results for any facility our bboxes don't cover geographically.
    collect("activity=CAMPING", "national geo-sweep", geofilter=True)
    for state in states:
        collect(f"activity=CAMPING&state={state}", f"state={state}", geofilter=False)

    # Enrich (campsite aggregate) + insert each unique facility exactly once.
    rows = []
    for f in facilities.values():
        row = _ridb_facility_row(f, key)
        if row is not None:
            rows.append(row)
        time.sleep(0.1)
    _insert_points(con, CAMPGROUND_LAYER, rows)
    print(f"[campground]   RIDB federal total: {len(rows)} unique facilities", flush=True)
    return len(rows)


CURATED_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "campgrounds_extra.csv")


def load_curated_campgrounds(con) -> int:
    """Hand-curated campgrounds that OSM/authoritative feeds miss — e.g. WI
    state-forest sites not yet mapped in OSM. A small tracked CSV in the repo,
    merged into the campground layer with meta.source='curated'. You control it,
    it adds zero runtime deps, and it's transparent in git. The proper long-term
    fix for any one row is mapping it in OSM (then it flows in on its own); this
    just guarantees coverage in the meantime."""
    if not os.path.exists(CURATED_CSV):
        return 0
    rows = []
    with open(CURATED_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                lat, lon = float(r["lat"]), float(r["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            meta = _compact({
                "sites": int(r["sites"]) if (r.get("sites") or "").strip() else 0,
                "electric": _truthy(r.get("electric")),
                "ada": _truthy(r.get("ada")),
                "pets": _truthy(r.get("pets")),
            })
            if (r.get("reservable") or "").strip():  # keep explicit (False = first-come)
                meta["reservable"] = _truthy(r.get("reservable"))
            if (r.get("state") or "").strip():
                meta["state"] = r["state"].strip()  # UI picks the reservation fallback
            meta["source"] = "curated"
            rows.append((
                (r.get("category") or "").strip() or None,
                (r.get("name") or "").strip() or "Campground",
                (r.get("url") or "").strip() or None,
                meta, f"POINT({lon} {lat})", 4326,
            ))
    _insert_points(con, CAMPGROUND_LAYER, rows)
    print(f"[campground]   curated: {len(rows)}", flush=True)
    return len(rows)


def load_shp(con, layer, zip_url, base, cat_field, name_field, srid) -> int:
    print(f"[{layer}] downloading {zip_url.rsplit('/', 1)[-1]} …", flush=True)
    with tempfile.TemporaryDirectory() as td:
        zp = os.path.join(td, "src.zip")
        req = urllib.request.Request(zip_url, headers=UA)
        with urllib.request.urlopen(req, timeout=300) as r, open(zp, "wb") as f:
            f.write(r.read())
        zipfile.ZipFile(zp).extractall(td)
        rdr = shapefile.Reader(os.path.join(td, base))
        flds = [f[0] for f in rdr.fields[1:]]
        ci = flds.index(cat_field) if cat_field and cat_field in flds else None
        ni = flds.index(name_field) if name_field and name_field in flds else None
        # NB: the caller clears the layer once before appending all its sources
        # (so MN + WI can union under a shared layer id) — don't clear here.
        batch, n = [], 0
        for sr in rdr.iterShapeRecords():
            geom = sr.shape.__geo_interface__
            if not geom or not geom.get("coordinates"):
                continue
            rec = sr.record
            cat = str(rec[ci]) if ci is not None and rec[ci] is not None else None
            name = str(rec[ni]) if ni is not None and rec[ni] is not None else None
            batch.append((cat, name, shape(geom).wkt, srid))
            if len(batch) >= 2000:
                _insert(con, layer, batch)
                n += len(batch)
                batch = []
                print(f"[{layer}]   {n} …", flush=True)
        if batch:
            _insert(con, layer, batch)
            n += len(batch)
    print(f"[{layer}] loaded {n} features", flush=True)
    return n


def load_usfs(con, bbox) -> int:
    import json

    layer = "national_forest"
    print(f"[{layer}] querying USFS ArcGIS over {bbox} …", flush=True)
    try:
        req = urllib.request.Request(_usfs_query(bbox), headers=UA)
        with urllib.request.urlopen(req, timeout=120) as r:
            fc = json.load(r)
    except Exception as exc:  # best-effort: skip if USFS is unreachable
        print(f"[{layer}] SKIPPED (USFS unreachable: {exc})", flush=True)
        return 0
    clear_layer(con, layer)
    rows = []
    for feat in fc.get("features", []):
        geom = feat.get("geometry")
        if not geom or not geom.get("coordinates"):
            continue
        props = feat.get("properties") or {}
        # ArcGIS f=geojson lowercases property keys -> read case-insensitively.
        name = next((v for k, v in props.items() if k.lower() == "forestname"), None)
        rows.append((None, name, shape(geom).wkt, 4326))
    _insert(con, layer, rows)
    print(f"[{layer}] loaded {len(rows)} features", flush=True)
    return len(rows)


# PAD-US (USGS Protected Areas DB) — Esri-hosted v3.0 mirror (the USGS service is
# flaky). Full-resolution + queryable, unlike BLM's national SMA (cached/
# generalized only). One source feeds two layers per state.
PADUS_URL = (
    "https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/"
    "Manager_Name/FeatureServer/0/query"
)
# Same FeatureServer without the /query suffix — the generic ArcGIS loader
# appends /query itself (used for the `wilderness` layer, Des_Tp='WA').
PADUS_FS = PADUS_URL.rsplit("/query", 1)[0]
# PAD-US Own_Type code -> our ownership category. PUBLIC fee only: PVT (private),
# UNK (unknown), DESG (a designation overlapping fee, not ownership) and TRIB
# (tribal — NOT open-access, must not read as public) are deliberately excluded
# so the public-land filter stays honest.
PADUS_PUBLIC_OWN = {"FED": "Federal", "STAT": "State", "LOC": "County", "NGO": "NGO"}
PADUS_PARK_DES = {"SP": "State Park", "SREC": "State Recreation Area"}


def load_padus(con, code, only=None) -> int:
    """PAD-US for one state (`code` = State_Nm, e.g. 'SD'). Feeds three layers
    from one query:
      - `ownership`: public fee land, categorized (private/tribal/unknown dropped),
      - `state_park`: State Park / State Rec Area designations (incl. Custer SP),
      - `national_park`: NPS-managed fee land (Wind Cave/Badlands NP, Jewel Cave
        NM, Mt Rushmore, …) — a display-only CONTEXT layer (camping there is
        designated-only; the federal parcels are already in `ownership`).
    Named by Unit_Nm. Paged by OBJECTID; best-effort. The caller clears each
    layer once first."""
    want_own = not only or "ownership" in only
    want_park = not only or "state_park" in only
    want_nps = not only or "national_park" in only
    if not (want_own or want_park or want_nps):
        return 0
    own_in = "Own_Type IN ('FED','STAT','LOC','NGO')"
    park_in = "Des_Tp IN ('SP','SREC')"
    base_where = f"State_Nm='{code}' AND ({own_in} OR {park_in})"
    print(f"[padus] querying PAD-US for {code} …", flush=True)
    own_rows: list = []
    park_rows: list = []
    nps_rows: list = []
    last = 0
    while True:
        params = urllib.parse.urlencode({
            "where": f"({base_where}) AND OBJECTID>{last}",
            "outFields": "Own_Type,Mang_Name,Des_Tp,Unit_Nm,OBJECTID",
            "orderByFields": "OBJECTID",
            "resultRecordCount": 1000,
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
        })
        try:
            req = urllib.request.Request(f"{PADUS_URL}?{params}", headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                fc = json.load(r)
        except Exception as exc:  # best-effort: skip if the service is unreachable
            print(f"[padus] SKIPPED ({code}: {exc})", flush=True)
            return 0
        feats = fc.get("features", [])
        if not feats:
            break
        for feat in feats:
            geom = feat.get("geometry")
            props = {k.lower(): v for k, v in (feat.get("properties") or {}).items()}
            oid = props.get("objectid")
            if oid is not None:
                last = max(last, int(oid))
            if not geom or not geom.get("coordinates"):
                continue
            wkt = shape(geom).wkt
            name = props.get("unit_nm")
            own, des, mang = props.get("own_type"), props.get("des_tp"), props.get("mang_name")
            if want_own and own in PADUS_PUBLIC_OWN:
                own_rows.append((PADUS_PUBLIC_OWN[own], name, wkt, 4326))
            if want_park and des in PADUS_PARK_DES:
                park_rows.append((None, name, wkt, 4326))
            if want_nps and own == "FED" and mang == "NPS":
                nps_rows.append((None, name, wkt, 4326))
        if len(feats) < 1000:
            break
    if own_rows:
        _insert(con, "ownership", own_rows)
    if park_rows:
        _insert(con, "state_park", park_rows)
    if nps_rows:
        _insert(con, "national_park", nps_rows)
    print(
        f"[padus] {code}: {len(own_rows)} ownership + {len(park_rows)} park "
        f"+ {len(nps_rows)} nps", flush=True
    )
    return len(own_rows) + len(park_rows) + len(nps_rows)


def load_arcgis_features(con, layer, base_url, where, name_field) -> int:
    """Append polygon features from an ArcGIS REST layer (f=geojson) into
    `layer`. Pages by OBJECTID (works on MapServer + FeatureServer alike, and is
    needed where a single query exceeds the server's transfer limit). Name is
    title-cased; category is left null (boundary layers render by layer, not
    category). Best-effort: an unreachable service appends nothing. The caller
    clears the layer once before any source, so a mid-run failure can't drop
    features another state already loaded into the same layer."""
    nf = name_field.lower()
    print(f"[{layer}] querying ArcGIS {base_url.rsplit('/services/', 1)[-1]} …", flush=True)
    rows: list = []
    last = 0
    while True:
        params = urllib.parse.urlencode({
            "where": f"({where}) AND OBJECTID>{last}",
            "outFields": f"{name_field},OBJECTID",
            "orderByFields": "OBJECTID",
            "resultRecordCount": 1000,
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
        })
        try:
            req = urllib.request.Request(f"{base_url}/query?{params}", headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                fc = json.load(r)
        except Exception as exc:  # best-effort: skip if the service is unreachable
            print(f"[{layer}] SKIPPED (ArcGIS error: {exc})", flush=True)
            return 0
        feats = fc.get("features", [])
        if not feats:
            break
        for feat in feats:
            geom = feat.get("geometry")
            props = {k.lower(): v for k, v in (feat.get("properties") or {}).items()}
            oid = props.get("objectid")
            if oid is not None:
                last = max(last, int(oid))
            if not geom or not geom.get("coordinates"):
                continue
            name = props.get(nf)
            rows.append((None, str(name).title() if name else None, shape(geom).wkt, 4326))
        if len(feats) < 1000 or oid is None:  # last page (or no OBJECTID to page on)
            break
    _insert(con, layer, rows)
    print(f"[{layer}] loaded {len(rows)} features", flush=True)
    return len(rows)


OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Overpass routinely 429/504s under load — a fresh full rebuild fires many
# queries back-to-back (one per state for dispersed/campgrounds, one per region
# for amenities). Without backoff a single transient rate-limit silently drops a
# whole state's layer (this bit us: MN/SD/WY dispersed vanished on a rebuild).
# Retry with linear backoff at the one choke point so every caller is resilient.
OVERPASS_RETRIES = 4
OVERPASS_BACKOFF_S = 20  # 20s, 40s, 60s between attempts


def _overpass(query: str, *, retries: int = OVERPASS_RETRIES) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(OVERPASS_URL, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except Exception as exc:  # 429/504/timeout — back off and retry
            last_exc = exc
            if attempt < retries - 1:
                wait = OVERPASS_BACKOFF_S * (attempt + 1)
                print(
                    f"[overpass] attempt {attempt + 1}/{retries} failed: {exc} "
                    f"— retrying in {wait}s",
                    flush=True,
                )
                time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def _boundary_containing(con, lon: float, lat: float):
    """The state_park/state_forest boundary polygon containing a point, or None
    -> (layer, name). Categorizes an OSM campground by the land it sits on (and
    drops private campgrounds outside state land). Needs the boundary layers
    loaded first — main() loads them before campgrounds."""
    return con.execute(
        "SELECT layer, name FROM overlay_features "
        "WHERE layer IN ('state_park','state_forest') AND ROWID IN ("
        "  SELECT ROWID FROM SpatialIndex WHERE f_table_name='overlay_features' "
        "  AND f_geometry_column='geom' AND search_frame=MakePoint(?, ?, 4326)) "
        "AND ST_Contains(geom, MakePoint(?, ?, 4326)) LIMIT 1",
        (lon, lat, lon, lat),
    ).fetchone()


def osm_state_park_campgrounds(iso: str):
    """Campground loader for a state with no authoritative campground GIS (e.g.
    WI). Pulls tourism=camp_site from OSM (Overpass) within the state, keeps the
    campground-LEVEL features — a polygon footprint (way), or a node named
    '…campground' — so individual pitches/shelters are dropped, and keeps only
    those inside a loaded state-park/forest boundary, which also gives the
    category. Amenities are best-effort from sparse OSM tags; the UI uses
    meta.state to pick the right reservation fallback link."""
    code = iso.split("-")[-1]
    cats = {"state_park": "State Park", "state_forest": "State Forest"}
    query = (
        "[out:json][timeout:120];"
        f'area["ISO3166-2"="{iso}"]->.a;'
        '(node["tourism"="camp_site"](area.a);way["tourism"="camp_site"](area.a););'
        "out center tags;"
    )

    def _load(con) -> int:
        print(f"[campground] OSM {iso} (state park/forest) …", flush=True)
        try:
            els = _overpass(query).get("elements", [])
        except Exception as exc:
            print(f"[campground]   OSM {iso} SKIPPED: {exc}", flush=True)
            return 0
        rows = []
        for el in els:
            tags = el.get("tags") or {}
            name = tags.get("name")
            # Campground level only: a polygon footprint, or a node named
            # "…campground" — drops individual pitches/shelters/numbered sites.
            if el.get("type") != "way" and not (name and re.search(r"camp\s?ground", name, re.I)):
                continue
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lon is None:
                continue
            b = _boundary_containing(con, lon, lat)
            if not b:
                continue  # only campgrounds on state park/forest land
            power = str(tags.get("power_supply", "")).lower()
            try:
                sites = int(float(tags.get("capacity")))
            except (TypeError, ValueError):
                sites = 0
            meta = _compact({
                "sites": sites,
                "electric": power not in ("", "no", "none") or tags.get("electricity") == "yes",
                "ada": tags.get("wheelchair") == "yes",
                "pets": tags.get("dog") == "yes",
            })
            meta["state"] = code  # UI picks the reservation fallback by state
            meta["source"] = "osm"
            url = tags.get("website") or tags.get("contact:website") or None
            cg_name = name or (f"{b[1]} Campground" if b[1] else "Campground")
            rows.append((cats[b[0]], cg_name, url, meta, f"POINT({lon} {lat})", 4326))
        _insert_points(con, CAMPGROUND_LAYER, rows)
        print(f"[campground]   OSM {iso}: {len(rows)}", flush=True)
        return len(rows)

    _load.__name__ = f"osm_campgrounds_{code}"
    return _load


DISPERSED_LAYER = "dispersed"

# OSM camp_site values that mean a developed or sub-pitch site, not a dispersed
# one. (camp_site=basic / informal / backcountry are the positive keepers.)
_DEVELOPED_CAMP_SITE = {"standard", "serviced", "deluxe", "pitch"}
# Names that betray a developed/commercial site even when OSM omits operator/fee
# (sparse rural tagging is why the strict filter can't rely on those alone).
_DEVELOPED_NAME_RE = re.compile(
    r"camp\s?ground|\bcg\b|koa|\bresort\b|\brv\b|\bcottages?\b|\bcabins?\b|"
    r"\branch\b|\blodge\b|\bretreat\b|\bglamping\b|\bmotel\b|\bhotel\b",
    re.I,
)
# Tags that mark a run-as-a-business site (a real dispersed spot is near-bare).
_COMMERCIAL_TAGS = (
    "website", "contact:website", "phone", "contact:phone",
    "addr:housenumber", "brand", "operator",
)


def _is_dispersed(tags: dict) -> bool:
    """STRICT 'primitive dispersed' filter over OSM tourism=camp_site tags.
    camp_site=basic / backcountry / informal are kept outright. Otherwise a site
    is kept ONLY if it's a near-bare, non-commercial camp_site node — anything
    with a fee/operator/reservation, a business tag (website/phone/address), an
    individual pitch, private access, or a developed/commercial name is dropped
    (those belong to the `campground` layer, not here)."""
    if str(tags.get("access", "")).lower() in ("private", "no", "permit"):
        return False
    cs = str(tags.get("camp_site", "")).lower()
    if cs in _DEVELOPED_CAMP_SITE:
        return False
    # Positive primitive signals — keep regardless of (rare) other tags.
    if cs == "basic" or _truthy(tags.get("backcountry")) or _truthy(tags.get("informal")):
        return True
    # Otherwise keep only bare, non-commercial nodes.
    if tags.get("operator") or _truthy(tags.get("fee")):
        return False
    if str(tags.get("reservation", "")).lower() in ("yes", "required"):
        return False
    if any(tags.get(t) for t in _COMMERCIAL_TAGS):
        return False
    if _DEVELOPED_NAME_RE.search(tags.get("name") or ""):
        return False
    return True


# A real dispersed spot sits a stone's throw from where you'd park — and OSM
# campgrounds are often re-tagged camp_site nodes, so anything this close to a
# known campground is almost certainly that campground, not a separate site.
DISPERSED_CAMPGROUND_DEDUP_M = 250.0


def _in_forest(con, lon: float, lat: float):
    """The national_forest / state_forest boundary containing a point -> its name,
    or None. Dispersed camping is fundamentally an NF/state-forest thing, so this
    is the gate that strips private land + town noise OSM tags can't catch. Needs
    those boundary layers loaded first (main() loads them before campgrounds)."""
    return con.execute(
        "SELECT name FROM overlay_features "
        "WHERE layer IN ('national_forest','state_forest') AND ROWID IN ("
        "  SELECT ROWID FROM SpatialIndex WHERE f_table_name='overlay_features' "
        "  AND f_geometry_column='geom' AND search_frame=MakePoint(?, ?, 4326)) "
        "AND ST_Contains(geom, MakePoint(?, ?, 4326)) LIMIT 1",
        (lon, lat, lon, lat),
    ).fetchone()


def _near_campground(con, lon: float, lat: float) -> bool:
    """True if a known campground point is within DISPERSED_CAMPGROUND_DEDUP_M —
    used to drop OSM camp_site nodes that are really developed campgrounds we
    already carry in the `campground` layer."""
    rdeg = DISPERSED_CAMPGROUND_DEDUP_M / 111_000.0
    row = con.execute(
        "SELECT MIN(ST_Distance(MakePoint(?, ?, 4326), geom, 1)) "
        "FROM overlay_points WHERE layer='campground' AND ROWID IN ("
        "  SELECT ROWID FROM SpatialIndex WHERE f_table_name='overlay_points' "
        "  AND f_geometry_column='geom' AND search_frame=BuildCircleMbr(?, ?, ?, 4326))",
        (lon, lat, lon, lat, rdeg),
    ).fetchone()
    return row is not None and row[0] is not None and row[0] <= DISPERSED_CAMPGROUND_DEDUP_M


def load_dispersed(con, states) -> int:
    """The `dispersed` point layer: established primitive/dispersed campsites from
    OSM (tourism=camp_site), per the strict filter in `_is_dispersed`. National —
    one Overpass query per state area, so any configured state is covered.

    Two extra gates cut the OSM noise (private/town sites + dup campgrounds):
    kept only if (1) inside a national_forest OR state_forest boundary, and (2)
    not within DISPERSED_CAMPGROUND_DEDUP_M of a known campground. backcountry=yes
    is KEPT but flagged meta.access='hike-in' (not towable by the teardrop).
    Display-only (TLC-3), NEVER scored — an existing fire ring is a lower-impact,
    safer-legal bet, not an assertion that camping is legal."""
    con.execute("DELETE FROM overlay_points WHERE layer=?", (DISPERSED_LAYER,))
    con.commit()
    total = 0
    for st in states:
        iso = f"US-{st}"
        query = (
            "[out:json][timeout:180];"
            f'area["ISO3166-2"="{iso}"]->.a;'
            '(node["tourism"="camp_site"](area.a);way["tourism"="camp_site"](area.a););'
            "out center tags;"
        )
        print(f"[dispersed] OSM {iso} …", flush=True)
        try:
            els = _overpass(query).get("elements", [])
        except Exception as exc:
            print(f"[dispersed]   OSM {iso} SKIPPED: {exc}", flush=True)
            continue
        rows, dropped_land, dropped_cg = [], 0, 0
        for el in els:
            tags = el.get("tags") or {}
            if not _is_dispersed(tags):
                continue
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lon is None:
                continue
            forest = _in_forest(con, lon, lat)
            if not forest:
                dropped_land += 1
                continue  # only dispersed sites on national/state forest land
            if _near_campground(con, lon, lat):
                dropped_cg += 1
                continue  # a known campground re-tagged as camp_site — not dispersed
            meta = _compact({
                "access": "hike-in" if _truthy(tags.get("backcountry")) else "drive-in",
                "camp_site": str(tags.get("camp_site", "")).lower() or None,
                "forest": forest[0] or None,
                "source": "osm",
            })
            name = tags.get("name") or "Dispersed site"
            rows.append(("Dispersed site", name, None, meta, f"POINT({lon} {lat})", 4326))
        _insert_points(con, DISPERSED_LAYER, rows)
        print(
            f"[dispersed]   OSM {iso}: {len(rows)} kept "
            f"(dropped {dropped_land} off-forest, {dropped_cg} near-campground)",
            flush=True,
        )
        total += len(rows)
    print(f"[dispersed] total: {total}", flush=True)
    return total


# --- resupply + nearest-town (the `amenity` point layer) -------------------
# Display-only (TLC-3) teardrop logistics shown on a scored site's bottom sheet:
# the nearest gas / groceries / water fill / dump station / propane, plus the
# nearest town. NEVER scored. Unlike campgrounds/dispersed (gated INSIDE a
# forest), resupply lives in the towns AROUND a forest, so we preload a buffer
# of ~RESUPPLY_BUFFER_KM around every loaded NF/SF boundary — keeping the
# convenience-store firehose down to actual camping country and bounding each
# Overpass query — rather than the whole state.
AMENITY_LAYER = "amenity"
RESUPPLY_BUFFER_KM = 40.0
RESUPPLY_BUFFER_DEG = RESUPPLY_BUFFER_KM / 111.0  # ~deg of latitude

_GROCERY_SHOPS = {"supermarket", "convenience", "general", "grocery", "greengrocer"}
_TOWN_PLACES = {"city", "town", "village"}

# Default display names per category for an unnamed OSM element.
_AMENITY_DEFAULT_NAME = {
    "fuel": "Gas station",
    "groceries": "Store",
    "water": "Drinking water",
    "dump_station": "Dump station",
    "propane": "Propane",
}


def _categorize_amenity(tags: dict) -> set[str]:
    """Every resupply category (+ `town`) an OSM element belongs to, by tags. An
    element can match several (a fuel station that also sells propane → both
    `fuel` and `propane`), so it's stored once per category and the spot sheet's
    nearest-of-each query finds it under either. Best-effort over sparse OSM
    tagging — display-only (TLC-3)."""
    cats: set[str] = set()
    amenity = tags.get("amenity")
    shop = tags.get("shop")
    if amenity == "fuel":
        cats.add("fuel")
    if shop in _GROCERY_SHOPS:
        cats.add("groceries")
    if amenity == "drinking_water" or (
        tags.get("man_made") == "water_tap" and _truthy(tags.get("drinking_water"))
    ):
        cats.add("water")
    if (
        amenity == "sanitary_dump_station"
        or tags.get("waste_disposal") == "dump_station"
        or _truthy(tags.get("sanitary_dump_station"))
    ):
        cats.add("dump_station")
    # Propane/LPG refill: a fuel station flagged for it, or a standalone gas dealer.
    if _truthy(tags.get("fuel:propane")) or _truthy(tags.get("fuel:lpg")) or shop == "gas":
        cats.add("propane")
    if tags.get("place") in _TOWN_PLACES:
        cats.add("town")
    return cats


def _amenity_name(category: str, tags: dict) -> str | None:
    """Display name for an (element, category) row. A town must be named (an
    unnamed place node is useless as 'nearest town' → dropped); every other
    category falls back to a generic per-category label."""
    name = (tags.get("name") or "").strip()
    if category == "town":
        return name or None
    return name or _AMENITY_DEFAULT_NAME.get(category, category)


def _amenity_meta(category: str, tags: dict) -> dict:
    """Compact, display-only meta for a resupply/town row — only what the sheet
    might show. Sparse by design (`_compact` drops empties)."""
    meta = {
        "brand": tags.get("brand") or tags.get("operator"),
        "hours": tags.get("opening_hours"),
    }
    if category == "groceries":
        meta["shop"] = tags.get("shop")
    elif category in ("dump_station", "water"):
        meta["fee"] = _truthy(tags.get("fee")) or None
    elif category == "town":
        meta = {"place": tags.get("place"), "population": tags.get("population")}
    return _compact(meta)


def _merge_bboxes(bboxes: list[tuple]) -> list[tuple]:
    """Greedily union overlapping (w, s, e, n) rectangles until stable. The
    buffered forest bboxes overlap heavily (dozens of small state forests in one
    region), so merging cuts the Overpass query count to a handful; cross-region
    duplicates are de-duped by OSM id anyway. Pure + deterministic."""
    boxes = [tuple(b) for b in bboxes]
    merged = True
    while merged:
        merged = False
        out: list[tuple] = []
        for b in boxes:
            placed = False
            for i, o in enumerate(out):
                # Overlap test (touching edges count as overlapping).
                if not (b[2] < o[0] or b[0] > o[2] or b[3] < o[1] or b[1] > o[3]):
                    out[i] = (min(b[0], o[0]), min(b[1], o[1]), max(b[2], o[2]), max(b[3], o[3]))
                    placed = True
                    merged = True
                    break
            if not placed:
                out.append(b)
        boxes = out
    return boxes


def load_amenities(con, states) -> int:
    """The `amenity` point layer: resupply (fuel/groceries/water/dump/propane) +
    nearest towns from OSM, within ~RESUPPLY_BUFFER_KM of every loaded NF/SF
    boundary. Display-only (TLC-3), NEVER scored. Reads the forest boundaries
    that main() loads first; if none are present it skips (nothing to buffer)."""
    con.execute("DELETE FROM overlay_points WHERE layer=?", (AMENITY_LAYER,))
    con.commit()
    rows = con.execute(
        "SELECT MbrMinX(geom), MbrMinY(geom), MbrMaxX(geom), MbrMaxY(geom) "
        "FROM overlay_features WHERE layer IN ('national_forest', 'state_forest')"
    ).fetchall()
    rows = [r for r in rows if r[0] is not None]
    if not rows:
        print("[amenity] no NF/SF boundaries loaded — skipping (load forests first)", flush=True)
        return 0
    buffered = [
        (w - RESUPPLY_BUFFER_DEG, s - RESUPPLY_BUFFER_DEG,
         e + RESUPPLY_BUFFER_DEG, n + RESUPPLY_BUFFER_DEG)
        for (w, s, e, n) in rows
    ]
    regions = _merge_bboxes(buffered)
    print(
        f"[amenity] {len(rows)} forest parts -> {len(regions)} query regions "
        f"(buffer {RESUPPLY_BUFFER_KM:.0f} km)",
        flush=True,
    )
    seen: set[tuple] = set()  # (osm_type, osm_id, category) across overlapping regions
    total = 0
    for idx, (w, s, e, n) in enumerate(regions, 1):
        # Overpass bbox order is (south, west, north, east).
        bbox = f"{s},{w},{n},{e}"
        query = (
            "[out:json][timeout:180];("
            f'nwr["amenity"="fuel"]({bbox});'
            f'nwr["shop"~"^(supermarket|convenience|general|grocery|greengrocer)$"]({bbox});'
            f'nwr["amenity"="drinking_water"]({bbox});'
            f'nwr["man_made"="water_tap"]["drinking_water"="yes"]({bbox});'
            f'nwr["amenity"="sanitary_dump_station"]({bbox});'
            f'nwr["waste_disposal"="dump_station"]({bbox});'
            f'nwr["shop"="gas"]({bbox});'
            f'node["place"~"^(city|town|village)$"]({bbox});'
            ");out center tags;"
        )
        print(f"[amenity] region {idx}/{len(regions)} …", flush=True)
        # Overpass routinely 504/429s a big region under load (we hit this on the
        # Black Hills region). Retry with backoff so a fresh rebuild is reliable
        # rather than silently dropping a whole state's resupply.
        els = None
        for attempt in range(4):
            try:
                els = _overpass(query).get("elements", [])
                break
            except Exception as exc:
                wait = 15 * (attempt + 1)
                print(
                    f"[amenity]   region {idx} attempt {attempt + 1}/4 failed: {exc}"
                    + (f" — retrying in {wait}s" if attempt < 3 else " — giving up"),
                    flush=True,
                )
                if attempt < 3:
                    time.sleep(wait)
        if els is None:
            continue
        rows_ins = []
        for el in els:
            tags = el.get("tags") or {}
            cats = _categorize_amenity(tags)
            if not cats:
                continue
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            if lat is None or lon is None:
                continue
            otype, oid = el.get("type"), el.get("id")
            for cat in cats:
                key = (otype, oid, cat)
                if key in seen:
                    continue
                name = _amenity_name(cat, tags)
                if name is None:
                    continue  # unnamed town
                seen.add(key)
                meta = _amenity_meta(cat, tags)
                rows_ins.append((cat, name, None, meta or None, f"POINT({lon} {lat})", 4326))
        _insert_points(con, AMENITY_LAYER, rows_ins)
        total += len(rows_ins)
        print(f"[amenity]   region {idx}: {len(rows_ins)} kept", flush=True)
    print(f"[amenity] total: {total}", flush=True)
    return total


# --- cell coverage (FCC BDC mobile broadband, display-only — TLC-3) ----------
# A polygon overlay of cellular coverage so a camper can tell "truly no signal"
# (stay-away if you need to be reachable) from "some signal, maybe not great"
# from "solid/fast." NEVER scored (TLC-3) — purely the Land panel. Source: the
# FCC National Broadband Map / Broadband Data Collection (BDC) mobile coverage,
# published as H3 res-9 hexagons per state × technology (carrier-reported, known
# to be optimistic — the UI says so). The download-listing API is anti-bot
# (401), and the data refreshes only ~twice a year, so files are staged manually
# under CELL_DIR (one-time chore) and this loader ingests them locally.
#
# REPRESENTATION: a single graded-quality layer. For one carrier we read three
# files — 4G LTE (5/1), 5G-NR 7/1, 5G-NR 35/3 — and grade each res-7 hex by its
# best available service: FAST (>= 50% of children have 35/3 5G), SOLID (>= 50%
# have any 4G/5G), PATCHY (some but < 50%), or NONE (a real gap, unpainted).
#
# PERF (the #500 lesson): we DON'T dissolve into state-spanning multipolygons (a
# single huge bbox defeats the R*Tree). Each covered res-9 hex is aggregated up
# to its res-7 parent and emitted as one small, uniform hexagon row — display-only
# coarsening that keeps every feature's bbox tiny so the R*Tree always prunes.
CELL_COVERAGE_LAYER = "cell_coverage"
CELL_SRC_RES = 9  # FCC BDC publishes coverage as res-9 hexes
CELL_HEX_RES = 7  # aggregated up to res-7 parents (~5 km²; tune coarser if busy)
# A res-7 cell is graded by how many of its 49 res-9 children report coverage.
# "Solid" demands a clear majority (>= 2/3), not a bare half: FCC overstates
# coverage in terrain — a Whitewater valley hex reads ~50% covered on paper yet
# is near-dead on the ground (the "covered" half is the bluff tops, not the
# campable valley floor) — so a bare-50% "Solid" was too generous. The 50–66%
# band now honestly reads "Patchy" (some signal, depends where you stand). "Fast"
# keeps a 50% bar, but on the already-strict 35/3 5G subset. Zero covered children
# stays a real gap ("none").
CELL_SOLID_FRAC = 2 / 3  # any-coverage fraction for "Solid"
CELL_FAST_FRAC = 0.5  # fast-5G (35/3) fraction for "Fast"
_CELL_CHILDREN = 7 ** (CELL_SRC_RES - CELL_HEX_RES)  # 49 res-9 cells per res-7 parent
_CELL_SOLID_MIN = max(1, math.ceil(CELL_SOLID_FRAC * _CELL_CHILDREN))  # 33
_CELL_FAST_MIN = max(1, math.ceil(CELL_FAST_FRAC * _CELL_CHILDREN))  # 25
# The graded coverage-quality tiers (best first), rendered as a sequential ramp.
CELL_TIERS = ("Fast", "Solid", "Patchy")
CELL_DIR = os.environ.get("FCC_BDC_DIR") or os.path.join(os.path.dirname(DB), "fcc-bdc")
# MN + WI + MI + SD union envelope (w, s, e, n). FCC files are per-FIPS and
# independent of the STATES registry (which lacks MI), so we clip to this box —
# bounding a nationwide "by provider" download to the four states we cover.
CELL_STATES_BBOX = (-104.1, 41.6, -82.3, 49.5)


def _cell_carrier(brand: str | None) -> str | None:
    """Normalize an FCC provider brand to one of the four nationwide carriers we
    surface, or None to drop it (regional carriers/MVNOs — 'does MY carrier
    work' only makes sense for the big four). Pure + tolerant of brand variants
    ('AT&T Mobility', 'Verizon Wireless', 'New Cingular Wireless', …)."""
    if not brand:
        return None
    low = str(brand).strip().lower()
    flat = re.sub(r"[^a-z0-9]", "", low)
    if "verizon" in flat:
        return "Verizon"
    if "tmobile" in flat:
        return "T-Mobile"
    if "uscellular" in flat or "unitedstatescellular" in flat:
        return "UScellular"
    if "at&t" in low or "cingular" in low or flat.startswith("att"):
        return "AT&T"
    return None


def _cell_source(filename: str) -> str | None:
    """Which coverage bucket a BDC file feeds, from its name:
      - ``"fast"``  — 5G-NR 35/3 (work-grade); feeds both the any-coverage and
        the fast tallies,
      - ``"any"``   — 4G LTE, or 5G-NR 7/1 / tier-less 5G (basic data); feeds the
        any-coverage tally only,
      - ``None``    — skip (e.g. a 3G file).
    The quality grade per hex is derived from these tallies (see
    `_coverage_tier`)."""
    low = filename.lower()
    if "4glte" in low or "4g_lte" in low:
        return "any"
    if "5gnr" in low or "5g_nr" in low or "5g-nr" in low:
        m = re.search(r"5g_?nr_?(\d+_\d+)", low)
        return "fast" if (m and m.group(1) == "35_3") else "any"
    return None


def _find_field(fields: list[str], *needles: str) -> int | None:
    """Index of the first field whose (lowercased) name contains any needle."""
    for i, f in enumerate(fields):
        fl = f.lower()
        if any(nd in fl for nd in needles):
            return i
    return None


def _res9_cell(value: str) -> str | None:
    """A source H3 index string -> the cell itself if it's a valid cell at least
    as fine as CELL_HEX_RES (so it has a res-7 parent we can count it toward), or
    None so a stray column value can't crash a load."""
    try:
        if h3.is_valid_cell(value) and h3.get_resolution(value) >= CELL_HEX_RES:
            return value
    except (ValueError, TypeError):
        pass
    return None


def _coverage_tier(any_count: int, fast_count: int) -> str | None:
    """Grade one res-7 hex from its covered-children tallies: 'Fast' (>= 50% of
    children have fast 5G), 'Solid' (>= 2/3 have any coverage), 'Patchy' (some but
    below the Solid bar), or None (no covered children — a real gap). Pure +
    deterministic; the ordinal that turns FCC optimism into honest grades."""
    if fast_count >= _CELL_FAST_MIN:
        return "Fast"
    if any_count >= _CELL_SOLID_MIN:
        return "Solid"
    if any_count >= 1:
        return "Patchy"
    return None


def _classify_parents(any_cells: set[str], fast_cells: set[str]) -> dict[str, str]:
    """Map each covered res-7 parent to its quality tier (`_coverage_tier`), by
    counting how many of its res-9 children fall in the any-coverage set and the
    fast set. Pure + deterministic. `fast_cells` ⊆ `any_cells`."""
    any_counts: dict[str, int] = {}
    fast_counts: dict[str, int] = {}
    for c in any_cells:
        p = h3.cell_to_parent(c, CELL_HEX_RES)
        any_counts[p] = any_counts.get(p, 0) + 1
    for c in fast_cells:
        p = h3.cell_to_parent(c, CELL_HEX_RES)
        fast_counts[p] = fast_counts.get(p, 0) + 1
    out = {}
    for p, a in any_counts.items():
        tier = _coverage_tier(a, fast_counts.get(p, 0))
        if tier is not None:
            out[p] = tier
    return out


def _hex_polygon_wkt(cell: str) -> str:
    """WKT POLYGON for an H3 cell boundary (lon/lat ring, closed)."""
    ring = [(lng, lat) for (lat, lng) in h3.cell_to_boundary(cell)]
    ring.append(ring[0])
    return "POLYGON((" + ",".join(f"{x} {y}" for x, y in ring) + "))"


def _iter_cell_records(path: str):
    """Yield (brand, res9_cell) for every covered hex in one FCC mobile file
    (shapefile base path or .gpkg). Uses an H3-index column when present (fast,
    geometry-free); otherwise falls back to each hex's centroid -> res-9 cell.
    Parent aggregation + the coverage threshold happen in the caller. Records
    outside CELL_STATES_BBOX are dropped."""
    w, s, e, n = CELL_STATES_BBOX
    if path.lower().endswith(".gpkg"):
        g = connect(path)
        try:
            tables = [
                r[0] for r in g.execute(
                    "SELECT table_name FROM gpkg_contents WHERE data_type='features'"
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            tables = []
        for tbl in tables:
            cols = [r[1] for r in g.execute(f"PRAGMA table_info('{tbl}')").fetchall()]
            bi = _find_field(cols, "brand", "holding", "operator", "provider_n", "company")
            hi = _find_field(cols, "h3", "hex")
            geo = _find_field(cols, "geom", "shape", "geometry")
            bcol = cols[bi] if bi is not None else "NULL"
            if hi is not None:
                for brand, hexid in g.execute(f'SELECT "{bcol}", "{cols[hi]}" FROM "{tbl}"'):
                    cell = _res9_cell(hexid)
                    if cell is not None:
                        yield brand, cell
            elif geo is not None:
                gc = cols[geo]
                for brand, lon, lat in g.execute(
                    f'SELECT "{bcol}", X(Centroid(GeomFromGPB("{gc}"))), '
                    f'Y(Centroid(GeomFromGPB("{gc}"))) FROM "{tbl}"'
                ):
                    if lon is None or not (w <= lon <= e and s <= lat <= n):
                        continue
                    yield brand, h3.latlng_to_cell(lat, lon, CELL_SRC_RES)
        g.close()
        return
    # Shapefile.
    rdr = shapefile.Reader(path)
    flds = [f[0] for f in rdr.fields[1:]]
    bi = _find_field(flds, "brand", "holding", "operator", "provider_n", "company")
    hi = _find_field(flds, "h3", "hex")
    for sr in rdr.iterShapeRecords():
        brand = sr.record[bi] if bi is not None else None
        if hi is not None:
            cell = _res9_cell(sr.record[hi])
            if cell is not None:
                yield brand, cell
            continue
        geom = sr.shape.__geo_interface__
        if not geom or not geom.get("coordinates"):
            continue
        c = shape(geom).centroid
        if not (w <= c.x <= e and s <= c.y <= n):
            continue
        yield brand, h3.latlng_to_cell(c.y, c.x, CELL_SRC_RES)


def _cell_files(directory: str) -> list[str]:
    """Shapefile bases (.shp) + .gpkg under `directory`, after extracting any
    .zip in place. Files whose name marks 3G are skipped (we keep 4G LTE + 5G)."""
    if not os.path.isdir(directory):
        return []
    for fn in sorted(os.listdir(directory)):
        if fn.lower().endswith(".zip"):
            try:
                zipfile.ZipFile(os.path.join(directory, fn)).extractall(directory)
            except zipfile.BadZipFile:
                print(f"[cell_coverage] bad zip skipped: {fn}", flush=True)
    out = []
    for root, _dirs, files in os.walk(directory):
        for fn in sorted(files):
            low = fn.lower()
            if re.search(r"(^|[_-])3g([_-]|\.)", low):  # skip 3G maps
                continue
            if low.endswith(".gpkg"):
                out.append(os.path.join(root, fn))
            elif low.endswith(".shp"):
                out.append(os.path.join(root, fn[:-4]))
    return out


def load_cell_coverage(con) -> int:
    """The `cell_coverage` polygon layer: a graded coverage-quality map (res-7
    hexes) from the FCC BDC mobile files staged under CELL_DIR. Each hex is tiered
    Fast / Solid / Patchy by how solidly its sub-cells are covered (see
    `_coverage_tier`); uncovered hexes are simply absent (a real gap). Guarded to
    the four nationwide carriers. Display-only (TLC-3), NEVER scored. Skips cleanly
    (returns 0) if no files are staged."""
    if h3 is None:
        print("[cell_coverage] h3 not installed — run with `--with h3`; skipping", flush=True)
        return 0
    files = _cell_files(CELL_DIR)
    if not files:
        print(f"[cell_coverage] no FCC files in {CELL_DIR} — skipping", flush=True)
        return 0
    print(f"[cell_coverage] {len(files)} file(s) in {CELL_DIR}", flush=True)
    # Two res-9 tallies feed the grade: every covered cell (any 4G/5G) and the
    # fast (5G 35/3) subset. Carrier (row brand or filename) is a GUARD — only the
    # four nationwide carriers count, so a stray regional file can't leak in.
    any_cells: set[str] = set()
    fast_cells: set[str] = set()
    carriers: set[str] = set()
    for path in files:
        src = _cell_source(os.path.basename(path))
        if src is None:
            print(f"[cell_coverage]   {os.path.basename(path)}: skipped (not 4G/5G)", flush=True)
            continue
        file_carrier = _cell_carrier(os.path.basename(path))  # None for id-only names
        kept = 0
        try:
            for brand, cell in _iter_cell_records(path):
                carrier = _cell_carrier(brand) or file_carrier
                if carrier is None:
                    continue  # not one of the four nationwide carriers — drop
                carriers.add(carrier)
                any_cells.add(cell)
                if src == "fast":
                    fast_cells.add(cell)
                kept += 1
        except Exception as exc:  # best-effort per file (bad/foreign schema)
            print(f"[cell_coverage]   {os.path.basename(path)} SKIPPED: {exc}", flush=True)
            continue
        print(
            f"[cell_coverage]   {os.path.basename(path)} [{src}]: {kept} covered res-9 hexes",
            flush=True,
        )
    tiers = _classify_parents(any_cells, fast_cells)  # res-7 parent -> Fast/Solid/Patchy
    clear_layer(con, CELL_COVERAGE_LAYER)
    w, s, e, n = CELL_STATES_BBOX
    by_tier: dict[str, list] = {t: [] for t in CELL_TIERS}
    for cell, tier in tiers.items():
        lat, lon = h3.cell_to_latlng(cell)  # drop parents outside the 4-state box
        if not (w <= lon <= e and s <= lat <= n):
            continue
        by_tier[tier].append((tier, None, _hex_polygon_wkt(cell), 4326))
    total = 0
    for tier in CELL_TIERS:  # best-first so the ramp draws light over dark cleanly
        rows = by_tier[tier]
        for i in range(0, len(rows), 2000):
            _insert(con, CELL_COVERAGE_LAYER, rows[i : i + 2000])
        total += len(rows)
        print(f"[cell_coverage]   {tier}: {len(rows)} hexes", flush=True)
    print(
        f"[cell_coverage] carriers: {sorted(carriers) or 'none'}; "
        f"any={len(any_cells)} fast={len(fast_cells)} res-9; total hexes: {total}",
        flush=True,
    )
    return total


# WI DNR managed-property layer: all DNR-owned properties (parks, forests, rec
# areas, wildlife areas, …) keyed by PROP_NAME — there's no per-type service, so
# parks vs forests are selected by name. (data-wi-dnr.opendata.arcgis.com backs
# onto this dnrmaps service.)
WI_MGD = "https://dnrmaps.wi.gov/arcgis/rest/services/LF_DML/LF_DNR_MGD_PROPERTY_WTM_Ext/MapServer/6"


# --- per-state source registry ---------------------------------------------
# Adding a state = adding a block here. Serving (store/API/UI) is state-blind;
# every source for a layer unions under one shared layer id. Federal sources
# (RIDB campgrounds, USFS forests) are national datasets that need only the
# `bbox` (USFS envelope) and the 2-letter code (RIDB). State boundary SHPs and
# DNR campgrounds are bespoke per state's open-data product.
#   SHP source tuple:    (layer, zip_url, shp_basename, cat_field|None, name_field|None, srid)
#   ArcGIS source tuple: (layer, rest_layer_url, where_clause, name_field)
STATES: dict[str, dict] = {
    "MN": {
        "name": "Minnesota",
        "bbox": (-97.5, 43.0, -89.0, 49.5),
        "shp_sources": [
            # govt_own categories: Federal / State / County Fee / Tax Forfeit / Tribal
            ("ownership",
             f"{GDRS}/us_mn_state_mngeo/plan_gov_own_open/shp_plan_gov_own_open.zip",
             "plan_gov_own_open", "govt_own", "co_name", 26915),
            ("state_forest",
             f"{GDRS}/us_mn_state_dnr/bdry_state_forest/shp_bdry_state_forest.zip",
             "state_forest_boundaries", None, "NAME", 26915),
            ("state_park",
             f"{GDRS}/us_mn_state_dnr/bdry_dnr_lrs_prk/shp_bdry_dnr_lrs_prk.zip",
             "dnr_stat_plan_areas_prk", "AREA_TYPE", "AREA_NAME", 26915),
        ],
        "arcgis_sources": [],
        "campground_loaders": [load_state_forest_campgrounds, load_state_park_campgrounds],
    },
    "WI": {
        "name": "Wisconsin",
        "bbox": (-92.9, 42.4, -86.8, 47.1),  # covers Chequamegon-Nicolet NF
        "shp_sources": [],
        # WI publishes no statewide parcel-ownership SHP like MN's govt_own, so
        # `ownership` (the "public land only" filter's data) comes from PAD-US
        # (national) — federal/state/county fee land. Coarser than MN's parcels.
        "padus_state": "WI",
        # WI has no shapefile/per-type service — parks & forests are rows in one
        # managed-property layer, selected by PROP_NAME.
        "arcgis_sources": [
            ("state_park", WI_MGD,
             "PROP_NAME LIKE '%STATE PARK%' OR PROP_NAME LIKE '%RECREATION AREA%'", "PROP_NAME"),
            ("state_forest", WI_MGD, "PROP_NAME LIKE '%STATE FOREST%'", "PROP_NAME"),
        ],
        # WI DNR publishes NO campground GIS layer (data is GoingToCamp-only), so
        # state-park/forest campgrounds come from OSM, categorized by the
        # boundary they sit in. Federal campgrounds still come from RIDB.
        "campground_loaders": [osm_state_park_campgrounds("US-WI")],
    },
    "SD": {
        "name": "South Dakota",
        "bbox": (-104.1, 42.4, -96.4, 46.0),  # whole state; covers the Black Hills + grasslands
        # No state SHP/ArcGIS product like MN's; ownership + state parks (incl.
        # Custer SP) come from PAD-US (national), which also covers any future
        # state. USFS forests + RIDB campgrounds are national (auto).
        "shp_sources": [],
        "arcgis_sources": [],
        "padus_state": "SD",  # PAD-US State_Nm -> ownership + state_park + national_park
        # Custer SP loops etc. from OSM (no SD campground GIS), categorized by the
        # state-park boundary they sit in. Federal sites still come from RIDB.
        "campground_loaders": [osm_state_park_campgrounds("US-SD")],
    },
    "WY": {
        "name": "Wyoming",
        # Whole state (104°03′W–111°03′W, 41°N–45°N), nudged out a hair: Yellowstone,
        # Grand Teton, the Wind Rivers, the Bighorns, the Snowy Range, the Black Hills
        # spillover. A big federal-land state (BLM + Bridger-Teton/Shoshone/Bighorn/
        # Medicine Bow-Routt/Caribou-Targhee NFs).
        "bbox": (-111.1, 40.9, -104.0, 45.1),
        # Like SD: no state DNR GIS product, and WY has NO state forest system, so
        # `ownership` (the public-land filter's data) + state parks + NPS context
        # all come from PAD-US (national). USFS forests + RIDB campgrounds are
        # national (auto). `state_forest` is simply empty for WY.
        "shp_sources": [],
        "arcgis_sources": [],
        "padus_state": "WY",  # PAD-US State_Nm -> ownership + state_park + national_park
        # WY has no campground GIS; state-park campgrounds come from OSM, categorized
        # by the park boundary they sit in (meta.state='WY' -> UI links reservations
        # to reserve.wyoming.gov). Federal sites still come from RIDB.
        "campground_loaders": [osm_state_park_campgrounds("US-WY")],
    },
}


def main() -> None:
    import sys

    only = set(sys.argv[1:]) or None  # e.g. `... national_forest` reloads one layer
    states = list(STATES)  # every configured state; layers union across them
    print(
        f"overlays DB: {DB} | states: {states}"
        + (f" | layers: {sorted(only)}" if only else ""),
        flush=True,
    )
    con = connect(DB)
    ensure_schema(con)
    total = 0

    # Polygon boundary layers, unioned across states under shared layer ids.
    # Clear each selected layer ONCE (across both SHP + ArcGIS sources), then
    # append every state's sources — SHP first, so an ArcGIS failure can't drop
    # features a shapefile source already loaded into the same layer.
    shp = [
        src
        for st in states
        for src in STATES[st]["shp_sources"]
        if not only or src[0] in only
    ]
    arc = [
        src
        for st in states
        for src in STATES[st]["arcgis_sources"]
        if not only or src[0] in only
    ]
    # PAD-US states feed ownership + state_park too — clear those even when no
    # SHP/ArcGIS source does, so a PAD-US-only state's layers rebuild cleanly.
    padus_states = [s for s in states if STATES[s].get("padus_state")]
    clear_layers = list(dict.fromkeys(src[0] for src in [*shp, *arc]))
    if padus_states:
        for lyr in ("ownership", "state_park", "national_park"):
            if (not only or lyr in only) and lyr not in clear_layers:
                clear_layers.append(lyr)
    for layer in clear_layers:
        clear_layer(con, layer)
    for src in shp:
        total += load_shp(con, *src)
    for src in arc:
        total += load_arcgis_features(con, *src)
    # PAD-US (national) — ownership + state parks for states that opt in.
    for s in padus_states:
        total += load_padus(con, STATES[s]["padus_state"], only)

    # National forests — one USFS query over the union of state envelopes
    # (a single query avoids returning a cross-border forest twice).
    if not only or "national_forest" in only:
        bbs = [STATES[s]["bbox"] for s in states]
        union = (
            min(b[0] for b in bbs), min(b[1] for b in bbs),
            max(b[2] for b in bbs), max(b[3] for b in bbs),
        )
        total += load_usfs(con, union)

    # Designated Wilderness (PAD-US Des_Tp='WA') across the configured states —
    # the Boundary Waters Canoe Area Wilderness (Superior NF), Black Elk (Black
    # Hills), the FWS refuge wildernesses, etc. Display-only context (TLC-3): a
    # vehicle-EXCLUDED boundary (paddle/hike-in only) — you can't tow a teardrop
    # in, so it's exactly where NOT to plan a drive-in site.
    if not only or "wilderness" in only:
        clear_layer(con, "wilderness")
        where = "Des_Tp='WA' AND State_Nm IN (" + ",".join(f"'{s}'" for s in states) + ")"
        total += load_arcgis_features(con, "wilderness", PADUS_FS, where, "Unit_Nm")

    if not only or CAMPGROUND_LAYER in only:
        total += load_campgrounds(con, states)
    if not only or DISPERSED_LAYER in only:
        total += load_dispersed(con, states)
    # Resupply + nearest-town — buffered around the NF/SF boundaries loaded above
    # (so run forests first if reloading just this layer).
    if not only or AMENITY_LAYER in only:
        total += load_amenities(con, states)
    # Cell coverage — FCC BDC mobile hexes staged under CELL_DIR (independent of
    # the STATES registry; covers MN/WI/MI/SD). Skips cleanly if none staged.
    if not only or CELL_COVERAGE_LAYER in only:
        total += load_cell_coverage(con)
    print("--- overlay_features (polygons) ---", flush=True)
    for lyr, c in con.execute(
        "SELECT layer, COUNT(*) FROM overlay_features GROUP BY layer ORDER BY layer"
    ).fetchall():
        print(f"  {lyr:16s} {c}", flush=True)
    print("--- overlay_points ---", flush=True)
    for lyr, c in con.execute(
        "SELECT layer, COUNT(*) FROM overlay_points GROUP BY layer ORDER BY layer"
    ).fetchall():
        print(f"  {lyr:16s} {c}", flush=True)
    print(f"TOTAL {total}", flush=True)
    con.close()


if __name__ == "__main__":
    main()
