"""Live MN state-park availability — UseDirect (Phase 2 / PR2, display-only).

MN's reservation backend (reservemn.usedirect.com) exposes an undocumented but
stable JSON API. The per-campground availability grid is:

    POST {grid_url}  {"FacilityId": N, "StartDate": "YYYY-MM-DD", "Nights": k, …}

returning `Facility.Units[]`, each with `Slices{date: {IsFree}}` where
`IsFree == true` means the unit is open that night. A campground marker maps to
a FacilityId (or several loop-facilities) via the committed offline table; this
client fetches one facility's grid and returns per-unit nightly openness.

Same posture as the federal client: browser UA (the one TLC-5 exception), no
auth (TLC-12 — the POST is a read, see CourtesyHttp.post_json), short-TTL
disposable disk cache, best-effort + degradable. The grid returns a multi-week
window from StartDate regardless of Nights; callers index the specific nights.
"""

from __future__ import annotations

import json
import logging

import httpx

from cairn.clients.cache import DiskCache
from cairn.clients.http import CourtesyHttp
from cairn.config import Settings
from cairn.errors import AvailabilityBadResponse, AvailabilityUnreachable

log = logging.getLogger("cairn.usedirect")


class UseDirectClient:
    """Per-facility availability grid over CourtesyHttp + a short disk cache."""

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

    async def facility_grid(
        self, facility_id: int, start_date: str, nights: int
    ) -> dict[str, dict[str, bool]]:
        """One facility's grid -> `{unit_id: {YYYY-MM-DD: is_free}}`.

        Fresh hits return the cached grid; misses POST and cache the raw JSON.
        Raises AvailabilityUnreachable / AvailabilityBadResponse on failure so
        the caller can degrade that one campground.
        """
        key = f"usedirect:{facility_id}:{start_date}:{nights}"
        cached = self._cache.get(key)
        if cached is not None:
            try:
                return self._parse_grid(json.loads(cached))
            except (ValueError, AttributeError):
                pass  # corrupt cache entry — refetch below

        body = {
            "FacilityId": facility_id,
            "StartDate": start_date,
            "Nights": nights,
            "UnitCategoryId": 0,
            "SleepingUnitId": 0,
            "MinVehicleLength": 0,
            "UnitTypesGroupIds": [],
            "InSeasonOnly": False,
            "WebOnly": True,
            "IsADA": False,
            "RestrictADA": False,
            "UnitSort": "orderby",
        }
        try:
            resp = await self._http.post_json(self._s.usedirect_grid_url, body, timeout=30.0)
        except httpx.HTTPError as exc:
            raise AvailabilityUnreachable(
                f"usedirect unreachable for facility {facility_id}: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise AvailabilityBadResponse(
                f"usedirect returned {resp.status_code} for facility {facility_id}"
            )
        try:
            raw = resp.json()
        except ValueError as exc:
            raise AvailabilityBadResponse(
                f"usedirect returned non-JSON for facility {facility_id}: {exc}"
            ) from exc
        self._cache.put(key, resp.content)
        return self._parse_grid(raw)

    @staticmethod
    def _parse_grid(raw: dict) -> dict[str, dict[str, bool]]:
        """Flatten the grid to `{unit_id: {YYYY-MM-DD: is_free}}`.

        Slices are keyed by a date string; each carries IsFree (open that night).
        """
        out: dict[str, dict[str, bool]] = {}
        facility = raw.get("Facility") or {}
        units = facility.get("Units") or {}
        if isinstance(units, dict):
            units = list(units.values())
        for u in units:
            slices = (u or {}).get("Slices") or {}
            if isinstance(slices, dict):
                slices = list(slices.values())
            night = {str(s.get("Date"))[:10]: bool(s.get("IsFree")) for s in slices}
            out[str(u.get("UnitId"))] = night
        return out

    async def aclose(self) -> None:
        await self._http.aclose()
