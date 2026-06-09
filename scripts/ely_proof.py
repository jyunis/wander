"""Milestone-3 vertical-slice proof: a real run over a small Ely, MN bbox.

Not a test; a manual smoke driver. Run inside the image with ./data mounted.
"""

import asyncio

from cairn.clients.overpass import OverpassClient
from cairn.config import DEFAULT_WEIGHTS, Settings
from cairn.service import RunService
from cairn.store import Store
from cairn.store.types import Bbox


async def main() -> None:
    s = Settings(_env_file=None, data_dir="/data")
    store = await Store.open(s)
    ov = OverpassClient(s)
    svc = RunService(store, s, overpass=ov)
    bbox = Bbox(-91.88, 47.90, -91.84, 47.93)  # ~12 km^2 near Ely (cached from M2)
    run = await svc.score_bbox(bbox, DEFAULT_WEIGHTS)

    print(f"RUN {run.id}: status={run.status} candidates={run.candidate_count} capped={run.capped}")
    print(f"  failure={run.failure_reason}")
    print(f"  degraded={run.degraded_features}")
    if run.provenance:
        print(f"  osm class_counts={run.provenance['osm']['class_counts']}")
        print(f"  elevation={run.provenance['elevation']}")

    gj = await store.get_run_geojson(run.id, top_n=5)
    print(f"  disclaimer present: {'physical suitability only' in gj['disclaimer'].lower()}")
    print("  TOP 5 candidates:")
    for f in gj["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        contribs = {c["feature"]: round(c["contribution"], 3) for c in p["contributions"]}
        print(
            f"    #{p['rank']} total={p['total']:.3f} @({lat:.4f},{lon:.4f}) "
            f"slope={p['slope_deg']:.1f} water={p['dist_water_m']:.0f}m "
            f"forest={p['dist_forest_m']:.0f}m solitude={p['dist_solitude_m']:.0f}m "
            f"near_campsite={p['near_campsite']} {contribs}"
        )

    await ov.aclose()
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
