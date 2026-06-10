"""Validation harness: how well does Cairn rank known-good DRIVE-IN dispersed
sites? Scores a bbox via the API and reports where ground-truth sites land.

Ground truth: eval/ground_truth_chippewa.json (parsed from minnesotacampguide).
Only access=="DRIVE" sites are in scope (a towed teardrop can't reach paddle/
hike sites). Use this before/after a scoring change to measure the lift.

Usage (inside the box, container running on :9931):
    python3 eval/validate.py --bbox -94.20,47.55,-94.12,47.62
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request
from pathlib import Path

API = "http://localhost:9931"
GT = Path(__file__).parent / "ground_truth_chippewa.json"

# Known-good drive-in sites not in the guide list (add your own here).
EXTRA: list[dict] = []


def meters(lat1, lon1, lat2, lon2):
    ml = math.radians((lat1 + lat2) / 2)
    return math.hypot((lon2 - lon1) * 111320 * math.cos(ml), (lat2 - lat1) * 111320)


def post_json(path, body):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode(),
        headers={"content-type": "application/json"})
    return json.load(urllib.request.urlopen(req))


def get_json(path):
    return json.load(urllib.request.urlopen(API + path))


def in_bbox(s, b):
    return b[0] <= s["lon"] <= b[2] and b[1] <= s["lat"] <= b[3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default="-94.20,47.55,-94.12,47.62",
                    help="min_lon,min_lat,max_lon,max_lat")
    ap.add_argument("--weights", default=None, help="JSON weights, optional")
    args = ap.parse_args()
    bbox = [float(x) for x in args.bbox.split(",")]

    gt = json.load(open(GT)) + EXTRA
    known = [s for s in gt if s["access"] == "DRIVE" and in_bbox(s, bbox)]
    print(f"bbox {bbox}: {len(known)} known-good DRIVE-IN sites in scope")

    body = {"bbox": bbox}
    if args.weights:
        body["weights"] = json.loads(args.weights)
    run = post_json("/score", body)
    rid = run["id"]
    while run["status"] in ("pending", "running"):
        time.sleep(1)
        run = get_json(f"/runs/{rid}")
    gj = get_json(f"/runs/{rid}/geojson")
    feats = gj["features"]
    n = len(feats)
    print(f"run {rid}: {gj['run']['candidate_count']} scored, "
          f"{gj['run']['distinct_count']} distinct sites\n")

    # Match each known site to the BEST-RANKED candidate within MATCH_M — the
    # right question is "did Cairn rank *some* candidate near this site highly",
    # not "what's the geometrically nearest point" (which may be on a different,
    # worse road that happens to pass closer).
    MATCH_M = 150.0
    pcts = []
    for s in sorted(known, key=lambda x: (x["stars"] == "?", x["stars"]), reverse=True):
        near = [
            f for f in feats
            if meters(s["lat"], s["lon"], f["geometry"]["coordinates"][1],
                      f["geometry"]["coordinates"][0]) <= MATCH_M
        ]
        if not near:
            print(f"  {s['stars']:>2}* {s['code']:<5} {s['name'][:22]:<22} "
                  f"no candidate within {MATCH_M:.0f} m")
            continue
        best = min(near, key=lambda f: f["properties"]["rank"])
        p = best["properties"]
        pct = 100 * p["rank"] / n
        pcts.append(pct)
        print(f"  {s['stars']:>2}* {s['code']:<5} {s['name'][:22]:<22} "
              f"best-within-{MATCH_M:.0f}m: rank {p['rank']:>4}/{n} (top {pct:4.1f}%) "
              f"road={p.get('road_score')} remote_km={(p.get('local_road_m') or 0)/1000:.1f} "
              f"distinct={p['distinct']}")
    pcts.sort()
    med = pcts[len(pcts) // 2] if pcts else float("nan")
    print(f"\nMETRIC  median rank-percentile of known-good sites: {med:.1f}% "
          f"(lower = better)   in-top-25: "
          f"{sum(1 for p in pcts if p <= 100*25/n)}/{len(pcts)}")


if __name__ == "__main__":
    main()
