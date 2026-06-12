"""Live federal campground availability — recreation.gov (Phase 2, TLC-3).

Display-only: which sites in a *campground* are open on a trip's dates. Never a
scoring input (engine-purity-guarded in tests/unit/test_tlc1_purity.py).

The endpoint is recreation.gov's undocumented-but-stable per-campground month
feed (the same one the `camply` tool uses):

    GET {base}/{facility_id}/month?start_date=<ISO 1st-of-month>

`start_date` must be percent-encoded or the service 400s "query not encoded";
httpx encodes `params=` for us. Returns `campsites` -> each site's
`availabilities` map of `date -> status` (Available / Reserved / Not
Reservable). Monthly granularity, so a date range loops its months.

The `{facility_id}` is the SAME id Cairn already stores: a federal campground's
saved-place `url` is `recreation.gov/camping/campgrounds/{fid}` — zero mapping.

UA exception (TLC-5): recreation.gov rejects a non-browser User-Agent, so this
one client sends a browser UA (see config.availability_user_agent). Still
GET-only, no auth, no Authorization header — TLC-12 intact. LIVE data, so a
SHORT-TTL disposable disk cache only (config.availability_cache_dir), never the
long overlay cache. Best-effort + fully degradable like RIDB/MVUM.
"""

from __future__ import annotations

import json
import logging

import httpx

from cairn.clients.cache import DiskCache
from cairn.clients.http import CourtesyHttp
from cairn.config import Settings
from cairn.errors import (
    AvailabilityBadResponse,
    AvailabilityNotReservable,
    AvailabilityUnreachable,
)

log = logging.getLogger("cairn.availability")

# A campsite is "open" for a night only when its status is exactly this.
AVAILABLE = "Available"


class RecreationGovClient:
    """Per-campground-month availability over CourtesyHttp + a short disk cache."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._http = CourtesyHttp(
            settings.availability_user_agent,  # browser UA — see module docstring
            concurrency=settings.availability_concurrency,
            max_retries=settings.http_max_retries,
        )
        self._cache = DiskCache(
            settings.availability_cache_dir,
            ttl_s=settings.availability_cache_ttl_s,
            suffix=".json",
        )

    async def campground_month(
        self, facility_id: str, year: int, month: int
    ) -> dict[str, dict[str, str]]:
        """One campground-month -> `{site_id: {date_iso: status}}`.

        Fresh hits return the cached month; misses fetch and cache the raw JSON
        (a stable, reusable unit — overlapping trips share it). Raises
        AvailabilityNotReservable on a 404 (the campground has no reservable
        grid — first-come), and AvailabilityUnreachable / AvailabilityBadResponse
        on transient failure, so the caller can degrade that one campground.
        """
        key = f"recgov:{facility_id}:{year:04d}-{month:02d}"
        cached = self._cache.get(key)
        if cached is not None:
            try:
                return self._parse_month(json.loads(cached))
            except (ValueError, AttributeError):
                pass  # corrupt cache entry — refetch below

        start_date = f"{year:04d}-{month:02d}-01T00:00:00.000Z"
        url = f"{self._s.recgov_availability_url}/{facility_id}/month"
        try:
            resp = await self._http.get(url, params={"start_date": start_date}, timeout=30.0)
        except httpx.HTTPError as exc:
            raise AvailabilityUnreachable(
                f"recreation.gov unreachable for {facility_id}: {exc}"
            ) from exc
        if resp.status_code == 404:
            # No reservable inventory for this facility -> first-come, not an error.
            raise AvailabilityNotReservable(
                f"recreation.gov has no availability grid for {facility_id} (404)"
            )
        if resp.status_code != 200:
            raise AvailabilityBadResponse(
                f"recreation.gov returned {resp.status_code} for {facility_id}"
            )
        try:
            raw = resp.json()
        except ValueError as exc:
            raise AvailabilityBadResponse(
                f"recreation.gov returned non-JSON for {facility_id}: {exc}"
            ) from exc
        self._cache.put(key, resp.content)
        return self._parse_month(raw)

    @staticmethod
    def _parse_month(raw: dict) -> dict[str, dict[str, str]]:
        """Flatten the month payload to `{site_id: {YYYY-MM-DD: status}}`.

        The feed keys availabilities by a full timestamp ("2026-07-01T00:00:00Z");
        we collapse to the date so a trip's plain ISO nights index directly.
        """
        out: dict[str, dict[str, str]] = {}
        for site_id, site in (raw.get("campsites") or {}).items():
            avail = (site or {}).get("availabilities") or {}
            out[str(site_id)] = {str(k)[:10]: str(v) for k, v in avail.items()}
        return out

    async def aclose(self) -> None:
        await self._http.aclose()
