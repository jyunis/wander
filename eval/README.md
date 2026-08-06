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

## `ground_truth_field.json`

Sites **visited in person**, marked `✓` in the report. Same core schema as the
scraped set so the two concatenate — including `stars` as a *string*, which
`validate.py` sorts on.

These are worth more than a scraped star rating: someone stood there, and each
carries two things the guide data cannot:

- **`measured`** — the physical facts at the point (`dist_water_m`,
  `dist_terminus_m`, `road_score`, `slope_deg`, …). Weight-independent, so they
  stay meaningful no matter how the scorer is retuned later.
- **`deduction`** — *why* a site fell short of 5★. This is where **missing
  features** surface, as opposed to mis-weighted ones. A site that scores 0.97
  and still loses a star is telling you about something the model cannot see.

`scored_as` snapshots the rank/total/weights at the time of discovery. Record it:
a ground-truth point is uninterpretable later without knowing which weights
produced the ranking, and these were **not** the shipped defaults.

### F1 — Bayfield forest near pond (2026-07-24)

First field-verified site, and the first from a Wander-planned trip. Bayfield
**County** Forest, WI — county land, not USFS (Cairn's own ownership overlay
agrees: `category=County`). Confirms the June retuning *for the right reasons*:
it ranked **#2 of the run at 0.970**, with all three high-weight features maxed —
`water_proximity` 1.0 (28 m from the pond), `road_end` 1.0 (`dist_terminus_m=0`,
literally the spur end), `road_character` 1.0 (forest road). Not a pile of weak
signals; the exact three the retuning bet on.

Rated 4★ for a "narrow uphill approach with a trailer". Whether that is a
scoreable gap is **open** (see `open_question`): the approach is mapped — a 546 m
`highway=track` spur ending at the candidate — but the pitch may have been
further up, on unmapped ground built out for hunters. `rough: false` proves
nothing either way; OSM has no surface tag on that way.

`coords_are` / `measured_of` exist because a Wander-derived entry's coordinates
are the **candidate**, not necessarily the pitch. Scraped entries give the site's
own location; if the two differ, `measured` describes the road end while `stars`
describes the campsite.

    python3 eval/validate.py --bbox=-91.56,46.49,-91.48,46.55

**One point cannot retune weights.** It confirms. Weights move when several
field-verified sites disagree with the ranking in the same direction.
