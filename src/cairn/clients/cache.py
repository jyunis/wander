"""Disposable, key-addressed disk cache (FC-10).

Tiles and raw Overpass JSON are cached on disk with TTL freshness. The cache
is derivable and disposable (TLC-7): deleting it only forces a refetch and
never alters a committed run. Keys are stable strings; the cache exposes the
key it used so callers can record it for provenance (TLC-2).

Writes are atomic (temp file + rename) so a concurrent reader never sees a
half-written entry.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path


class DiskCache:
    """A flat content-addressed directory cache with a single TTL."""

    def __init__(self, directory: Path, ttl_s: int, suffix: str = "") -> None:
        self._dir = directory
        self._ttl_s = ttl_s
        self._suffix = suffix
        self._dir.mkdir(parents=True, exist_ok=True)

    def key_to_name(self, key: str) -> str:
        """The opaque on-disk filename for a logical cache key."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"{digest}{self._suffix}"

    def path_for(self, key: str) -> Path:
        return self._dir / self.key_to_name(key)

    def get(self, key: str) -> bytes | None:
        """Return cached bytes if present and still fresh, else None."""
        path = self.path_for(key)
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        if self._ttl_s >= 0 and (time.time() - st.st_mtime) > self._ttl_s:
            return None  # stale; treat as miss (left for eventual sweep)
        try:
            return path.read_bytes()
        except FileNotFoundError:  # pragma: no cover - raced deletion
            return None

    def put(self, key: str, data: bytes) -> None:
        path = self.path_for(key)
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # atomic on POSIX

    def has_fresh(self, key: str) -> bool:
        return self.get(key) is not None

    def mtime(self, key: str) -> float | None:
        """Unix mtime of the cached entry (when its data was fetched), or None."""
        try:
            return self.path_for(key).stat().st_mtime
        except FileNotFoundError:
            return None
