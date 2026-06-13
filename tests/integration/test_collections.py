"""Collections store layer (v9) — multi-membership tags over saved places.

CRUD + member counts, the PUT-set and bulk-add membership ops, GET-saved
hydration, and the FK cascades (deleting a collection or a saved place prunes
the join rows but leaves the other side intact). Uses the shared `store`
fixture (real SpatiaLite store on a temp DB). Collections are user data +
display, never scoring (TLC-3)."""

from __future__ import annotations


async def test_collection_crud_and_count(store):
    c = await store.add_collection("Scouted", "#5b9c5a")
    assert c["id"] and c["name"] == "Scouted" and c["color"] == "#5b9c5a"
    assert c["count"] == 0 and c["created_at"]

    cols = await store.list_collections()
    assert any(x["id"] == c["id"] and x["count"] == 0 for x in cols)

    upd = await store.update_collection(c["id"], name="Scouted 2026", color="#b5651d")
    assert upd["name"] == "Scouted 2026" and upd["color"] == "#b5651d"
    assert await store.update_collection(999999, name="x") is None

    assert await store.delete_collection(c["id"]) is True
    assert await store.delete_collection(c["id"]) is False


async def test_position_increments_and_orders(store):
    a = await store.add_collection("A")
    b = await store.add_collection("B")
    assert b["position"] > a["position"]
    ordered = [x["id"] for x in await store.list_collections()]
    assert ordered.index(a["id"]) < ordered.index(b["id"])


async def test_set_place_collections_replaces_and_hydrates(store):
    s = await store.add_saved("site", "Spur", 47.5, -94.2)
    c1 = await store.add_collection("Lakeside")
    c2 = await store.add_collection("BWCA")

    place = await store.set_place_collections(s["id"], [c1["id"], c2["id"]])
    assert sorted(place["collection_ids"]) == sorted([c1["id"], c2["id"]])

    # PUT semantics: a second set REPLACES (not merges).
    place = await store.set_place_collections(s["id"], [c2["id"]])
    assert place["collection_ids"] == [c2["id"]]

    # Unknown collection ids are dropped, not FK-errored; missing place -> None.
    place = await store.set_place_collections(s["id"], [c1["id"], 999999])
    assert place["collection_ids"] == [c1["id"]]
    assert await store.set_place_collections(999999, [c1["id"]]) is None


async def test_list_saved_hydrates_collection_ids(store):
    s1 = await store.add_saved("site", "A", 1.0, 2.0)
    s2 = await store.add_saved("campground", "B", 3.0, 4.0)
    c = await store.add_collection("Fav")
    await store.set_place_collections(s1["id"], [c["id"]])

    by_id = {p["id"]: p for p in await store.list_saved()}
    assert by_id[s1["id"]]["collection_ids"] == [c["id"]]
    assert by_id[s2["id"]]["collection_ids"] == []
    # add_saved returns an empty membership set up front.
    assert s2["collection_ids"] == []


async def test_bulk_add_places_to_collection(store):
    s1 = await store.add_saved("site", "A", 1.0, 2.0)
    s2 = await store.add_saved("site", "B", 3.0, 4.0)
    c = await store.add_collection("Trip ideas")

    added = await store.add_places_to_collection(c["id"], [s1["id"], s2["id"]])
    assert added == 2
    # Idempotent: re-adding one already-present + a new-but-none yields 0 new.
    again = await store.add_places_to_collection(c["id"], [s1["id"], s2["id"]])
    assert again == 0
    # Unknown saved ids skipped; unknown collection -> None.
    assert await store.add_places_to_collection(c["id"], [999999]) == 0
    assert await store.add_places_to_collection(999999, [s1["id"]]) is None

    assert (await store.list_collections())[0]["count"] == 2


async def test_delete_collection_cascades_memberships(store):
    s = await store.add_saved("site", "X", 1.0, 2.0)
    c = await store.add_collection("Temp")
    await store.set_place_collections(s["id"], [c["id"]])
    await store.delete_collection(c["id"])
    # The saved place survives; its membership row is gone.
    assert any(p["id"] == s["id"] for p in await store.list_saved())
    cur = await store.db.execute(
        "SELECT COUNT(*) FROM place_collections WHERE collection_id=?", (c["id"],)
    )
    assert (await cur.fetchone())[0] == 0


async def test_delete_saved_prunes_memberships(store):
    s = await store.add_saved("site", "X", 1.0, 2.0)
    c = await store.add_collection("Keep")
    await store.set_place_collections(s["id"], [c["id"]])
    await store.delete_saved(s["id"])
    # The collection survives with a zero count; the join row is gone.
    assert (await store.update_collection(c["id"]))["count"] == 0
