"""Build an offline basemap region from a bbox the user drew (M4).

`scripts/build_region.py` bakes a fixed catalogue; this is the same extract driven
from the app. Pre-baked forest regions turned out to be the wrong unit: a forest
is a rectangle you didn't draw, so when the rectangle doesn't look like the forest
the map reads as broken. A box you drew makes no such promise, and it matches how
a trip is actually planned — "the area I'm going to," not "the Superior NF."

This module only *produces* files in `regions_dir`. `cairn.field.RegionStore` is
unchanged: it still lists whatever `.pmtiles` archives exist there, so pre-baked
and user-drawn regions coexist with no special cases.

Extraction shells out to the `pmtiles` CLI (baked into the image, pinned +
checksummed — see Dockerfile), which pulls **only the tiles inside the bbox** out
of a Protomaps daily planet build over HTTP range requests. Protomaps keeps only
the last few builds, so the date is probed rather than pinned. Keyless (TLC-12).

Display-only (TLC-3): a basemap is never a scoring input.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import httpx

from cairn.config import Settings
from cairn.errors import ValidationError
from cairn.field import SUFFIX, valid_region_id

log = logging.getLogger("cairn.regionbuild")

PARTIAL_SUFFIX = ".pmtiles.partial"
META_SUFFIX = ".json"

# Mercator's latitude limit; a bbox beyond it has no tiles.
_MAX_LAT = 85.05112878


@dataclass
class BuildJob:
    """One extract, in flight or finished. In-memory: a lost job on restart is a
    lost progress bar, not lost data — the archive either landed or it didn't."""

    id: str
    region_id: str
    name: str
    bbox: tuple[float, float, float, float]
    maxzoom: int
    estimated_bytes: int
    state: str = "running"  # running | done | error
    bytes_written: int = 0
    error: str | None = None
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "job_id": self.id,
            "region_id": self.region_id,
            "name": self.name,
            "state": self.state,
            "bytes_written": self.bytes_written,
            "estimated_bytes": self.estimated_bytes,
            "error": self.error,
            "elapsed_s": round(time.time() - self.started_at, 1),
        }


def slugify(name: str) -> str:
    """A region id from a human name. Must satisfy `field.valid_region_id`, since
    it lands in a URL path and is joined onto `regions_dir`."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:64].rstrip("-")


def _tile_xy(lon: float, lat: float, zoom: float) -> tuple[float, float]:
    lat = max(min(lat, _MAX_LAT), -_MAX_LAT)
    n = 1 << int(zoom)
    x = (lon + 180.0) / 360.0 * n
    sin_lat = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * n
    return x, y


def count_tiles(bbox: tuple[float, float, float, float], maxzoom: int) -> int:
    """Tiles in the pyramid z0..maxzoom covering `bbox`.

    Tiles, not degrees², are the unit: Web Mercator stretches high latitudes (the
    reason Greenland looks continental), so a degree of latitude spans more tile
    rows the further north you go. The same-sized box costs ~27% more tiles in
    the BWCA than in south Texas. Everything downstream — the size estimate and
    the cap — counts tiles, so a northern box can't sneak past a southern limit.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    total = 0
    for zoom in range(maxzoom + 1):
        x0, y0 = _tile_xy(min_lon, max_lat, zoom)  # north-west
        x1, y1 = _tile_xy(max_lon, min_lat, zoom)  # south-east
        total += (int(x1) - int(x0) + 1) * (int(y1) - int(y0) + 1)
    return total


@dataclass(frozen=True)
class DryRun:
    """What `pmtiles extract --dry-run` says the archive will be."""

    tiles: int
    bytes: int


# `pmtiles` reports sizes SI-style via humanize: "2.2 MB", "755 kB".
_SIZE_RE = re.compile(r"archive size of ([\d.]+)\s*(B|kB|MB|GB)", re.IGNORECASE)
_TILES_RE = re.compile(r"Region tiles (\d+)")
_SI = {"b": 1, "kb": 1_000, "mb": 1_000_000, "gb": 1_000_000_000}


