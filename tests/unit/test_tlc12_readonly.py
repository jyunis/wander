"""TLC-12: Cairn's outbound reach is read-only, no auth, no state mutation.

A structural test over the client modules: the courtesy layer exposes only read
helpers (GET, plus a `post_json` for the handful of public APIs whose *query* is
a POST — e.g. the UseDirect availability grid), and no client source issues a
state-mutating verb (PUT/PATCH/DELETE) or sends an Authorization header.
"""

from __future__ import annotations

import inspect

import cairn.clients.http as http_mod
import cairn.clients.overpass as overpass_mod
import cairn.clients.terrain as terrain_mod
from cairn.clients.http import CourtesyHttp


def test_courtesy_http_exposes_no_mutating_verbs():
    public = {n for n in dir(CourtesyHttp) if not n.startswith("_")}
    assert "get" in public
    # A read-only POST query helper is allowed; bare write verbs are not.
    for verb in ("post", "put", "patch", "delete"):
        assert verb not in public


def test_client_sources_have_no_writes_or_auth():
    for module in (http_mod, overpass_mod, terrain_mod):
        src = inspect.getsource(module).lower()
        # The Authorization header is set as a dict key — match the literal,
        # not the word in prose/docstrings.
        assert '"authorization"' not in src
        assert "'authorization'" not in src
        # State-MUTATING verbs only. POST is permitted (a read query for APIs
        # that require it); PUT/PATCH/DELETE are never legitimate for Cairn.
        # (DiskCache.put is a local disk write, not an HTTP verb.)
        for verb in ("client.put(", "client.patch(", "client.delete("):
            assert verb not in src, f"{module.__name__} issues {verb}"
        for verb in ("httpx.put", "httpx.patch", "httpx.delete"):
            assert verb not in src, f"{module.__name__} issues {verb}"
