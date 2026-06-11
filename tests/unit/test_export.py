"""GPX/KML trip export (TLC-3 — user data + display). Asserts well-formed XML,
one waypoint per member, lon/lat ordering per format, escaping, and the slug."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from cairn.export import trip_slug, trip_to_gpx, trip_to_kml

GPX_NS = "{http://www.topografix.com/GPX/1/1}"
KML_NS = "{http://www.opengis.net/kml/2.2}"


def _trip() -> dict:
    return {
        "name": "Labor Day <weekend>",
        "start_date": "2026-09-05",
        "end_date": "2026-09-07",
        "members": [
            {
                "saved_id": 1, "role": "primary", "kind": "site", "name": "Quiet spur",
                "lat": 47.5, "lon": -94.2, "payload": {"total": 0.78},
                "rating": 4, "note": "lakeside",
            },
            {
                "saved_id": 2, "role": "fallback", "kind": "campground", "name": "Deer Lake",
                "lat": 47.6, "lon": -94.0, "payload": None, "rating": None, "note": None,
            },
        ],
    }


def test_gpx_is_wellformed_with_one_wpt_per_member():
    root = ET.fromstring(trip_to_gpx(_trip()))
    wpts = root.findall(f"{GPX_NS}wpt")
    assert len(wpts) == 2
    assert wpts[0].get("lat") == "47.500000" and wpts[0].get("lon") == "-94.200000"
    assert wpts[0].find(f"{GPX_NS}name").text == "Quiet spur"
    assert wpts[0].find(f"{GPX_NS}type").text == "primary"
    # The description packs role · kind · stars · score · note for the field.
    desc = wpts[0].find(f"{GPX_NS}desc").text
    assert "Primary" in desc and "site" in desc
    assert "★★★★" in desc and "0.78" in desc and "lakeside" in desc


def test_kml_is_wellformed_with_lonlat_coordinates():
    root = ET.fromstring(trip_to_kml(_trip()))
    placemarks = root.findall(f".//{KML_NS}Placemark")
    coords = [p.find(f"{KML_NS}Point/{KML_NS}coordinates").text for p in placemarks]
    assert coords == ["-94.200000,47.500000,0", "-94.000000,47.600000,0"]


def test_special_chars_escaped_and_doc_still_parses():
    root = ET.fromstring(trip_to_gpx(_trip()))
    assert root.find(f"{GPX_NS}metadata/{GPX_NS}name").text == "Labor Day <weekend>"


def test_empty_trip_exports_valid_doc():
    t = {"name": "Empty", "members": []}
    assert ET.fromstring(trip_to_gpx(t)).findall(f"{GPX_NS}wpt") == []
    assert ET.fromstring(trip_to_kml(t)).findall(f".//{KML_NS}Placemark") == []


def test_slug():
    assert trip_slug({"name": "Labor Day weekend!"}) == "labor-day-weekend"
    assert trip_slug({"name": "   "}) == "trip"
    assert trip_slug({}) == "trip"
