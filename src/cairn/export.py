"""Trip export (TLC-3 — user data + display, never scoring).

Turns a hydrated trip dict (as returned by ``Store.get_trip``) into a GPX 1.1
or KML 2.2 waypoint document for handoff to the tools that actually do offline:
onX / Gaia / Garmin (GPX) and Google Earth / Maps (KML). Cairn is the brain
(plan at home), those are the legs (navigate in the field) — export is the
bridge.

Pure string formatting: no network, no clock, no randomness. One waypoint per
trip member, carrying its name, a human-readable description (role · kind ·
rating · score · note), and the role as the waypoint type so primaries and
fallbacks stay distinguishable after import.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape


def trip_slug(trip: dict) -> str:
    """A filesystem-safe slug from the trip name, for the download filename."""
    base = re.sub(r"[^a-z0-9]+", "-", (trip.get("name") or "trip").strip().lower())
    return base.strip("-") or "trip"


def _date_range(trip: dict) -> str:
    start, end = trip.get("start_date"), trip.get("end_date")
    if start and end:
        return f"{start} → {end}"
    return start or end or ""


def _member_label(m: dict) -> str:
    name = (m.get("name") or "").strip()
    if name:
        return name
    if m.get("kind") == "campground":
        return "Campground"
    payload = m.get("payload") or {}
    return "Scored site" if payload.get("total") is not None else "Dropped pin"


def _member_desc(m: dict) -> str:
    """A compact one-line description for the waypoint: role · kind · stars ·
    score · note — whatever's present, so it reads well in onX/Google."""
    parts: list[str] = []
    role = m.get("role")
    if role:
        parts.append(role.capitalize())
    kind = m.get("kind")
    if kind:
        parts.append(str(kind))
    rating = m.get("rating")
    if rating:
        parts.append("★" * int(round(float(rating))))
    total = (m.get("payload") or {}).get("total")
    if total is not None:
        parts.append(f"score {float(total):.2f}")
    note = (m.get("note") or "").strip()
    if note:
        parts.append(note)
    return " · ".join(parts)


def _members(trip: dict) -> list[dict]:
    return trip.get("members") or []


def trip_to_gpx(trip: dict) -> str:
    """The trip as a GPX 1.1 document (one <wpt> per member)."""
    name = escape((trip.get("name") or "Trip").strip())
    date_range = _date_range(trip)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="Cairn" '
        'xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
        'http://www.topografix.com/GPX/1/1/gpx.xsd">',
        "  <metadata>",
        f"    <name>{name}</name>",
    ]
    if date_range:
        lines.append(f"    <desc>{escape(date_range)}</desc>")
    lines.append("  </metadata>")
    for m in _members(trip):
        lat = f"{float(m['lat']):.6f}"
        lon = f"{float(m['lon']):.6f}"
        lines.append(f'  <wpt lat="{lat}" lon="{lon}">')
        lines.append(f"    <name>{escape(_member_label(m))}</name>")
        desc = _member_desc(m)
        if desc:
            lines.append(f"    <desc>{escape(desc)}</desc>")
        lines.append(f"    <type>{escape(m.get('role') or 'primary')}</type>")
        lines.append("  </wpt>")
    lines.append("</gpx>")
    return "\n".join(lines) + "\n"


def trip_to_kml(trip: dict) -> str:
    """The trip as a KML 2.2 document (one <Placemark> per member)."""
    name = escape((trip.get("name") or "Trip").strip())
    date_range = _date_range(trip)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "  <Document>",
        f"    <name>{name}</name>",
    ]
    if date_range:
        lines.append(f"    <description>{escape(date_range)}</description>")
    for m in _members(trip):
        lat = f"{float(m['lat']):.6f}"
        lon = f"{float(m['lon']):.6f}"
        lines.append("    <Placemark>")
        lines.append(f"      <name>{escape(_member_label(m))}</name>")
        desc = _member_desc(m)
        if desc:
            lines.append(f"      <description>{escape(desc)}</description>")
        # KML coordinates are lon,lat[,alt].
        lines.append(f"      <Point><coordinates>{lon},{lat},0</coordinates></Point>")
        lines.append("    </Placemark>")
    lines.append("  </Document>")
    lines.append("</kml>")
    return "\n".join(lines) + "\n"
