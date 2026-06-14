"""GeocodeClient.reverse_label — assembles a "Locality, County, ST" run label
from a Nominatim reverse-geocode address (mocked HTTP — no live network)."""

from __future__ import annotations

from cairn.clients.nominatim import GeocodeClient
from cairn.config import Settings


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._p = payload

    def json(self):
        return self._p


class _Http:
    def __init__(self, payload):
        self.payload = payload

    async def get(self, url, *, params=None, timeout=15.0):
        return _Resp(self.payload)

    async def aclose(self):
        pass


def _client(payload):
    c = GeocodeClient(Settings(_env_file=None))
    c._http = _Http(payload)
    return c


async def test_label_locality_county_state():
    c = _client({"address": {"city": "Grand Rapids", "county": "Itasca County",
                             "state": "Minnesota", "ISO3166-2-lvl4": "US-MN"}})
    assert await c.reverse_label(47.2, -93.5) == "Grand Rapids, Itasca County, MN"


async def test_label_backcountry_county_state_only():
    # No town out in the forest -> county + state code.
    c = _client({"address": {"county": "Cook County", "ISO3166-2-lvl4": "US-MN"}})
    assert await c.reverse_label(47.9, -90.5) == "Cook County, MN"


async def test_label_falls_back_to_state_name_without_iso():
    c = _client({"address": {"village": "Ely", "state": "Minnesota"}})
    assert await c.reverse_label(47.9, -91.8) == "Ely, Minnesota"


async def test_label_empty_address_uses_display_name():
    c = _client({"address": {}, "display_name": "Somewhere, USA"})
    assert await c.reverse_label(0, 0) == "Somewhere, USA"
