# Cairn evaluation set

Ground-truth dispersed campsites for measuring how well Cairn ranks **real**
sites — and, importantly, for keeping it honest about scope.

## `ground_truth_chippewa.json`

76 designated/dispersed sites in Chippewa National Forest, parsed from
[minnesotacampguide.com](https://www.minnesotacampguide.com/free-car-camping-chippewa-national-forest-designated-dispersed),
each with `lat`/`lon`, a `stars` quality rating, `access`, road tags, and a
descriptive `blurb` (the source write-up — also used as the note when these are
seeded as saved favorites).

**Access matters.** A towed teardrop can only reach `access == "DRIVE"` sites.
Of the 76, only **25 are drive-in** — the other 51 are paddle-in / hike-in /
gated / feral and are out of scope for Cairn (it scores road-anchored
candidates). Validation uses the drive-in subset only.

**Key finding:** 18 of the 25 drive-in sites are on named **Forest Roads**
(`highway=track`, "Forest Road ####"), only 3 on county roads/highways — strong
evidence that *road character* (forest-road class + network remoteness) is the
dominant signal, which v1 scoring does not capture.

## `validate.py`

Scores a bbox via the running API and reports where the known-good drive-in
sites land in the ranking (rank percentile; lower = better). Run it before and
after a scoring change to measure the lift.

    python3 eval/validate.py --bbox -94.20,47.55,-94.12,47.62

### Baseline (v1 scoring, 2026-06-10)
The 5★ forest-road site (D55 Middle Pigeon) ranked **223/262 (bottom 15%)**;
the top-ranked candidates were generic flat, near-water, max-solitude points.
The ranking was essentially uncorrelated with site quality — the motivation for
the road-character feature work.
