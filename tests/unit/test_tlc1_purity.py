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
