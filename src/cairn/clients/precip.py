"""Recent-precipitation client for the road-mud wetness banner (P2).

The static `mud_tier` baked onto roads answers "bad *when* wet"; this answers
"*is* it wet now" — the live half a reader multiplies against the tier by eye
("Soft road + saturated forest = stay out"). One Open-Meteo forecast fetch per
forest centroid (keyless, near-real-time), cached a few hours.

DISPLAY-ONLY and CLOCK-DEPENDENT: this is recent rainfall, so it is barred from
the scoring engine twice over — TLC-3 (display context, never an input) AND
TLC-1 (a clock reading would destroy determinism). It is never written into the
baked tier. Guarded by `test_engine_never_references_precip`.

The summary math (`_summarize` + its helpers) is a pure function of the daily
precip array — no clock, no network — so it's unit-tested directly. The client
only fetches, caches, and hands the parsed arrays to it.
"""

from __future__ import annotations

import json

import httpx

from cairn.clients.cache import DiskCache
from cairn.clients.http import CourtesyHttp
from cairn.config import Settings
from cairn.errors import PrecipBadResponse, PrecipUnreachable

# --- pure summary helpers (no clock, no network) ---------------------------

# The headline window: "X in / 72h" = the last three daily totals.
WINDOW_DAYS = 3
# Per-day decay for the wetness label so yesterday counts for more than a week
# ago (age 0 = most recent day). 0.55 -> a day's rain is ~half as relevant each
# day older; a week-old storm barely registers.
DECAY = 0.55
# Wetness label cut points on the decay-weighted recent inches. First guess,
# tunable like the mud-tier weights (a calibration pass pairs the two).
WETNESS_BANDS = ((0.10, "dry"), (0.50, "damp"), (1.20, "wet"))  # else "saturated"


def _window_sum(values: list[float], days: int) -> float:
    """Sum of the last `days` daily totals (the headline 72h figure)."""
    return float(sum(values[-days:]))


def _decay_weighted(values: list[float], decay: float = DECAY) -> float:
    """Decay-weighted recent rainfall (values most-recent-LAST): the last day
    has weight 1, each older day `decay`× less. Emphasizes fresh rain over a
    storm that's had a week to drain. Pure + deterministic."""
    n = len(values)
    return float(sum(v * decay ** (n - 1 - i) for i, v in enumerate(values)))


def _wetness_label(weighted_in: float) -> str:
    """Decay-weighted recent inches -> dry | damp | wet | saturated."""
    for thr, label in WETNESS_BANDS:
        if weighted_in < thr:
            return label
    return "saturated"


def _season_note(month: int) -> str:
    """A short seasonal caveat — mud is as much about season as today's rain.
    Spring frost-out is the real mud season; summer dries fast; frozen ground
    isn't muddy. `month` is 1-12 (0/unknown -> no note)."""
    if month in (4, 5):
        return "spring frost-out — peak mud season; check seasonal load restrictions"
    if month in (6, 7, 8):
        return "summer — dries fast after rain"
    if month in (9, 10):
        return "fall — cooler, dries slower"
    if month in (11, 12, 1, 2, 3):
        return "winter — ground likely frozen; mud model less relevant"
    return ""


def _summarize(lat: float, lon: float, dates: list[str], precip: list) -> dict:
    """Build the banner summary from Open-Meteo's daily arrays (most-recent-last).
    Pure: the only "clock" is the latest date Open-Meteo returned, read off
    `dates`, never the system clock. Nulls in `precip` count as 0."""
    vals = [float(p or 0.0) for p in precip]
    as_of = dates[-1] if dates else None
    weighted = _decay_weighted(vals)
    month = int(as_of.split("-")[1]) if as_of else 0
    return {
        "lat": lat,
        "lon": lon,
        "as_of": as_of,
        "window_h": WINDOW_DAYS * 24,
        "recent_in": round(_window_sum(vals, WINDOW_DAYS), 2),
        "weighted_in": round(weighted, 2),
        "label": _wetness_label(weighted),
        "season_note": _season_note(month),
        "daily": [{"date": d, "in": round(float(p or 0.0), 2)} for d, p in zip(dates, precip)][
            -WINDOW_DAYS - 4 :
        ],  # last 7 days for a sparkline/tooltip
        "source": "open-meteo",
    }


# --- client ----------------------------------------------------------------


class PrecipClient:
    """Open-Meteo recent-rainfall, cached per ~1 km centroid in the disposable
    cache (LIVE data -> short TTL, never the long overlay cache)."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._http = CourtesyHttp(
            settings.user_agent,
            concurrency=2,  # single-user; Open-Meteo is generous but stay polite
            max_retries=settings.http_max_retries,
        )
        assert settings.precip_cache_dir is not None
        self._cache = DiskCache(
            settings.precip_cache_dir, settings.precip_cache_ttl_s, suffix=".json"
        )

    async def recent_rain(self, lat: float, lon: float) -> dict:
        """Recent-rainfall summary near (lat, lon): `{as_of, window_h, recent_in,
        weighted_in, label, season_note, daily, source}`. Cached a few hours per
        ~1 km centroid. Raises PrecipUnreachable/PrecipBadResponse (the route maps
        those to 502; the banner just stays hidden)."""
        # Round the centroid so nearby forest centers share one cache entry.
        key = f"precip/{round(lat, 2)},{round(lon, 2)}/{self._s.precip_past_days}"
        raw = self._cache.get(key)
        if raw is None:
            raw = await self._fetch(lat, lon)
            self._cache.put(key, raw)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise PrecipBadResponse(f"open-meteo non-JSON: {exc}") from exc
        daily = data.get("daily") or {}
        dates = daily.get("time") or []
        precip = daily.get("precipitation_sum") or []
        if not dates or len(dates) != len(precip):
            raise PrecipBadResponse("open-meteo: missing or misaligned daily arrays")
        return _summarize(lat, lon, dates, precip)

    async def _fetch(self, lat: float, lon: float) -> bytes:
        params = {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "daily": "precipitation_sum",
            "past_days": str(self._s.precip_past_days),
            "forecast_days": "1",  # include today's accumulation so far
            "timezone": "auto",
            "precipitation_unit": "inch",
        }
        try:
            resp = await self._http.get(self._s.precip_url, params=params, timeout=20.0)
        except httpx.HTTPError as exc:
            raise PrecipUnreachable(f"open-meteo unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise PrecipBadResponse(f"open-meteo returned {resp.status_code}")
        return resp.content

    async def aclose(self) -> None:
        await self._http.aclose()
