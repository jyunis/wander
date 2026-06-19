"""Register a USFS MVUM GroundOverlay KMZ as a Wander image-overlay.

USFS publishes per-district MVUMs as KMZ *GroundOverlays* — a single
georeferenced raster (the rendered map sheet), NOT vector data. So the
dispersed-camping corridors exist only as drawn symbols, not queryable
attributes (see documents MCP #33). This script pulls a KMZ, extracts the PNG
and its lat/lon box, and writes them to the MVUM dir + a manifest the API serves;
the UI drapes it as a display-only Leaflet image overlay (TLC-3, never scored).

Run in-image (writes the live data dir):
    docker run --rm -v /extra1/docker/cairn:/app -w /app -e MVUM_DIR=/app/data/mvum \
      --entrypoint uv cairn:dev run python scripts/load_mvum.py

Add a district = add a (url, key, name) line in DISTRICTS below and re-run.
"""

from __future__ import annotations

import io
import json
import os
import re
import urllib.request
import zipfile

MVUM_DIR = os.environ.get("MVUM_DIR", "/data/mvum")
UA = {"User-Agent": "wander-mvum-loader (+http://wander.home.arpa)"}

# (KMZ url, key, display name). key must be [a-z0-9_]+ (used in the image path).
DISTRICTS = [
    ("https://www.fs.usda.gov/media/62696", "mystic", "MVUM — Mystic RD (Black Hills)"),
]


def _tag(kml: str, name: str) -> str | None:
    m = re.search(rf"<{name}>([^<]+)</{name}>", kml)
    return m.group(1).strip() if m else None


def load_district(url: str, key: str, name: str) -> None:
    if not re.fullmatch(r"[a-z0-9_]+", key):
        raise ValueError(f"bad key {key!r} (use [a-z0-9_]+)")
    os.makedirs(MVUM_DIR, exist_ok=True)
    print(f"[mvum] {key}: downloading {url} …", flush=True)
    data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180).read()
    z = zipfile.ZipFile(io.BytesIO(data))
    kml_name = next(n for n in z.namelist() if n.lower().endswith(".kml"))
    kml = z.read(kml_name).decode("utf-8", "replace")
    if "<GroundOverlay" not in kml:
        raise ValueError(f"{key}: not a GroundOverlay KMZ (no raster to drape)")
    rot = _tag(kml, "rotation")
    if rot and abs(float(rot)) > 0.01:
        raise ValueError(f"{key}: rotated overlay ({rot}°) — needs an axis-aligned LatLonBox")
    box = {t: float(_tag(kml, t)) for t in ("north", "south", "east", "west")}
    href = _tag(kml, "href")  # raster filename inside the zip
    png = z.read(href)
    out = os.path.join(MVUM_DIR, f"{key}.png")
    with open(out, "wb") as f:
        f.write(png)
    # Upsert into the manifest (bounds as [south, west, north, east] for Leaflet).
    mpath = os.path.join(MVUM_DIR, "manifest.json")
    manifest = json.load(open(mpath)) if os.path.exists(mpath) else []
    manifest = [m for m in manifest if m.get("key") != key]
    manifest.append({
        "key": key,
        "name": name,
        "image": f"{key}.png",
        "bounds": [box["south"], box["west"], box["north"], box["east"]],
    })
    json.dump(manifest, open(mpath, "w"), indent=2)
    print(f"[mvum] {key}: wrote {out} ({len(png)} bytes); bounds {box}", flush=True)


def main() -> None:
    for url, key, name in DISTRICTS:
        try:
            load_district(url, key, name)
        except Exception as exc:  # noqa: BLE001 - best-effort per district
            print(f"[mvum] {key}: FAILED ({exc})", flush=True)


if __name__ == "__main__":
    main()
