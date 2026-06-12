"""Trip-level campground availability (Phase 2, display-only — TLC-3).

Given a hydrated trip, annotate each *campground*-kind member (any role) with
whether it's open on the trip's dates: "N of M sites open Jul 5-7". Federal
campgrounds resolve via recreation.gov (clients/availability.py); everything
else degrades gracefully to "unsupported" (the UI shows the reservation link it
already has). Dispersed primaries are free/first-come and get nothing.

This is serve-path orchestration, not scoring — engine-purity-guarded so
engine/* can never import it (tests/unit/test_tlc1_purity.py). Best-effort +
fully degradable: one campground failing (or the whole source being offline)
leaves the others, and the trip itself, working — just without badges.

"Open" = a site Available on *every* night of the stay (book the whole trip).
Nights are [start_date, end_date): the checkout day is not a night.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, timedelta

from cairn.clients.availability import AVAILABLE, RecreationGovClient
from cairn.clients.usedirect import UseDirectClient
from cairn.errors import AvailabilityError, AvailabilityNotReservable

log = logging.getLogger("cairn.availability")

# A saved federal campground's url is recreation.gov/camping/campgrounds/{fid}.
_FED_FID_RE = re.compile(r"recreation\.gov/camping/campgrounds/(\d+)", re.IGNORECASE)

SYSTEM_RECGOV = "recreation.gov"
SYSTEM_USEDIRECT = "usedirect"


def is_mn_state_forest(payload: dict | None) -> bool:
    """True for a MN state-FOREST campground (display-only). MN state forest
    campgrounds are first-come, first-served — they take no online reservation
    (UseDirect lists every one with AllowWebBooking=False; only state PARKS are
    bookable). So they get a "first-come" badge, not an availability count.

    WI forests (which DO reserve, via GoingToCamp) are excluded by their url and
    stay link-out."""
    if not payload:
        return False
    url = str(payload.get("url") or "").lower()
    if "wisconsin" in url or "dnr.wi.gov" in url or "goingtocamp" in url:
        return False  # WI -> link out (GoingToCamp), not first-come
    cat = str(payload.get("category") or "")
    name = str(payload.get("name") or "")
    return cat == "State Forest" or "state forest" in name.lower()


def usedirect_facility_ids(
    payload: dict | None, mapping: dict | None
) -> list[int]:
    """The UseDirect FacilityId(s) for a MN state-park campground member, by
    matching the saved marker name against the committed mapping. A list (one
    campground can be several loop-facilities); [] if unmapped -> degrades."""
    if not payload or not mapping:
        return []
    v = mapping.get(payload.get("name"))
    if v is None:
        return []
    return [int(x) for x in v] if isinstance(v, list) else [int(v)]


def federal_facility_id(payload: dict | None) -> str | None:
    """The recreation.gov facility id for a campground member, or None.

    Federal iff the saved category names "Federal" AND the url parses as a
    campground page — the id is then taken straight from that url (zero
    mapping). Anything else (MN/WI state, unverified) returns None -> degrades.
    """
    if not payload:
        return None
    if not re.search(r"federal", str(payload.get("category") or ""), re.IGNORECASE):
        return None
    m = _FED_FID_RE.search(str(payload.get("url") or ""))
    return m.group(1) if m else None


def nights_between(start: str, end: str) -> list[str]:
    """The ISO nights of a stay: [start, end). A same-day (or inverted) range
    is treated as a single night on the start date."""
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if e <= s:
        return [s.isoformat()]
    out, d = [], s
    while d < e:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def months_spanning(nights: list[str]) -> list[tuple[int, int]]:
    """Distinct (year, month) pairs covering the nights, in order."""
    seen: list[tuple[int, int]] = []
    for n in nights:
        d = date.fromisoformat(n)
        if (d.year, d.month) not in seen:
            seen.append((d.year, d.month))
    return seen


def count_open_all_nights(
    month_maps: list[dict[str, dict[str, str]]], nights: list[str]
) -> tuple[int, int]:
    """(open_sites, total_sites): sites Available on *every* night across the
    merged months. total_sites is the campground's full site count."""
    merged: dict[str, dict[str, str]] = {}
    for mm in month_maps:
        for site_id, dates in mm.items():
            merged.setdefault(site_id, {}).update(dates)
    open_count = sum(
        1 for dates in merged.values() if all(dates.get(n) == AVAILABLE for n in nights)
    )
    return open_count, len(merged)


def format_range(start: str, end: str) -> str:
    """A compact human date span: "Jul 5-7", "Jul 30 - Aug 2", "Jul 5"."""
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if s == e:
        return f"{s:%b} {s.day}"
    if (s.year, s.month) == (e.year, e.month):
        return f"{s:%b} {s.day}–{e.day}"
    if s.year == e.year:
        return f"{s:%b} {s.day} – {e:%b} {e.day}"
    return f"{s:%b} {s.day}, {s.year} – {e:%b} {e.day}, {e.year}"


