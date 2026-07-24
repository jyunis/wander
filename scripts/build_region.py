"""Build an offline vector basemap (PMTiles) for Wander Field.

Extracts a bbox out of a Protomaps daily planet build into a standalone
`.pmtiles` archive under `data/regions/`, which `/field/regions` then serves and
the field PWA downloads once, over signal, for offline use.

Keyless. Protomaps publishes the planet as one big PMTiles archive supporting
HTTP range requests, so `pmtiles extract` pulls **only the tiles inside the
bbox** — a 3°×1.3° forest extract is ~31 MB against a ~120 GB planet, and we
never download the planet. Only the last ~3 daily builds stay up, so the build
date is probed, not pinned.

Needs the `pmtiles` CLI on PATH (or `$PMTILES_BIN`) — a single prebuilt Go
binary, no toolchain:

    curl -sL https://github.com/protomaps/go-pmtiles/releases/download/v1.22.3/\
go-pmtiles_1.22.3_Linux_x86_64.tar.gz | tar xz pmtiles

Run (host or in-image; writes into the mounted data dir):

    python scripts/build_region.py                # every region that's missing
    python scripts/build_region.py superior-nf    # just this one
    python scripts/build_region.py --force superior-nf

Schema is Protomaps Basemap v4: earth / water / landcover / landuse /
roads[kind] / boundaries / places / pois. Display-only (TLC-3) — a basemap is
never a scoring input.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("build_region")

BUILD_URL = "https://build.protomaps.com/{date}.pmtiles"
# Protomaps keeps only the last few daily builds; probe back from today.
MAX_BUILD_AGE_DAYS = 5

# Regions we ship. bbox = (min_lon, min_lat, max_lon, max_lat).
#
# z14 is the floor for useful last-mile navigation (individual forest spurs are
# legible) and the ceiling for a phone-sized archive — MapLibre overzooms past
# it. Keep display names in sync with cairn.field.REGION_NAMES.
#
# The bbox MUST CONTAIN the forest it is named after — the first cut of
# superior-nf (-92.9,47,-89.9,48.3) clipped the forest's western and northern
# lobes off while swallowing half of Lake Superior, which is exactly what a user
# sees when the boundary outline runs off the edge of the download. These boxes
# are the forest's own bounds (src/cairn/data/region_shapes/) plus a margin for
# the approach roads, which is the last-mile part of last-mile navigation.
#
# It stays a RECTANGLE deliberately. `pmtiles extract` also takes a --region
# polygon, but then tiles exist inside the bbox and not inside the shape, and the
# client — which paints out everything outside the downloaded area — would have
# to mask to that shape or the map would disintegrate again inside its own box.
REGIONS: dict[str, dict] = {
    "superior-nf": {"bbox": (-93.45, 46.70, -89.75, 48.70), "maxzoom": 14},
    "chippewa-nf": {"bbox": (-94.90, 46.80, -93.40, 47.90), "maxzoom": 14},
}


def find_build_url() -> str:
    """The most recent daily planet build that's actually still up."""
    for age in range(MAX_BUILD_AGE_DAYS):
        stamp = (date.today() - timedelta(days=age)).strftime("%Y%m%d")
        url = BUILD_URL.format(date=stamp)
        req = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                if res.status == 200:
                    log.info("using planet build %s", stamp)
                    return url
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
    raise SystemExit(
        f"No Protomaps build found in the last {MAX_BUILD_AGE_DAYS} days "
        f"({BUILD_URL.format(date='YYYYMMDD')}). Is build.protomaps.com reachable?"
    )


def pmtiles_bin() -> str:
    """Locate the `pmtiles` CLI, with a copy-pasteable hint when it's missing."""
    found = os.environ.get("PMTILES_BIN") or shutil.which("pmtiles")
    if not found:
        raise SystemExit(
            "pmtiles CLI not found. Install the prebuilt binary (no toolchain):\n"
            "  https://github.com/protomaps/go-pmtiles/releases\n"
            "then put it on PATH or set PMTILES_BIN=/path/to/pmtiles"
        )
    return found


def build(region_id: str, out_dir: Path, build_url: str, force: bool) -> Path | None:
    spec = REGIONS[region_id]
    out = out_dir / f"{region_id}.pmtiles"
    if out.exists() and not force:
        size_mb = out.stat().st_size / 1e6
        log.info("%s: already built (%.1f MB) — pass --force to rebuild", region_id, size_mb)
        return None

    min_lon, min_lat, max_lon, max_lat = spec["bbox"]
    # Extract to a temp name so a failed/killed run never leaves a truncated
    # archive where the region registry (and a phone mid-download) can find it.
    tmp = out.with_suffix(".pmtiles.partial")
    cmd = [
        pmtiles_bin(), "extract", build_url, str(tmp),
        f"--bbox={min_lon},{min_lat},{max_lon},{max_lat}",
        f"--maxzoom={spec['maxzoom']}",
    ]
    log.info("%s: extracting %s", region_id, " ".join(cmd[2:]))
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"{region_id}: pmtiles extract failed ({exc.returncode})") from exc

    tmp.replace(out)
    log.info("%s: wrote %s (%.1f MB)", region_id, out, out.stat().st_size / 1e6)
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Build offline PMTiles basemaps for Wander Field.")
    ap.add_argument("regions", nargs="*", choices=list(REGIONS), help="default: all")
    ap.add_argument("--force", action="store_true", help="rebuild even if the archive exists")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path(os.environ.get("CAIRN_REGIONS_DIR", "data/regions")),
        help="default: data/regions (or $CAIRN_REGIONS_DIR)",
    )
    args = ap.parse_args()

    wanted = args.regions or list(REGIONS)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    todo = [r for r in wanted if args.force or not (args.out_dir / f"{r}.pmtiles").exists()]
    if not todo:
        log.info("nothing to build (%s already present)", ", ".join(wanted))
        return

    # Probe for a live build only once we know there's work — it's a network hit.
    build_url = find_build_url()
    for region_id in todo:
        build(region_id, args.out_dir, build_url, args.force)


if __name__ == "__main__":
    sys.exit(main())
