"""Providers directory (sprint 2, B3): GET /providers, GET /providers/{id}, primary_dentist_id.
Temporary database, no network, no model, no key."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.db import Store
from app.db.core import connect, reset
from app.main import app
from app.routers.providers import UNKNOWN_ZIP, haversine_mi
from app.routers.session import get_store


@pytest.fixture()
def store(tmp_path):
    path = tmp_path / "providers.db"
    reset(path)
    return Store(path)


@pytest.fixture()
def client(store):
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def enter(client, member="m-jordan", household_id=None, sandbox=True):
    body = {"member_id": member, "sandbox": sandbox}
    if household_id:
        body["household_id"] = household_id
    r = client.post("/auth/demo-login", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    return d, {"Authorization": f"Bearer {d['token']}"}


def family(client):
    d, hj = enter(client, "m-jordan")
    hid = d["sandbox"]["household_id"]
    ids = {m["id"].split(".")[0].removeprefix("m-"): m["id"] for m in d["household"]["members"]}
    heads = {"jordan": hj}
    for who in ("alex", "noah"):
        _, heads[who] = enter(client, f"m-{who}", hid)
    return hid, ids, heads


def rows(store, sql, args=()):
    with connect(store.path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def search(client, headers, **params):
    return client.get("/providers", headers=headers, params=params)


def set_plan(client, hid, headers, tier):
    r = client.put(f"/households/{hid}/plan", headers=headers, json={"tier_id": tier})
    assert r.status_code == 200, r.text


# ---------- data ----------

def test_seed_shape(store):
    ps = rows(store, "SELECT * FROM providers")
    assert 28 <= len(ps) <= 40
    assert {p["specialty"] for p in ps} == {"general", "pediatric", "orthodontics", "oral_surgery",
                                            "endodontics", "periodontics"}
    zips = {r["zip"] for r in rows(store, "SELECT zip FROM zip_centroids")}
    assert {p["zip"] for p in ps} <= zips
    assert len(zips) >= len({p["zip"] for p in ps}) + 15
    assert {"36830", "30303", "19087", "46802", "27401"} <= zips
    nets = {tuple(json.loads(p["network_plan_ids"])) for p in ps}
    assert nets == {("basic", "preferred", "premium"), ("preferred", "premium"), ()}


def test_haversine_known_pair():
    # Auburn AL to Montgomery AL centres: about 45 miles in a straight line.
    assert 40 <= haversine_mi(32.58, -85.50, 32.37, -86.30) <= 55
    assert haversine_mi(10, 10, 10, 10) == 0


# ---------- search ----------

def test_zip_distance_sanity_sorted_and_capped(client):
    _hid, _ids, heads = family(client)
    r = search(client, heads["jordan"], zip="36830", radius_mi=100)
    assert r.status_code == 200
    items = r.json()
    assert 0 < len(items) <= 50
    d = [i["distance_mi"] for i in items]
    assert d == sorted(d)
    assert all(round(x, 1) == x for x in d)
    assert d[0] < 5
    montgomery = [i for i in items if i["city"] == "Montgomery"]
    assert montgomery and all(35 <= i["distance_mi"] <= 60 for i in montgomery)
    assert all(i["estimate"] is None for i in items)


def test_default_radius_and_radius_filter(client):
    _hid, _ids, heads = family(client)
    default = search(client, heads["jordan"], zip="36830").json()
    assert default and all(i["distance_mi"] <= 25 for i in default)
    assert not any(i["city"] == "Montgomery" for i in default)
    tight = search(client, heads["jordan"], zip="36830", radius_mi=1).json()
    assert len(tight) < len(default) and all(i["distance_mi"] <= 1 for i in tight)
    assert search(client, heads["jordan"], zip="36830", radius_mi=101).status_code == 422
    assert search(client, heads["jordan"], zip="36830", radius_mi=0).status_code == 422


def test_filters_specialty_accepting_q_network(client):
    _hid, _ids, heads = family(client)
    h = heads["jordan"]
    base = search(client, h, zip="30303", radius_mi=100).json()
    assert {i["city"] for i in base} >= {"Atlanta", "Decatur"}
    ortho = search(client, h, zip="30303", radius_mi=100, specialty="orthodontics").json()
    assert ortho and all(i["specialty"] == "orthodontics" for i in ortho)
    acc = search(client, h, zip="30303", radius_mi=100, accepting="true").json()
    assert acc and all(i["accepting_new"] for i in acc) and len(acc) < len(base)
    q = search(client, h, zip="30303", radius_mi=100, q="  PEACHBUD ").json()
    assert [i["practice_name"] for i in q] == ["Peachbud Family Dentistry"]
    by_dentist = search(client, h, zip="30303", radius_mi=100, q="chao").json()
    assert [i["dentist_name"] for i in by_dentist] == ["Dr. Mei-Ling Chao"]
    ins = search(client, h, zip="30303", radius_mi=100, network="in").json()
    outs = search(client, h, zip="30303", radius_mi=100, network="out").json()
    assert ins and outs and all(i["in_network"] for i in ins) and not any(i["in_network"] for i in outs)
    assert len(ins) + len(outs) == len(base)
    assert search(client, h, zip="30303", specialty="podiatry").status_code == 422
    assert search(client, h, zip="30303", network="maybe").status_code == 422


def test_network_flips_when_the_household_plan_changes(client):
    hid, _ids, heads = family(client)
    h = heads["jordan"]

    def in_ids(tier):
        set_plan(client, hid, h, tier)
        items = search(client, h, zip="36830", radius_mi=100).json()
        flags = {i["id"]: i["in_network"] for i in items}
        only_in = {i["id"] for i in search(client, h, zip="36830", radius_mi=100, network="in").json()}
        assert only_in == {k for k, v in flags.items() if v}
        return only_in

    preferred = in_ids("preferred")
    basic = in_ids("basic")
    premium = in_ids("premium")
    assert basic and basic < preferred
    assert preferred == premium
    assert in_ids("preferred") == preferred          # and back again
    items = search(client, h, zip="36830", radius_mi=100).json()
    assert any(i["network_plan_ids"] == [] and not i["in_network"] for i in items)


def test_zip_errors_and_member_zip_fallback(client):
    _hid, ids, heads = family(client)
    h = heads["jordan"]
    for bad in ("00000", "abc", "3683"):
        r = search(client, h, zip=bad)
        assert r.status_code == 422 and r.json()["detail"] == UNKNOWN_ZIP
    # No zip: Marc's profile ZIP (36830) is used; same as passing it.
    assert search(client, h).json() == search(client, h, zip="36830").json()
    # No ZIP anywhere: 422 with the same message.
    assert client.patch(f"/members/{ids['jordan']}/profile", headers=h, json={"zip": ""}).status_code == 200
    r = search(client, h)
    assert r.status_code == 422 and r.json()["detail"] == UNKNOWN_ZIP
    # A ZIP on the member that the directory does not know.
    client.patch(f"/members/{ids['jordan']}/profile", headers=h, json={"zip": "99999"})
    assert search(client, h).status_code == 422


def test_requires_sign_in(client):
    assert client.get("/providers", params={"zip": "36830"}).status_code == 401
    assert client.get("/providers/prv-001").status_code == 401
    assert client.get("/providers", headers={"Authorization": "Bearer nope"}).status_code == 401


# ---------- estimates ----------

def set_usage(store, member_id, used_dollars, deductible_dollars):
    with connect(store.path) as c:
        c.execute("UPDATE member_usage SET max_used_cents = ?, deductible_met_cents = ? "
                  "WHERE member_id = ? AND plan_year = 2026",
                  (used_dollars * 100, deductible_dollars * 100, member_id))


def test_estimate_matches_post_estimate_golden_crown(client, store):
    _hid, ids, heads = family(client)
    h = heads["jordan"]
    set_usage(store, ids["jordan"], 1100, 0)           # G3: $1,100 used, deductible not met
    items = search(client, h, zip="36830", radius_mi=100, code="D2740").json()
    innet = next(i for i in items if i["in_network"])
    outnet = next(i for i in items if not i["in_network"])
    assert innet["estimate"]["you_pay"] == 800 and innet["estimate"]["plan_pays"] == 400
    assert innet["estimate"]["in_network"] is True and innet["estimate"]["balance_bill"] == 0
    assert "not modeled" in innet["estimate"]["note"]
    ref = client.post("/estimate", json={"plan_id": "preferred", "code": "D2740",
                                         "usage": {"max_used": 1100, "deductible_met": 0}}).json()
    assert ref["in_network"]["you_pay"] == 800
    for i in items:
        e = i["estimate"]
        want = ref["in_network"] if i["in_network"] else ref["out_of_network"]
        assert (e["you_pay"], e["plan_pays"], e["balance_bill"], e["in_network"]) == \
               (want["you_pay"], want["plan_pays"], want["balance_bill"], i["in_network"])
    assert outnet["estimate"]["balance_bill"] > 0 and "balance billing" in outnet["estimate"]["note"]


def test_estimate_out_of_network_fresh_year_is_925(client, store):
    _hid, ids, heads = family(client)
    h = heads["jordan"]
    set_usage(store, ids["jordan"], 0, 0)
    items = search(client, h, zip="36830", radius_mi=100, code="D2740", network="out").json()
    assert items
    e = items[0]["estimate"]
    assert (e["you_pay"], e["plan_pays"], e["balance_bill"]) == (925, 575, 300)
    assert e["in_network"] is False
    e2 = search(client, h, zip="36830", radius_mi=100, code="D2740", network="in").json()[0]["estimate"]
    assert e2["you_pay"] == 625                         # G4: same crown in network, fresh year


def test_estimate_follows_plan_change_and_member(client, store):
    hid, ids, heads = family(client)
    h = heads["jordan"]
    set_usage(store, ids["jordan"], 0, 0)
    pays = {}
    for tier in ("basic", "preferred", "premium"):
        set_plan(client, hid, h, tier)
        r = client.get("/providers/prv-001", headers=h, params={"code": "D2740"})   # in all tiers
        pays[tier] = r.json()["estimate"]["you_pay"]
        ref = client.post("/estimate", json={"plan_id": tier, "code": "D2740"}).json()
        assert pays[tier] == ref["in_network"]["you_pay"]
    assert len(set(pays.values())) > 1
    # The primary may ask for another member (AC: $1,100 used, deductible met).
    set_plan(client, hid, h, "preferred")
    r = search(client, h, zip="36830", code="D2740", member_id=ids["alex"], network="in")
    assert r.status_code == 200
    ref = client.post("/estimate", json={"plan_id": "preferred", "code": "D2740",
                                         "usage": {"max_used": 1100, "deductible_met": 50}}).json()
    assert r.json()[0]["estimate"]["you_pay"] == ref["in_network"]["you_pay"]


def test_unknown_code_is_404_and_visibility_rules(client):
    _hid, ids, heads = family(client)
    assert search(client, heads["jordan"], zip="36830", code="D9999").status_code == 404
    # An adult may only ask about themself.
    assert search(client, heads["alex"], zip="36830", code="D2740", member_id=ids["jordan"]).status_code == 403
    assert search(client, heads["alex"], zip="36830", code="D2740", member_id=ids["alex"]).status_code == 200
    assert search(client, heads["alex"], zip="36830", code="D2740").status_code == 200   # default: self
    # The primary may ask about a managed member; an unknown member is 404.
    assert search(client, heads["jordan"], zip="36830", member_id=ids["maya"]).status_code == 200
    assert search(client, heads["jordan"], zip="36830", member_id="m-nobody").status_code == 404


def test_get_provider(client):
    _hid, _ids, heads = family(client)
    h = heads["jordan"]
    r = client.get("/providers/prv-001", headers=h)
    assert r.status_code == 200
    d = r.json()
    assert d["practice_name"] == "Plainsman Family Dental" and d["estimate"] is None
    assert d["distance_mi"] is not None          # Marc's profile ZIP is 36830
    far = client.get("/providers/prv-001", headers=h, params={"zip": "46802"}).json()
    assert far["distance_mi"] > 500
    assert client.get("/providers/prv-001", headers=h, params={"zip": "11111"}).status_code == 422
    assert client.get("/providers/nope", headers=h).status_code == 404


# ---------- primary dentist ----------

def test_primary_dentist_set_clear_and_validate(client, store):
    hid, ids, heads = family(client)
    h = heads["jordan"]
    url = f"/members/{ids['alex']}/profile"

    def stored():
        return rows(store, "SELECT primary_dentist_id FROM members WHERE id = ?",
                    (ids["alex"],))[0]["primary_dentist_id"]

    r = client.patch(url, headers=h, json={"primary_dentist_id": "prv-001"})
    assert r.status_code == 200 and r.json()["primary_dentist_id"] == "prv-001"
    assert stored() == "prv-001"
    got = client.get(f"/households/{hid}", headers=h).json()
    assert next(m for m in got["members"] if m["id"] == ids["alex"])["primary_dentist_id"] == "prv-001"
    r = client.patch(url, headers=h, json={"primary_dentist_id": "prv-does-not-exist"})
    assert r.status_code == 422 and "dentist" in r.json()["detail"]
    assert stored() == "prv-001"
    r = client.patch(url, headers=h, json={"primary_dentist_id": None})
    assert r.status_code == 200 and r.json()["primary_dentist_id"] is None
    client.patch(url, headers=h, json={"primary_dentist_id": "prv-002"})
    assert client.patch(url, headers=h, json={"primary_dentist_id": ""}).json()["primary_dentist_id"] is None
    # An adult may set their own, but not another person's.
    assert client.patch(url, headers=heads["alex"], json={"primary_dentist_id": "prv-003"}).status_code == 200
    assert client.patch(f"/members/{ids['jordan']}/profile", headers=heads["alex"],
                        json={"primary_dentist_id": "prv-003"}).status_code == 403
    # The shared template family can't be edited; no token is 401.
    _d, th = enter(client, "m-jordan", sandbox=False)
    assert client.patch("/members/m-jordan/profile", headers=th,
                        json={"primary_dentist_id": "prv-001"}).status_code == 403
    assert client.patch(url, json={"primary_dentist_id": "prv-001"}).status_code == 401


# ---------- global reference data and sandboxes ----------

def test_sandbox_clone_reset_and_cleanup_leave_providers_alone(client, store):
    before = rows(store, "SELECT * FROM providers ORDER BY id")
    zips = rows(store, "SELECT * FROM zip_centroids ORDER BY zip")
    hid, ids, heads = family(client)
    hid2, _ids2, _h2 = family(client)
    assert rows(store, "SELECT * FROM providers ORDER BY id") == before     # no per-sandbox copies
    assert rows(store, "SELECT * FROM zip_centroids ORDER BY zip") == zips
    assert rows(store, "SELECT COUNT(*) AS n FROM providers WHERE id LIKE '%.%'")[0]["n"] == 0
    client.patch(f"/members/{ids['alex']}/profile", headers=heads["jordan"],
                 json={"primary_dentist_id": "prv-001"})
    assert client.post("/demo/reset", headers=heads["jordan"]).status_code == 200
    assert rows(store, "SELECT * FROM providers ORDER BY id") == before
    assert rows(store, "SELECT * FROM zip_centroids ORDER BY zip") == zips
    got = client.get(f"/households/{hid}", headers=heads["jordan"]).json()
    assert all(m["primary_dentist_id"] is None for m in got["members"])      # reset clears the choice
    assert search(client, heads["jordan"], zip="36830").status_code == 200
    from app.db import sandbox
    with connect(store.path) as c:
        sandbox._delete_household_rows(c, hid2, keep_household=False)
        c.commit()
    assert rows(store, "SELECT * FROM providers ORDER BY id") == before


def test_reseed_and_older_database_get_the_directory(tmp_path):
    from app.db import core
    path = tmp_path / "re.db"
    core.reset(path)
    n = rows(Store(path), "SELECT COUNT(*) AS n FROM providers")[0]["n"]
    assert n >= 28
    core.reseed(path)
    assert rows(Store(path), "SELECT COUNT(*) AS n FROM providers")[0]["n"] == n
    # A database migrated before 008 gets the data when 008 is applied.
    old = tmp_path / "old.db"
    real = core.MIGRATIONS_DIR
    older = tmp_path / "mig"
    older.mkdir()
    for f in sorted(real.glob("00[1-7]_*.sql")):
        (older / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    core.MIGRATIONS_DIR = older
    try:
        core.migrate(old)
        core.seed(old)
    finally:
        core.MIGRATIONS_DIR = real
    assert core.migrate(old) == ["008_providers.sql", "009_reports.sql", "010_real_delivery.sql"]
    assert rows(Store(old), "SELECT COUNT(*) AS n FROM providers")[0]["n"] == n
    assert core.migrate(old) == []
    assert rows(Store(old), "SELECT COUNT(*) AS n FROM providers")[0]["n"] == n   # not duplicated


def test_specialty_check_constraint(store):
    with connect(store.path) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO providers (id, practice_name, dentist_name, specialty, address, city, state, "
                  "zip, lat, lon, phone) VALUES ('x','x','x','podiatry','x','x','x','00000',0,0,'x')")
