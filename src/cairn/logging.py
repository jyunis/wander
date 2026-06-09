"""Logging setup — a single stream handler at the configured level.

Kept deliberately tiny; structured logging is not a v1 requirement.
"""

from __future__ import annotations

import logging
import sys


def configure(level: str = "INFO") -> None:
    """Configure root logging once, idempotently."""
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level.upper())
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level.upper())
