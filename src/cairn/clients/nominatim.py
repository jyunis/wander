"""Nominatim forward geocoder (OSM, keyless).

Turns a place name ("Chippewa National Forest", "Grand Rapids, MN") into
coordinates for the UI jump-to field. Proxied server-side so the request
carries Cairn's descriptive User-Agent — the Nominatim usage policy requires
one, and a browser can't set it. Read-only GET, no key (TLC-12). A small
in-process LRU caches repeated queries so re-typing the same place issues no
network call (and keeps us comfortably under the 1 req/s policy).
"""

from __future__ import annotations

from collections import OrderedDict

import httpx

from cairn.clients.http import CourtesyHttp
from cairn.config import Settings
from cairn.errors import GeocodeBadResponse, GeocodeUnreachable


class GeocodeClient:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._http = CourtesyHttp(
            settings.user_agent,
            concurrency=1,  # single-user, polite: never parallel-hammer Nominatim
            max_retries=settings.http_max_retries,
        )
        self._cache: OrderedDict[str, list[dict]] = OrderedDict()
        self._cache_cap = 128

    async def search(self, query: str, viewbox: str | None = None) -> list[dict]:
        """Geocode `query` -> a list of `{display_name, lat, lon, bbox}` (best
        first). `viewbox` ("min_lon,min_lat,max_lon,max_lat") biases results
        toward that area without restricting to it. Returns [] for no match."""
        q = query.strip()
        if not q:
            return []
        key = f"{q.lower()}|{viewbox or ''}"
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        params = {
            "q": q,
            "format": "jsonv2",
            "limit": str(self._s.geocode_limit),
            "addressdetails": "0",
        }
        if viewbox:
            params["viewbox"] = viewbox
            params["bounded"] = "0"  # prefer, don't restrict
        try:
            resp = await self._http.get(self._s.geocode_url, params=params, timeout=15.0)
        except httpx.HTTPError as exc:
            raise GeocodeUnreachable(f"nominatim unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise GeocodeBadResponse(f"nominatim returned {resp.status_code}")
        try:
            raw = resp.json()
        except ValueError as exc:
            raise GeocodeBadResponse(f"nominatim returned non-JSON: {exc}") from exc

        results = [self._normalize(r) for r in raw if self._normalize(r)]
        self._cache[key] = results
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_cap:
            self._cache.popitem(last=False)
        return results

    @staticmethod
    def _normalize(r: dict) -> dict | None:
        try:
            lat = float(r["lat"])
            lon = float(r["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        out = {"display_name": r.get("display_name", ""), "lat": lat, "lon": lon}
        # Nominatim boundingbox is [south, north, west, east] as strings; expose
        # as [min_lon, min_lat, max_lon, max_lat] so the UI can fitBounds to a
        # forest/city extent rather than a fixed zoom.
        bb = r.get("boundingbox")
        if isinstance(bb, list) and len(bb) == 4:
            try:
                s, n, w, e = (float(x) for x in bb)
                out["bbox"] = [w, s, e, n]
            except (TypeError, ValueError):
                pass
        return out

    async def aclose(self) -> None:
        await self._http.aclose()