async def trip_availability(
    trip: dict,
    client: RecreationGovClient | None,
    *,
    usedirect_client: UseDirectClient | None = None,
    usedirect_map: dict | None = None,
) -> dict:
    """Per-campground-member availability for a trip, keyed by saved_id.

    Federal campgrounds resolve via recreation.gov; MN state-park campgrounds
    (mapped to a UseDirect FacilityId) via `usedirect_client`; everything else
    degrades to "unsupported" (link out). Returns `{"dates": {...}, "members":
    {saved_id: badge}}` where each badge is one of:
      - {"system": "recreation.gov", "status": "ok", "open_sites", "total_sites",
         "summary"}                      federal, dates set, fetched OK
      - {"system": "recreation.gov", "status": "first_come"} federal, not reservable (FCFS)
      - {"system": "recreation.gov", "status": "no_dates"}   federal, trip has no dates
      - {"system": "recreation.gov", "status": "error"}      federal, source failed
      - {"system": "usedirect", "status": "ok"/"no_dates"/"error"}  MN state park
      - {"system": None, "status": "unsupported"}            unmapped -> link out
    """
    members = trip.get("members") or []
    start, end = trip.get("start_date"), trip.get("end_date")
    cg_members = [m for m in members if m.get("kind") == "campground"]

    nights = nights_between(start, end) if (start and end) else None
    dates_block = {
        "start": start,
        "end": end,
        "nights": len(nights) if nights else 0,
    }

    def _ok_badge(system: str, open_sites: int, total: int) -> dict:
        return {
            "system": system,
            "status": "ok",
            "open_sites": open_sites,
            "total_sites": total,
            "summary": f"{open_sites} of {total} site{'s' if total != 1 else ''} "
            f"open {format_range(start, end)}",
        }

    async def resolve_federal(saved_id: str, payload: dict, fid: str) -> tuple[str, dict]:
        # Fast path: RIDB already flags this federal campground first-come — no
        # reservable grid exists, so skip the (404-ing) fetch entirely.
        if (payload.get("meta") or {}).get("reservable") is False:
            return saved_id, {"system": SYSTEM_RECGOV, "status": "first_come"}
        if nights is None:
            return saved_id, {"system": SYSTEM_RECGOV, "status": "no_dates"}
        if client is None:
            return saved_id, {"system": SYSTEM_RECGOV, "status": "error"}
        try:
            month_maps = [
                await client.campground_month(fid, y, mo)
                for (y, mo) in months_spanning(nights)
            ]
        except AvailabilityNotReservable:
            # 404 -> no reservable inventory; it's first-come, not a failure.
            return saved_id, {"system": SYSTEM_RECGOV, "status": "first_come"}
        except AvailabilityError as exc:
            log.info("recreation.gov fetch failed for %s: %s", fid, exc)
            return saved_id, {"system": SYSTEM_RECGOV, "status": "error"}
        open_sites, total = count_open_all_nights(month_maps, nights)
        return saved_id, _ok_badge(SYSTEM_RECGOV, open_sites, total)

    async def resolve_usedirect(saved_id: str, fids: list[int]) -> tuple[str, dict]:
        if nights is None:
            return saved_id, {"system": SYSTEM_USEDIRECT, "status": "no_dates"}
        if usedirect_client is None:
            return saved_id, {"system": SYSTEM_USEDIRECT, "status": "error"}
        # One window from the first night covering the whole stay; the grid
        # returns a multi-week window regardless, so we just index our nights.
        span = (date.fromisoformat(nights[-1]) - date.fromisoformat(nights[0])).days + 1
        units: dict[str, dict[str, bool]] = {}
        try:
            for fid in fids:  # a campground may be several loop-facilities -> merge
                grid = await usedirect_client.facility_grid(fid, nights[0], span)
                for uid, daymap in grid.items():
                    units[f"{fid}:{uid}"] = daymap
        except AvailabilityError as exc:
            log.info("usedirect fetch failed for %s: %s", fids, exc)
            return saved_id, {"system": SYSTEM_USEDIRECT, "status": "error"}
        if not units:  # no inventory came back -> can't characterize; degrade
            return saved_id, {"system": SYSTEM_USEDIRECT, "status": "error"}
        open_sites = sum(1 for d in units.values() if all(d.get(n) for n in nights))
        return saved_id, _ok_badge(SYSTEM_USEDIRECT, open_sites, len(units))

    async def resolve(member: dict) -> tuple[str, dict]:
        payload = member.get("payload") or {}
        saved_id = str(member.get("saved_id"))
        fid = federal_facility_id(payload)
        if fid is not None:
            return await resolve_federal(saved_id, payload, fid)
        ud_fids = usedirect_facility_ids(payload, usedirect_map)
        if ud_fids:
            return await resolve_usedirect(saved_id, ud_fids)
        if is_mn_state_forest(payload):
            # MN state forests are first-come (no online reservation) — date-
            # independent, so this holds with or without trip dates.
            return saved_id, {"system": SYSTEM_USEDIRECT, "status": "first_come"}
        return saved_id, {"system": None, "status": "unsupported"}

    results = await asyncio.gather(*(resolve(m) for m in cg_members))
    return {"dates": dates_block, "members": dict(results)}
