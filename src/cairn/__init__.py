"""Cairn — a campsite-discovery scorer for a teardrop camper.

`score_view(bbox, weights) -> ranked, drive-to-able dispersed-campsite
candidates`, via MCDA over OSM (Overpass) + AWS Terrarium elevation tiles.

Cairn scores PHYSICAL SUITABILITY ONLY — it never asserts that a site is
legal to camp (TLC-3). See the Architecture / Contracts design docs.
"""

__version__ = "0.1.0"

# The single disclaimer string of record (TLC-3). Imported by the API,
# GeoJSON serializer, and surfaced in the UI. Do not fork copies of this.
DISCLAIMER = (
    "Physical suitability only — Wander does not verify land ownership or "
    "camping legality. Confirm you are allowed to camp here before you go."
)
