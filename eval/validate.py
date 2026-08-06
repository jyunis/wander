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
# Sites visited in person (eval/README.md). Same core schema as the scraped set,
# so the two concatenate — note `stars` must stay a STRING in both, since the
# report sorts on it and a bare int would blow up the comparison.
GT_FIELD = Path(__file__).parent / "ground_truth_field.json"

# Known-good drive-in sites not in the guide list (add your own here).
EXTRA: list[dict] = []


def load_ground_truth():
    """Scraped guide sites + field-verified sites + ad-hoc examples.

    Field-verified entries are tagged so the report can mark them: they are worth
    more than a scraped star rating (someone stood there), and they are the only
    ones carrying a `deduction` — the reason a site fell short of 5 stars, which
    is where missing features show up.
    """
    sites = json.load(open(GT)) + EXTRA
    if GT_FIELD.exists():
        for s in json.load(open(GT_FIELD)):
            sites.append({**s, "field_verified": True})
    return sites


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

    gt = load_ground_truth()
    known = [s for s in gt if s["access"] == "DRIVE" and in_bbox(s, bbox)]
    n_field = sum(1 for s in known if s.get("field_verified"))
    print(f"bbox {bbox}: {len(known)} known-good DRIVE-IN sites in scope"
          + (f" ({n_field} field-verified)" if n_field else ""))

    body = {"bbox": bbox}
    if args.weights:
        body["weights"] = json.loads(args.weights)
    # Scoring went synchronous + ephemeral (TLC-15): POST /score/view returns the
    # ranked GeoJSON directly. The old async POST /score + poll /runs/{id} path is
    # gone, which left this harness 405-ing.
    gj = post_json("/score/view", body)
    feats = gj["features"]
    n = len(feats)
    print(f"{gj['run']['candidate_count']} scored, "
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
        tag = "✓" if s.get("field_verified") else " "  # ✓ = someone stood there
        if not near:
            print(f" {tag}{s['stars']:>2}* {s['code']:<5} {s['name'][:22]:<22} "
                  f"no candidate within {MATCH_M:.0f} m")
            continue
        best = min(near, key=lambda f: f["properties"]["rank"])
        p = best["properties"]
        pct = 100 * p["rank"] / n
        pcts.append(pct)
        print(f" {tag}{s['stars']:>2}* {s['code']:<5} {s['name'][:22]:<22} "
              f"best-within-{MATCH_M:.0f}m: rank {p['rank']:>4}/{n} (top {pct:4.1f}%) "
              f"road={p.get('road_score')} remote_km={(p.get('local_road_m') or 0)/1000:.1f} "
              f"distinct={p['distinct']}")
        if s.get("deduction"):
            print(f"        └─ lost a star to: {s['deduction']}")
    pcts.sort()
    med = pcts[len(pcts) // 2] if pcts else float("nan")
    print(f"\nMETRIC  median rank-percentile of known-good sites: {med:.1f}% "
          f"(lower = better)   in-top-25: "
          f"{sum(1 for p in pcts if p <= 100*25/n)}/{len(pcts)}")


if __name__ == "__main__":
    main()
