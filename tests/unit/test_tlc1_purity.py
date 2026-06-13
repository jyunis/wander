"""TLC-1 purity lint: a static source check that the scoring/feature modules use
no nondeterminism primitives. The stub scoring.py is pure, so this passes today
and guards the implementer against introducing randomness, clocks, or PID reads.

We match on actual usage patterns (imports / calls), not bare words, so prose in
docstrings ("no clock, no randomness") does not trip the check.
"""

from __future__ import annotations

import re
from pathlib import Path

import cairn.engine.scoring as scoring_mod

ENGINE_DIR = Path(scoring_mod.__file__).parent

# Modules to lint: scoring.py always; features.py only if it exists yet.
MODULES = ["scoring.py", "features.py"]

# (human label, compiled usage pattern) — usage, not the word in prose.
FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("import random", re.compile(r"^\s*import\s+random\b", re.MULTILINE)),
    ("from random import", re.compile(r"^\s*from\s+random\s+import\b", re.MULTILINE)),
    ("random. call", re.compile(r"\brandom\.\w+\s*\(")),
    ("import uuid", re.compile(r"^\s*import\s+uuid\b", re.MULTILINE)),
    ("from uuid import", re.compile(r"^\s*from\s+uuid\s+import\b", re.MULTILINE)),
    ("uuid. call", re.compile(r"\buuid\.\w+\s*\(")),
    ("import time", re.compile(r"^\s*import\s+time\b", re.MULTILINE)),
    ("from time import", re.compile(r"^\s*from\s+time\s+import\b", re.MULTILINE)),
    ("time. call", re.compile(r"\btime\.\w+\s*\(")),
    ("from datetime import", re.compile(r"^\s*from\s+datetime\s+import\b", re.MULTILINE)),
    ("datetime.now(", re.compile(r"\bdatetime\.now\s*\(")),
    ("datetime.today(", re.compile(r"\bdatetime\.today\s*\(")),
    ("os.getpid(", re.compile(r"\bos\.getpid\s*\(")),
    ("Math.random", re.compile(r"\bMath\.random\b")),
]


def _strip_comments(source: str) -> str:
    """Drop full-line comments so a `# uses time.time` note can't false-positive.

    (String/docstring content is left in place; the usage patterns above are
    call/import shaped, so prose like "no clock" still won't match.)
    """
    lines = []
    for line in source.splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def test_scoring_module_exists_and_is_linted():
    assert (ENGINE_DIR / "scoring.py").is_file()


def test_engine_never_references_overlay_data_tlc3():
    # TLC-3: the scoring engine must not read the display-only overlay /
    # campground data (ownership, boundaries, campgrounds). Those are context,
    # never a scoring input — enforced here so a future change can't quietly
    # wire campground proximity (Phase 3) or ownership into scoring.
    forbidden = re.compile(r"store\.overlays|OverlayStore|overlay_points|nearest_campground")
    for path in ENGINE_DIR.glob("*.py"):
        source = _strip_comments(path.read_text(encoding="utf-8"))
        assert forbidden.search(source) is None, (
            f"{path.name} references display-only overlay data (TLC-3 violation)"
        )


def test_engine_never_references_trip_or_export_data_tlc3():
    # TLC-3: trips + GPX/KML export are user data + display, never scoring
    # inputs. Guard the engine against quietly importing the trip store or the
    # export module. Match code-shaped tokens only (not the bare word "trips",
    # which could appear in prose) so this can't false-positive.
    forbidden = re.compile(
        r"trip_members|add_trip|get_trip|add_member|cairn\.export|trip_to_gpx|trip_to_kml"
    )
    for path in ENGINE_DIR.glob("*.py"):
        source = _strip_comments(path.read_text(encoding="utf-8"))
        assert forbidden.search(source) is None, (
            f"{path.name} references trip/export data (TLC-3 violation)"
        )


def test_engine_never_references_collections_data_tlc3():
    # TLC-3: collections (multi-membership tags over saved places) are user data
    # + display/organization — never a scoring input. Guard the engine against
    # importing the collections store surface. Code-shaped tokens only.
    forbidden = re.compile(
        r"place_collections|add_collection|list_collections|set_place_collections|"
        r"add_places_to_collection|delete_collection"
    )
    for path in ENGINE_DIR.glob("*.py"):
        source = _strip_comments(path.read_text(encoding="utf-8"))
        assert forbidden.search(source) is None, (
            f"{path.name} references collections data (TLC-3 violation)"
        )


def test_engine_never_references_availability_data_tlc3():
    # TLC-3: live campground availability (Phase 2) is display-only — "is this
    # campground open on my dates," never a scoring input. Guard the engine
    # against importing the availability client or trip-availability service so
    # a future change can't wire open/closed counts into scoring. Code-shaped
    # tokens only (not the bare word "availability", which appears in prose).
    forbidden = re.compile(
        r"cairn\.availability|RecreationGovClient|campground_month|"
        r"trip_availability|count_open_all_nights|federal_facility_id|"
        r"UseDirectClient|facility_grid|usedirect_facility_ids"
    )
    for path in ENGINE_DIR.glob("*.py"):
        source = _strip_comments(path.read_text(encoding="utf-8"))
        assert forbidden.search(source) is None, (
            f"{path.name} references display-only availability data (TLC-3 violation)"
        )


def test_no_nondeterminism_primitives_in_engine_sources():
    checked_any = False
    for name in MODULES:
        path = ENGINE_DIR / name
        if not path.is_file():
            continue  # features.py may not exist yet — guarded.
        checked_any = True
        source = _strip_comments(path.read_text(encoding="utf-8"))
        for label, pattern in FORBIDDEN_PATTERNS:
            assert pattern.search(source) is None, (
                f"{name} uses forbidden nondeterminism primitive: {label}"
            )
    assert checked_any, "expected at least scoring.py to be present"
