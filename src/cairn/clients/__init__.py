"""Clients — all outbound I/O lives here (TLC-5, TLC-12).

`overpass.py` (OSM fetch + cache), `terrain.py` (Terrarium fetch/decode +
bounded LRU slope sampler), `cache.py` (disposable disk cache). Read-only GETs
only; descriptive User-Agent; cache-first with backoff. Added in milestone 2.
"""
