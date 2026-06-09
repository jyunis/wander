"""Disk cache: hit/miss, TTL staleness, atomic overwrite (FC-10)."""

from __future__ import annotations

import os
import time

from cairn.clients.cache import DiskCache


def test_put_then_get_roundtrips(tmp_path):
    c = DiskCache(tmp_path, ttl_s=3600)
    assert c.get("k") is None
    c.put("k", b"hello")
    assert c.get("k") == b"hello"
    assert c.has_fresh("k") is True


def test_ttl_expiry_is_a_miss(tmp_path):
    c = DiskCache(tmp_path, ttl_s=1)
    c.put("k", b"v")
    # Backdate the file mtime well past the TTL.
    old = time.time() - 10
    os.utime(c.path_for("k"), (old, old))
    assert c.get("k") is None


def test_overwrite_is_atomic_and_leaves_no_tmp(tmp_path):
    c = DiskCache(tmp_path, ttl_s=3600)
    c.put("k", b"one")
    c.put("k", b"two")
    assert c.get("k") == b"two"
    leftover = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftover == []


def test_distinct_keys_distinct_entries(tmp_path):
    c = DiskCache(tmp_path, ttl_s=3600)
    c.put("a", b"1")
    c.put("b", b"2")
    assert c.get("a") == b"1"
    assert c.get("b") == b"2"
    assert c.mtime("a") is not None
    assert c.mtime("missing") is None
