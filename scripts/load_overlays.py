#!/usr/bin/env python3
"""Load static land-overlay reference layers into ``/data/overlays.db``.

These are **display-only** layers — public land ownership (incl. tax-forfeited)
and forest/park boundaries. Per TLC-3 they are NEVER scored or used to assert a
site is legal; Cairn renders them as visual context only. The data is public
(MnGeo / MN DNR downloads + the USFS ArcGIS service).

The overlay store is a separate SpatiaLite file from ``cairn.db`` so the scoring
DB stays lean, and it is fully re-derivable (disposable, excluded from restic) —
re-running this script drops and reloads each layer.

Run in-image with network access (pyshp + shapely added on the fly):

    docker run --rm -v /extra1/docker/cairn:/app -w /app \
      --entrypoint uv cairn:dev \
      run --with pyshp --with shapely python scripts/load_overlays.py

Env: OVERLAYS_DB (default /data/overlays.db).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
import urllib.request
import zipfile

import shapefile  # pyshp
from shapely.geometry import shape

DB = os.environ.get("OVERLAYS_DB", "/data/overlays.db")
UA = {"User-Agent": "cairn-overlays-loader (+http://cairn.home.arpa)"}
GDRS = "https://resources.gisdata.mn.gov/pub/gdrs/data/pub"

# (layer, zip_url, shp_basename, category_field|None, name_field|None, src_srid)
SHP_SOURCES = [
    (
        "ownership",
        f"{GDRS}/us_mn_state_mngeo/plan_gov_own_open/shp_plan_gov_own_open.zip",
        "plan_gov_own_open",
        "govt_own",  # Federal / State / County Fee / Tax Forfeit / Tribal
        "co_name",
        26915,
    ),
    (
        "state_forest",
        f"{GDRS}/us_mn_state_dnr/bdry_state_forest/shp_bdry_state_forest.zip",
        "state_forest_boundaries",
        None,
        "NAME",
        26915,
    ),
    (
        "state_park",
        f"{GDRS}/us_mn_state_dnr/bdry_dnr_lrs_prk/shp_bdry_dnr_lrs_prk.zip",
        "dnr_stat_plan_areas_prk",
        "AREA_TYPE",  # State Park / State Recreation Area / State Wayside
        "AREA_NAME",
        26915,
    ),
]

# USFS national forest administrative boundaries — the one live ArcGIS source
# (keyless). Queried for the Minnesota extent only and returned in EPSG:4326.
USFS_QUERY = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/"
    "EDW_ForestSystemBoundaries_01/MapServer/0/query"
    "?where=1%3D1&geometry=-97.5,43.0,-89.0,49.5&geometryType=esriGeometryEnvelope"
    "&inSR=4326&outSR=4326&spatialRel=esriSpatialRelIntersects"
    "&outFields=FORESTNAME&returnGeometry=true&f=geojson"
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


def load_campgrounds(con) -> int:
    """The unified `campground` point layer (national/state-park/state-forest).
    Cleared once, then appended from each best-effort source."""
    con.execute("DELETE FROM overlay_points WHERE layer=?", (CAMPGROUND_LAYER,))
    con.commit()
    total = 0
    for fn in (load_state_forest_campgrounds, load_state_park_campgrounds, load_ridb_campgrounds):
        try:
            total += fn(con)
        except Exception as exc:  # best-effort per source
            print(f"[campground]   {fn.__name__} SKIPPED: {exc}", flush=True)
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


def load_ridb_campgrounds(con) -> int:
    """Recreation.gov RIDB federal campgrounds (national forest/NPS/BLM/Corps).
    Keyed: needs RIDB_API_KEY in the env. Skipped (returns 0) without it, so the
    state layers still load — same best-effort posture as MVUM/structures. Each
    facility is enriched with reservable/fee/ADA/phone/activities + a campsite
    aggregate (count/types/electric/max-RV)."""
    key = os.environ.get("RIDB_API_KEY")
    if not key:
        print("[campground]   RIDB skipped (no RIDB_API_KEY)", flush=True)
        return 0

    rows, offset = [], 0
    while True:
        data = _ridb_get(
            "https://ridb.recreation.gov/api/v1/facilities?"
            f"activity=CAMPING&state=MN&limit=50&offset={offset}&full=true",
            key,
        )
        recs = data.get("RECDATA", [])
        for f in recs:
            lat, lon = f.get("FacilityLatitude"), f.get("FacilityLongitude")
            fid = str(f.get("FacilityID", ""))
            if not lat or not lon or not fid:
                continue
            blurb = re.sub(r"<[^>]+>", " ", f.get("FacilityDescription") or "")
            blurb = re.sub(r"\s+", " ", blurb).strip()
            meta = {
                "reservable": bool(f.get("Reservable")),
                "fee": (f.get("FacilityUseFeeDescription") or "").strip(),
                "ada": (f.get("FacilityAdaAccess") or "").upper() == "Y",
                "phone": (f.get("FacilityPhone") or "").strip(),
                "activities": [
                    (a.get("ActivityName") or "").title()
                    for a in (f.get("ACTIVITY") or [])
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
            rows.append(("National / Federal", f.get("FacilityName") or "", resv,
                         m, f"POINT({lon} {lat})", 4326))
            time.sleep(0.1)
        offset += len(recs)
        if not recs or offset >= data.get("METADATA", {}).get("RESULTS", {}).get("TOTAL_COUNT", 0):
            break
    _insert_points(con, CAMPGROUND_LAYER, rows)
    print(f"[campground]   RIDB federal: {len(rows)}", flush=True)
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
        clear_layer(con, layer)
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


def load_usfs(con) -> int:
    import json

    layer = "national_forest"
    print(f"[{layer}] querying USFS ArcGIS (MN extent) …", flush=True)
    try:
        req = urllib.request.Request(USFS_QUERY, headers=UA)
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


def main() -> None:
    import sys

    only = set(sys.argv[1:]) or None  # e.g. `... national_forest` reloads one layer
    print(f"overlays DB: {DB}" + (f" (layers: {sorted(only)})" if only else ""), flush=True)
    con = connect(DB)
    ensure_schema(con)
    total = 0
    for layer, url, base, cat, name, srid in SHP_SOURCES:
        if only and layer not in only:
            continue
        total += load_shp(con, layer, url, base, cat, name, srid)
    if not only or "national_forest" in only:
        total += load_usfs(con)
    if not only or CAMPGROUND_LAYER in only:
        total += load_campgrounds(con)
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
