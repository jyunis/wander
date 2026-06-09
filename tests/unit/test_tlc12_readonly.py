"""TLC-12: Cairn's outbound reach is read-only GETs, no auth, no writes.

A structural test over the client modules: the courtesy layer exposes only a
GET helper, and no client source issues a write verb or sends an Authorization
header to an external host.
"""

from __future__ import annotations

import inspect

import cairn.clients.http as http_mod
import cairn.clients.overpass as overpass_mod
import cairn.clients.terrain as terrain_mod
from cairn.clients.http import CourtesyHttp


def test_courtesy_http_exposes_no_write_verbs():
    public = {n for n in dir(CourtesyHttp) if not n.startswith("_")}
    assert "get" in public
    for verb in ("post", "put", "patch", "delete"):
        assert verb not in public


def test_client_sources_have_no_writes_or_auth():
    for module in (http_mod, overpass_mod, terrain_mod):
        src = inspect.getsource(module).lower()
        # The Authorization header is set as a dict key — match the literal,
        # not the word in prose/docstrings.
        assert '"authorization"' not in src
        assert "'authorization'" not in src
        # HTTP write verbs only (disk DiskCache.put is a legitimate cache write).
        for verb in ("client.post(", "client.put(", "client.patch(", "client.delete("):
            assert verb not in src, f"{module.__name__} issues {verb}"
        for verb in ("httpx.post", "httpx.put", "httpx.patch", "httpx.delete"):
            assert verb not in src, f"{module.__name__} issues {verb}"