async def dry_run(settings: Settings, build_url: str, bbox, maxzoom: int) -> DryRun:
    """Ask the extractor how big this box will be, without downloading it.

    A bytes/tile constant cannot do this job: tile size falls from ~63 kB at z6 to
    ~1.1 kB at z14, so a small box is dominated by fixed low-zoom overhead, and
    tile size varies with what's *in* the tile — the dense lake shorelines of the
    BWCA cost 3.4x Superior NF's average. Calibrating on one box mispredicted
    another by 37%. `--dry-run` reads the planet's directories (a handful of range
    requests, ~2-4 s) and reports the real answer.
    """
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    proc = await asyncio.create_subprocess_exec(
        settings.pmtiles_bin, "extract", build_url, "/dev/null",
        f"--bbox={min_lon},{min_lat},{max_lon},{max_lat}",
        f"--maxzoom={maxzoom}", "--dry-run",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=settings.region_dry_run_timeout_seconds
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ValidationError("sizing that area timed out; try again") from None

    text = (stdout or b"").decode(errors="replace")
    if proc.returncode != 0:
        tail = text.strip().splitlines()
        raise ValidationError(f"could not size that area: {tail[-1] if tail else 'pmtiles failed'}")

    size_match = _SIZE_RE.search(text)
    tiles_match = _TILES_RE.search(text)
    if not size_match:
        raise ValidationError("could not size that area (unexpected pmtiles output)")
    value, unit = float(size_match.group(1)), size_match.group(2).lower()
    return DryRun(
        tiles=int(tiles_match.group(1)) if tiles_match else count_tiles(bbox, maxzoom),
        bytes=int(value * _SI[unit]),
    )


def validate_request(name: str, bbox, maxzoom: int, settings: Settings) -> tuple[str, int]:
    """The cheap checks: everything decidable without touching network or disk.

    Returns (region_id, tile_count). The tile cap is a coarse pre-filter that
    rejects the absurd before we spawn a process; the real size limit is enforced
    against `dry_run`, because tiles-to-bytes varies six-fold with terrain.
    """
    if not name or not name.strip():
        raise ValidationError("name is required")
    if len(name) > 60:
        raise ValidationError("name must be 60 characters or fewer")

    region_id = slugify(name)
    if not valid_region_id(region_id):
        raise ValidationError(f"{name!r} has no usable letters or digits for a region id")

    if len(bbox) != 4:
        raise ValidationError("bbox must be [min_lon, min_lat, max_lon, max_lat]")
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    if not (min_lon < max_lon and min_lat < max_lat):
        raise ValidationError("bbox must have min < max on both axes")
    if not (-180 <= min_lon and max_lon <= 180 and -_MAX_LAT <= min_lat and max_lat <= _MAX_LAT):
        raise ValidationError("bbox is outside the mapped world")

    if not (settings.region_min_zoom <= maxzoom <= settings.region_max_zoom):
        raise ValidationError(
            f"maxzoom must be between {settings.region_min_zoom} and {settings.region_max_zoom}"
        )

    tiles = count_tiles((min_lon, min_lat, max_lon, max_lat), maxzoom)
    if tiles > settings.region_max_tiles:
        raise ValidationError(
            f"that area is too big: {tiles:,} tiles at z{maxzoom} "
            f"(cap {settings.region_max_tiles:,}). Draw a smaller box, or lower the detail."
        )
    return region_id, tiles


async def find_build_url(settings: Settings) -> str:
    """The most recent Protomaps daily planet build that is still up."""
    async with httpx.AsyncClient(
        timeout=30, headers={"User-Agent": settings.user_agent}, follow_redirects=True
    ) as client:
        for age in range(settings.region_build_max_age_days):
            stamp = (date.today() - timedelta(days=age)).strftime("%Y%m%d")
            url = settings.protomaps_build_url.format(date=stamp)
            try:
                res = await client.head(url)
            except httpx.HTTPError:
                continue
            if res.status_code == 200:
                log.info("using planet build %s", stamp)
                return url
    raise ValidationError(
        "no Protomaps planet build is currently reachable; try again later"
    )


class RegionBuilder:
    """Runs one extract at a time and reports progress by polling the partial file.

    Serial on purpose: an extract saturates the link to build.protomaps.com, and
    two at once just makes both slow. Single-user service (FC-1).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._dir = Path(settings.regions_dir)
        self._jobs: dict[str, BuildJob] = {}
        self._lock = asyncio.Lock()
        self._current: BuildJob | None = None

    # --- lifecycle --------------------------------------------------------

    def sweep_partials(self) -> int:
        """Delete half-written archives from a killed run. They are named so that
        the region registry never sees them, but they still eat disk."""
        if not self._dir.is_dir():
            return 0
        stale = list(self._dir.glob(f"*{PARTIAL_SUFFIX}"))
        for path in stale:
            path.unlink(missing_ok=True)
        if stale:
            log.info("swept %d partial region archive(s)", len(stale))
        return len(stale)

    # --- jobs -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._current is not None and self._current.state == "running"

    def get(self, job_id: str) -> BuildJob | None:
        return self._jobs.get(job_id)

    async def estimate(self, bbox, maxzoom: int) -> DryRun:
        """Exactly how big this box would be. Used by the picker while you draw."""
        build_url = await find_build_url(self._settings)
        return await dry_run(self._settings, build_url, bbox, maxzoom)

    async def start(self, name: str, bbox, maxzoom: int) -> BuildJob:
        region_id, _tiles = validate_request(name, bbox, maxzoom, self._settings)

        if self.busy:
            raise ValidationError("a region is already being built; wait for it to finish")
        if (self._dir / f"{region_id}{SUFFIX}").exists():
            raise ValidationError(f"a region named {name!r} already exists")

        # Both are network hits and both can fail — do them before we commit to a
        # job, so the user gets a 422 with a real number rather than a dead job.
        build_url = await find_build_url(self._settings)
        planned = await dry_run(self._settings, build_url, bbox, maxzoom)
        if planned.bytes > self._settings.region_max_bytes:
            raise ValidationError(
                f"that area would be {planned.bytes / 1e6:.0f} MB "
                f"(cap {self._settings.region_max_bytes / 1e6:.0f} MB). "
                "Draw a smaller box, or lower the detail."
            )

        job = BuildJob(
            id=uuid.uuid4().hex[:12],
            region_id=region_id,
            name=name.strip(),
            bbox=tuple(float(v) for v in bbox),
            maxzoom=maxzoom,
            estimated_bytes=planned.bytes,
        )
        self._jobs[job.id] = job
        self._current = job
        asyncio.create_task(self._run(job, build_url))
        return job

    async def _run(self, job: BuildJob, build_url: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        out = self._dir / f"{job.region_id}{SUFFIX}"
        tmp = self._dir / f"{job.region_id}{PARTIAL_SUFFIX}"
        tmp.unlink(missing_ok=True)

        min_lon, min_lat, max_lon, max_lat = job.bbox
        cmd = [
            self._settings.pmtiles_bin, "extract", build_url, str(tmp),
            f"--bbox={min_lon},{min_lat},{max_lon},{max_lat}",
            f"--maxzoom={job.maxzoom}",
        ]
        log.info("region %s: %s", job.region_id, " ".join(cmd[1:]))

        progress = asyncio.create_task(self._track(job, tmp))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._settings.region_build_timeout_seconds
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(
                    f"timed out after {self._settings.region_build_timeout_seconds}s"
                ) from None
            if proc.returncode != 0:
                tail = (stderr or b"").decode(errors="replace").strip().splitlines()
                raise RuntimeError(tail[-1] if tail else f"pmtiles exited {proc.returncode}")

            # Only now is it a region: the registry must never see a partial file.
            tmp.replace(out)
            (self._dir / f"{job.region_id}{META_SUFFIX}").write_text(
                json.dumps({"name": job.name, "created": int(time.time())})
            )
            job.bytes_written = out.stat().st_size
            job.state = "done"
            log.info("region %s: built %.1f MB", job.region_id, job.bytes_written / 1e6)
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as job.error
            tmp.unlink(missing_ok=True)
            job.state = "error"
            job.error = str(exc)
            log.warning("region %s failed: %s", job.region_id, exc)
        finally:
            progress.cancel()
            self._current = None

    async def _track(self, job: BuildJob, tmp: Path) -> None:
        """`pmtiles` prints a progress bar we'd rather not parse; the partial
        file's size is a truthful, format-independent proxy."""
        try:
            while True:
                await asyncio.sleep(0.5)
                if tmp.exists():
                    job.bytes_written = tmp.stat().st_size
        except asyncio.CancelledError:
            pass
