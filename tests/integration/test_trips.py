"""Trip planner store layer (v8) — CRUD, hydrated members, role upsert, and the
FK cascade that keeps trip_members in sync when a trip or saved place is deleted.
Uses the shared `store` fixture (real SpatiaLite store on a temp DB)."""

from __future__ import annotations


async def test_trip_crud_and_member_count(store):
    t = await store.add_trip("BWCA", "2026-08-01", "2026-08-03", "north shore")
    assert t["id"] and t["name"] == "BWCA" and t["created_at"]

    trips = await store.list_trips()
    assert any(x["id"] == t["id"] and x["member_count"] == 0 for x in trips)

    got = await store.get_trip(t["id"])
    assert got["members"] == [] and got["start_date"] == "2026-08-01"
    assert got["note"] == "north shore"
    assert await store.get_trip(999999) is None

    upd = await store.update_trip(t["id"], name="BWCAW")
    assert upd["name"] == "BWCAW" and upd["start_date"] == "2026-08-01"  # date preserved

    assert await store.delete_trip(t["id"]) is True
    assert await store.get_trip(t["id"]) is None
    assert await store.delete_trip(t["id"]) is False


async def test_members_hydrate_and_upsert_role(store):
    trip = await store.add_trip("Trip")
    s1 = await store.add_saved("site", "Spur", 47.5, -94.2, {"total": 0.7})
    s2 = await store.add_saved("campground", "Deer Lake", 47.6, -94.0, {"category": "Federal"})

    assert await store.add_member(trip["id"], s1["id"], "primary") is not None
    hydrated = await store.add_member(trip["id"], s2["id"], "fallback")
    members = hydrated["members"]
    assert len(members) == 2
    # Primary first; each member carries its coords/name/payload from saved_places.
    assert members[0]["role"] == "primary" and members[0]["lat"] == 47.5
    assert members[0]["name"] == "Spur" and members[0]["payload"]["total"] == 0.7
    assert members[1]["role"] == "fallback" and members[1]["kind"] == "campground"

    counts = {x["id"]: x["member_count"] for x in await store.list_trips()}
    assert counts[trip["id"]] == 2

    # Re-adding the same saved place upserts its role (no duplicate row).
    flipped = await store.add_member(trip["id"], s1["id"], "fallback")
    assert len(flipped["members"]) == 2
    assert {m["saved_id"]: m["role"] for m in flipped["members"]}[s1["id"]] == "fallback"


async def test_add_member_missing_trip_or_saved_returns_none(store):
    trip = await store.add_trip("T")
    s = await store.add_saved("site", "X", 1.0, 2.0)
    assert await store.add_member(999999, s["id"]) is None
    assert await store.add_member(trip["id"], 999999) is None


async def test_remove_member(store):
    trip = await store.add_trip("T")
    s = await store.add_saved("site", "X", 1.0, 2.0)
    await store.add_member(trip["id"], s["id"])
    assert await store.remove_member(trip["id"], s["id"]) is True
    assert (await store.get_trip(trip["id"]))["members"] == []
    assert await store.remove_member(trip["id"], s["id"]) is False


async def test_delete_trip_cascades_members(store):
    trip = await store.add_trip("T")
    s = await store.add_saved("site", "X", 1.0, 2.0)
    await store.add_member(trip["id"], s["id"])
    await store.delete_trip(trip["id"])
    # The saved place survives; the membership row is gone (FK cascade).
    assert any(x["id"] == s["id"] for x in await store.list_saved())
    cur = await store.db.execute(
        "SELECT COUNT(*) FROM trip_members WHERE trip_id=?", (trip["id"],)
    )
    assert (await cur.fetchone())[0] == 0


async def test_delete_saved_prunes_membership(store):
    trip = await store.add_trip("T")
    s = await store.add_saved("site", "X", 1.0, 2.0)
    await store.add_member(trip["id"], s["id"])
    await store.delete_saved(s["id"])
    assert (await store.get_trip(trip["id"]))["members"] == []
