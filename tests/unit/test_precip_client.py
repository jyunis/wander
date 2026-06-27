"""Pure-summary tests for the recent-precip client (cairn.clients.precip): the
window sum, decay weighting, wetness label, season note, and the full
`_summarize` over Open-Meteo's daily arrays. No clock, no network — the summary
is a pure function of the daily precip array (the only "today" is the last date
Open-Meteo returned).
"""

from __future__ import annotations

from cairn.clients import precip as p


def test_window_sum_last_three_days():
    vals = [0.0, 1.0, 0.0, 0.2, 0.3, 0.5]  # most-recent last
    assert p._window_sum(vals, 3) == 1.0  # 0.2 + 0.3 + 0.5
    assert p._window_sum([0.4], 3) == 0.4  # fewer than the window
    assert p._window_sum([], 3) == 0.0


def test_decay_weighted_emphasizes_recent():
    # Same inch of rain weighs more the more recent it is (age 0 = last).
    today = p._decay_weighted([0.0, 0.0, 1.0])
    week_ago = p._decay_weighted([1.0, 0.0, 0.0])
    assert today == 1.0  # last day, weight 1
    assert week_ago < today
    assert abs(week_ago - 0.55 ** 2) < 1e-9
    assert p._decay_weighted([]) == 0.0


def test_wetness_label_bands():
    assert p._wetness_label(0.0) == "dry"
    assert p._wetness_label(0.09) == "dry"
    assert p._wetness_label(0.10) == "damp"
    assert p._wetness_label(0.49) == "damp"
    assert p._wetness_label(0.50) == "wet"
    assert p._wetness_label(1.19) == "wet"
    assert p._wetness_label(1.20) == "saturated"
    assert p._wetness_label(5.0) == "saturated"


def test_season_note_by_month():
    assert "frost-out" in p._season_note(4)
    assert "frost-out" in p._season_note(5)
    assert "summer" in p._season_note(7)
    assert "fall" in p._season_note(10)
    assert "frozen" in p._season_note(1)
    assert "frozen" in p._season_note(12)
    assert p._season_note(0) == ""  # unknown month -> no note


def test_summarize_full():
    dates = [
        "2026-06-21", "2026-06-22", "2026-06-23", "2026-06-24",
        "2026-06-25", "2026-06-26", "2026-06-27",
    ]
    precip = [0.0, 0.0, 0.5, 0.0, 0.0, 0.0, 0.2]  # most-recent last
    s = p._summarize(47.4, -94.2, dates, precip)
    assert s["lat"] == 47.4 and s["lon"] == -94.2
    assert s["as_of"] == "2026-06-27"
    assert s["window_h"] == 72
    assert s["recent_in"] == 0.2  # last 3 days: 0 + 0 + 0.2
    assert s["label"] in {"dry", "damp", "wet", "saturated"}
    assert "summer" in s["season_note"]  # June
    assert s["source"] == "open-meteo"
    # daily carries the recent window, each {date, in}.
    assert s["daily"][-1] == {"date": "2026-06-27", "in": 0.2}
    assert all(set(d) == {"date", "in"} for d in s["daily"])


def test_summarize_treats_nulls_as_zero():
    dates = ["2026-04-10", "2026-04-11", "2026-04-12"]
    precip = [None, 1.0, None]  # Open-Meteo can return null for a missing day
    s = p._summarize(46.0, -92.0, dates, precip)
    assert s["recent_in"] == 1.0
    assert "frost-out" in s["season_note"]  # April
    assert s["daily"][0]["in"] == 0.0


def test_summarize_empty_dates_is_safe():
    s = p._summarize(46.0, -92.0, [], [])
    assert s["as_of"] is None
    assert s["recent_in"] == 0.0
    assert s["label"] == "dry"
    assert s["season_note"] == ""
