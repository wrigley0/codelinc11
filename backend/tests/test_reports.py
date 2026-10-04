"""Reports (sprint 2, B4): synthetic claims, EOBs and copay-style visits.
Temporary database, no network, no model, no key."""
import pytest
from fastapi.testclient import TestClient

from app.db import Store
from app.db.core import connect, reset
from app.engine.reports import lines_problems, parse_amount_cents, totals
from app.main import app
from app.reports import DEMO_ONLY, HEADER, list_samples
from app.routers.session import get_store

SAMPLE_IDS = ["sample-paid-claim", "sample-eob-deductible", "sample-eob-out-of-network",
              "sample-denied-claim", "sample-copay-visit"]
PLAIN = {"Content-Type": "text/plain"}


@pytest.fixture()
def store(tmp_path):
    path = tmp_path / "reports.db"
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
    """A fresh demo family: (household id, {name: member id}, {name: auth headers})."""
    d, hj = enter(client, "m-jordan")
    hid = d["sandbox"]["household_id"]
    ids = {m["id"].split(".")[0].removeprefix("m-"): m["id"] for m in d["household"]["members"]}
    heads = {"jordan": hj}
    for who in ("alex", "noah"):
        _, heads[who] = enter(client, f"m-{who}", hid)
    return hid, ids, heads


def template(client):
    """The shared template family (read only): ids are the plain seed ids."""
    _, h = enter(client, "m-jordan", sandbox=False)
    return h


def rows(store, sql, args=()):
    with connect(store.path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def get_reports(client, mid, headers, **params):
    r = client.get(f"/members/{mid}/reports", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def add_sample(client, mid, headers, sample_id):
    r = client.post(f"/members/{mid}/reports/samples/{sample_id}", headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def doc(**overrides):
    """A valid EOB in the sample template; override or drop (None) any field."""
    f = {"Type": "EOB", "Date": "2026-09-22", "Provider": "Loblolly Smiles", "Code": "D2392",
         "Description": "Filling, 2 surfaces", "Claim number": "SYN-CLM-9", "EOB number": "SYN-EOB-9",
         "Billed": "260.00", "Allowed": "200.00", "Deductible": "50.00", "Coinsurance": "30.00",
         "Plan paid": "120.00", "Balance billing": "0.00", "You owe": "80.00"}
    f.update(overrides)
    return "\n".join([HEADER] + [f"{k}: {v}" for k, v in f.items() if v is not None]) + "\n"


def upload(client, mid, headers, text, **params):
    return client.post(f"/members/{mid}/reports/upload", headers={**headers, **PLAIN},
                       content=text.encode("utf-8"), params=params)


# ---------- samples ----------

def test_samples_shape_and_auth(client):
    assert client.get("/reports/samples").status_code == 401
    r = client.get("/reports/samples", headers=template(client))
    assert r.status_code == 200
    body = r.json()
    assert [s["id"] for s in body] == SAMPLE_IDS
    for s in body:
        assert set(s) == {"id", "title", "kind", "description", "text"}
        assert s["text"].startswith(HEADER + "\n")
    assert {s["kind"] for s in body} == {"claim", "eob", "copay"}


def test_every_sample_parses_adds_up_and_has_no_arithmetic_problem(client):
    _hid, ids, heads = family(client)
    for sid in SAMPLE_IDS:
        item = add_sample(client, ids["alex"], heads["alex"], sid)
        assert item["member_id"] == ids["alex"] and item["id"].endswith("." + ids["alex"].split(".")[1])
        ex = client.get(f"/members/{ids['alex']}/reports/{item['id']}/explain", headers=heads["alex"]).json()
        assert ex["lines_add_up"] is True


def test_sample_unknown_is_404(client):
    _hid, ids, heads = family(client)
    r = client.post(f"/members/{ids['alex']}/reports/samples/nope", headers=heads["alex"])
    assert r.status_code == 404


# ---------- seeds, listing, totals ----------

def test_alex_seeded_reports_match_his_visits_and_the_golden_usage(client):
    heads = template(client)
    body = get_reports(client, "m-alex", heads)
    assert body["count"] == 5
    assert [i["kind"] for i in body["items"]] == ["copay", "claim", "eob", "claim", "eob"]
    assert [i["service_date"] for i in body["items"]] == sorted(i["service_date"] for i in body["items"])
    # AC's visit history: a cleaning, a filling and an extraction (database/seeds/demo_household.json).
    assert [i["service_date"] for i in body["items"] if i["kind"] != "claim"] == ["2026-02-20", "2026-06-03", "2026-08-12"]
    t = body["totals"]
    assert t == {"billed": 1410.0, "allowed": 1410.0, "plan_paid": 1100.0, "you_paid": 310.0, "you_owe_open": 0.0}
    assert t["plan_paid"] == 1100.0                      # equals AC's $1,100 of yearly maximum used (S2)
    filling = next(i for i in body["items"] if i["kind"] == "eob" and i["code"] == "D2392")
    d = filling["data"]
    assert (d["billed"], d["allowed"], d["deductible_applied"], d["plan_paid"], d["you_owe"]) == (340, 340, 50, 220, 120)
    assert filling["provider_id"] == "prv-002" and filling["paid_status"] == "paid"


def test_jordan_has_one_open_eob(client):
    heads = template(client)
    body = get_reports(client, "m-jordan", heads)
    assert body["count"] == 1 and body["items"][0]["paid_status"] == "unpaid"
    assert body["totals"]["you_owe_open"] == 90.0 and body["totals"]["you_paid"] == 0.0


def test_totals_are_the_sum_of_stored_values(client):
    _hid, ids, heads = family(client)
    for sid in SAMPLE_IDS:
        add_sample(client, ids["alex"], heads["alex"], sid)
    body = get_reports(client, ids["alex"], heads["alex"])
    items = body["items"]
    eob_claims = {i["data"]["claim_number"] for i in items if i["kind"] == "eob"}
    counted = [i for i in items if not (i["kind"] == "claim" and i["data"]["claim_number"] in eob_claims)]
    t = body["totals"]
    assert t["billed"] == round(sum(i["data"].get("billed") or 0 for i in counted), 2)
    assert t["plan_paid"] == round(sum(i["data"].get("plan_paid") or 0 for i in counted), 2)
    assert t["you_owe_open"] == round(
        sum(i["data"].get("you_owe") or 0 for i in items if i["paid_status"] == "unpaid"), 2)
    assert t["you_paid"] == round(
        sum(i["data"].get("you_owe") or 0 for i in items if i["paid_status"] == "paid"), 2)
    # The two sample EOBs and the copay visit are unpaid: 80 + 925 + 25.
    assert t["you_owe_open"] == 1030.0


def test_filters_order_and_bad_dates(client):
    heads = template(client)
    asc = get_reports(client, "m-alex", heads)["items"]
    desc = get_reports(client, "m-alex", heads, order="desc")["items"]
    assert [i["service_date"] for i in desc] == sorted((i["service_date"] for i in asc), reverse=True)
    assert {i["id"] for i in desc} == {i["id"] for i in asc}
    eobs = get_reports(client, "m-alex", heads, kind="eob")
    assert eobs["count"] == 2 and all(i["kind"] == "eob" for i in eobs["items"])
    assert eobs["totals"]["plan_paid"] == 980.0 and eobs["totals"]["billed"] == 1290.0
    window = get_reports(client, "m-alex", heads, **{"from": "2026-06-01", "to": "2026-06-30"})
    assert {i["service_date"] for i in window["items"]} == {"2026-06-03"}
    assert client.get("/members/m-alex/reports", headers=heads, params={"from": "June"}).status_code == 422
    assert client.get("/members/m-alex/reports", headers=heads, params={"to": "2026-13-40"}).status_code == 422
    assert client.get("/members/m-alex/reports", headers=heads, params={"kind": "bill"}).status_code == 422
    assert client.get("/members/m-alex/reports", headers=heads, params={"order": "sideways"}).status_code == 422


# ---------- visibility ----------

def test_visibility_rules(client):
    _hid, ids, heads = family(client)
    assert get_reports(client, ids["alex"], heads["jordan"])["count"] == 5            # primary sees anyone
    assert get_reports(client, ids["alex"], heads["alex"])["count"] == 5              # adult sees themself
    for path in ("reports", "reports/anything", "reports/anything/explain"):
        assert client.get(f"/members/{ids['jordan']}/{path}", headers=heads["alex"]).status_code == 403
    assert client.get(f"/members/{ids['maya']}/reports", headers=heads["alex"]).status_code == 403
    assert client.get(f"/members/{ids['maya']}/reports", headers=heads["jordan"]).status_code == 200
    assert client.get("/members/m-nobody/reports", headers=heads["jordan"]).status_code == 404
    assert client.get(f"/members/{ids['alex']}/reports").status_code == 401
    # Sophia is a managed member: she has no login at all.
    assert client.post("/auth/demo-login", json={"member_id": "m-maya"}).status_code != 200
    # AC cannot write to Marc's reports either.
    r = client.post(f"/members/{ids['jordan']}/reports/samples/sample-paid-claim", headers=heads["alex"])
    assert r.status_code == 403
    assert upload(client, ids["jordan"], heads["alex"], doc()).status_code == 403


def test_get_one_and_unknown_ids(client):
    heads = template(client)
    first = get_reports(client, "m-alex", heads)["items"][0]
    r = client.get(f"/members/m-alex/reports/{first['id']}", headers=heads)
    assert r.status_code == 200 and r.json() == first
    assert client.get("/members/m-alex/reports/nope", headers=heads).status_code == 404
    assert client.get("/members/m-alex/reports/nope/explain", headers=heads).status_code == 404
    # An id that belongs to someone else is not found under this member.
    other = get_reports(client, "m-jordan", heads)["items"][0]["id"]
    assert client.get(f"/members/m-alex/reports/{other}", headers=heads).status_code == 404


# ---------- writes are demo family only ----------

def test_template_family_is_read_only(client, store):
    heads = template(client)
    before = rows(store, "SELECT * FROM report_items ORDER BY id")
    item_id = get_reports(client, "m-jordan", heads)["items"][0]["id"]
    assert client.post("/members/m-alex/reports/samples/sample-paid-claim", headers=heads).status_code == 403
    assert upload(client, "m-alex", heads, doc()).status_code == 403
    assert client.post(f"/members/m-jordan/reports/{item_id}/mark-paid", headers=heads).status_code == 403
    assert client.delete(f"/members/m-jordan/reports/{item_id}", headers=heads).status_code == 403
    assert rows(store, "SELECT * FROM report_items ORDER BY id") == before


# ---------- upload ----------

def test_upload_accepts_the_template_and_saves_it(client):
    _hid, ids, heads = family(client)
    r = upload(client, ids["alex"], heads["alex"], doc(Provider="Plainsman Family Dental"), kind="eob",
               filename="my-eob.txt")
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["kind"] == "eob" and item["provider_id"] == "prv-001"
    assert item["data"]["you_owe"] == 80.0 and item["paid_status"] == "unpaid"
    assert item["title"] == "EOB: Filling, 2 surfaces"
    assert get_reports(client, ids["alex"], heads["alex"])["count"] == 6
    # An unknown practice is kept as written, with no directory link.
    other = upload(client, ids["alex"], heads["alex"], doc(Provider="Made Up Dental")).json()
    assert other["provider_id"] is None and other["provider_name"] == "Made Up Dental"
    # "Paid: yes" saves it as already paid. A $0 balance has nothing to pay.
    assert upload(client, ids["alex"], heads["alex"], doc(Paid="yes")).json()["paid_status"] == "paid"
    zero = doc(Deductible="0.00", Coinsurance="0.00", **{"Plan paid": "200.00", "You owe": "0.00"})
    assert upload(client, ids["alex"], heads["alex"], zero).json()["paid_status"] == "not_applicable"


@pytest.mark.parametrize("text", [
    "I had a filling last week and paid $80.",                                  # free text
    doc().replace(HEADER, "SOME OTHER HEADER"),                                  # wrong header
    "\n" + doc().replace(HEADER + "\n", ""),                                     # no header
    doc(Note="hello"),                                                           # unknown key
    doc() + "Billed: 270.00\n",                                                  # duplicate key
    doc() + "this line has no colon\n",                                          # not Key: Value
    doc(Remark="<script>alert(1)</script>"),                                     # angle brackets
    doc(Provider="Loblolly <b>Smiles</b>"),
    doc(Description="x" * 121),                                                  # over-long value
    doc(Billed="two hundred"),                                                   # bad amount
    doc(Billed="-5.00"),
    doc(Billed="1234567.00"),
    doc(**{"You owe": "81.00"}),                                                 # lines do not add up
    doc(**{"Plan paid": "121.00"}),
    doc(Date="09/22/2026"),                                                      # bad date
    doc(Date="2027-01-15"),                                                      # after the demo date
    doc(Type="Invoice"),
    doc(Code="2392"),
    doc(**{"EOB number": None}),                                                 # missing required field
    doc(**{"Claim number": "bad number!"}),
    doc(Status="paid"),                                                          # status only belongs on claims
    doc(Paid="maybe"),
    "",
])
def test_upload_rejects_anything_but_the_template(client, text):
    _hid, ids, heads = family(client)
    r = upload(client, ids["alex"], heads["alex"], text)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == DEMO_ONLY == "Demo accepts the sample documents only."
    assert get_reports(client, ids["alex"], heads["alex"])["count"] == 5


def test_upload_never_echoes_the_text(client):
    _hid, ids, heads = family(client)
    marker = "ZXQ-SECRET-MARKER-12345"
    r = upload(client, ids["alex"], heads["alex"], f"{marker} my real record\n")
    assert r.status_code == 422 and marker not in r.text
    r = upload(client, ids["alex"], heads["alex"], doc(Remark=marker + "<"))
    assert r.status_code == 422 and marker not in r.text


def test_upload_content_type_size_kind_and_encoding(client):
    _hid, ids, heads = family(client)
    url = f"/members/{ids['alex']}/reports/upload"
    r = client.post(url, headers={**heads["alex"], "Content-Type": "application/json"}, content=doc().encode())
    assert r.status_code == 415
    r = client.post(url, headers={**heads["alex"], "Content-Type": "text/plain; charset=utf-8"}, content=doc().encode())
    assert r.status_code == 201
    big = doc() + "Remark: " + "x" * (20 * 1024)
    assert upload(client, ids["alex"], heads["alex"], big).status_code == 413
    assert upload(client, ids["alex"], heads["alex"], doc(), kind="claim").status_code == 422   # type mismatch
    assert upload(client, ids["alex"], heads["alex"], doc(), kind="bill").status_code == 422
    assert upload(client, ids["alex"], heads["alex"], doc(), filename="x" * 81).status_code == 422
    r = client.post(url, headers={**heads["alex"], **PLAIN}, content=b"\xff\xfe\x00bad")
    assert r.status_code == 422 and r.json()["detail"] == DEMO_ONLY


def test_cap_of_100_items_per_person(client, store):
    _hid, ids, heads = family(client)
    item = {"kind": "other", "service_date": "2026-01-01", "title": "t", "provider_name": "p",
            "data": {}, "paid_status": "not_applicable"}
    for _ in range(95):
        store.add_report_item(ids["alex"], ids["alex"], item)
    assert get_reports(client, ids["alex"], heads["alex"])["count"] == 100
    r = upload(client, ids["alex"], heads["alex"], doc())
    assert r.status_code == 422 and "100" in r.json()["detail"]
    assert client.post(f"/members/{ids['alex']}/reports/samples/sample-paid-claim",
                       headers=heads["alex"]).status_code == 422
    # Another person's count is separate.
    assert client.post(f"/members/{ids['jordan']}/reports/samples/sample-paid-claim",
                       headers=heads["jordan"]).status_code == 201


def test_other_kind_is_accepted(client):
    _hid, ids, heads = family(client)
    text = (f"{HEADER}\nType: Other\nDate: 2026-05-01\nProvider: Made Up Dental\n"
            "Description: Statement from the office\nYou owe: 15.00\n")
    r = upload(client, ids["alex"], heads["alex"], text)
    assert r.status_code == 201 and r.json()["kind"] == "other" and r.json()["paid_status"] == "unpaid"
    ex = client.get(f"/members/{ids['alex']}/reports/{r.json()['id']}/explain", headers=heads["alex"])
    assert ex.status_code == 200 and ex.json()["steps"][0]["key"] == "you_owe"


# ---------- mark paid and delete ----------

def test_mark_paid_moves_the_total(client):
    _hid, ids, heads = family(client)
    item = add_sample(client, ids["alex"], heads["alex"], "sample-eob-deductible")
    assert item["paid_status"] == "unpaid"
    before = get_reports(client, ids["alex"], heads["alex"])["totals"]
    r = client.post(f"/members/{ids['alex']}/reports/{item['id']}/mark-paid", headers=heads["alex"])
    assert r.status_code == 200 and r.json()["paid_status"] == "paid"
    after = get_reports(client, ids["alex"], heads["alex"])["totals"]
    assert after["you_owe_open"] == before["you_owe_open"] - 80 and after["you_paid"] == before["you_paid"] + 80
    assert client.post(f"/members/{ids['alex']}/reports/{item['id']}/mark-paid",
                       headers=heads["alex"]).status_code == 200                      # again: fine
    claim = add_sample(client, ids["alex"], heads["alex"], "sample-paid-claim")
    r = client.post(f"/members/{ids['alex']}/reports/{claim['id']}/mark-paid", headers=heads["alex"])
    assert r.status_code == 422 and "nothing to pay" in r.json()["detail"].lower()
    assert client.post(f"/members/{ids['alex']}/reports/nope/mark-paid", headers=heads["alex"]).status_code == 404


def test_delete(client):
    _hid, ids, heads = family(client)
    item = add_sample(client, ids["alex"], heads["alex"], "sample-copay-visit")
    r = client.delete(f"/members/{ids['alex']}/reports/{item['id']}", headers=heads["alex"])
    assert r.status_code == 204 and r.content == b""
    assert client.get(f"/members/{ids['alex']}/reports/{item['id']}", headers=heads["alex"]).status_code == 404
    assert client.delete(f"/members/{ids['alex']}/reports/{item['id']}", headers=heads["alex"]).status_code == 404


# ---------- explain ----------

def test_explain_eob_steps_match_the_stored_numbers(client):
    _hid, ids, heads = family(client)
    item = add_sample(client, ids["alex"], heads["alex"], "sample-eob-deductible")
    ex = client.get(f"/members/{ids['alex']}/reports/{item['id']}/explain", headers=heads["alex"]).json()
    assert [s["key"] for s in ex["steps"]] == ["billed", "allowed", "deductible", "plan_paid", "you_owe"]
    assert [s["amount"] for s in ex["steps"]] == [260.0, 200.0, 50.0, 120.0, 80.0]
    d = item["data"]
    assert [s["amount"] for s in ex["steps"]] == [d["billed"], d["allowed"], d["deductible_applied"],
                                                  d["plan_paid"], d["you_owe"]]
    for s, text in zip(ex["steps"], ["$260", "$200", "$50", "$120", "$80"], strict=True):
        assert text in s["plain"]
    assert ex["balance_billing_note"] is None and ex["lines_add_up"] is True
    assert "not a bill" in ex["what_it_is"]
    assert any("$80" in line for line in ex["what_to_do_next"])
    assert "This is an estimate" in ex["disclaimer"] and "made-up" in ex["synthetic_notice"]
    assert {ln["label"] for ln in ex["lines"]} >= {"Billed", "Allowed amount", "Deductible", "Plan paid", "You owe"}


def test_explain_out_of_network_balance_billing(client):
    _hid, ids, heads = family(client)
    item = add_sample(client, ids["alex"], heads["alex"], "sample-eob-out-of-network")
    ex = client.get(f"/members/{ids['alex']}/reports/{item['id']}/explain", headers=heads["alex"]).json()
    assert [s["amount"] for s in ex["steps"]] == [1500.0, 1200.0, 50.0, 575.0, 925.0]   # G5: you pay $925
    note = ex["balance_billing_note"]
    assert "$300" in note and "$1,200" in note and "out of network" in note and "balance billing" in note
    assert "$300" in ex["steps"][-1]["plain"]


def test_explain_claims_and_copay(client):
    _hid, ids, heads = family(client)
    ok = add_sample(client, ids["alex"], heads["alex"], "sample-paid-claim")
    denied = add_sample(client, ids["alex"], heads["alex"], "sample-denied-claim")
    copay = add_sample(client, ids["alex"], heads["alex"], "sample-copay-visit")
    ex_ok, ex_den, ex_cp = (client.get(f"/members/{ids['alex']}/reports/{i['id']}/explain",
                                       headers=heads["alex"]).json() for i in (ok, denied, copay))
    assert "claim is the bill" in ex_ok["what_it_is"] and "paid its share" in ex_ok["what_it_is"]
    assert [s["key"] for s in ex_ok["steps"]] == ["billed"]
    assert "did not pay" in ex_den["what_it_is"]
    assert any("appeal" in t for t in ex_den["what_to_do_next"]) and any("don't wait" in t for t in ex_den["what_to_do_next"])
    assert [s["key"] for s in ex_cp["steps"]] == ["billed", "allowed", "plan_paid", "you_owe"]
    assert [s["amount"] for s in ex_cp["steps"]] == [150.0, 150.0, 125.0, 25.0]
    assert "copay" in ex_cp["steps"][-1]["plain"]


def test_explanations_are_calm_and_never_say_delay(client):
    _hid, ids, heads = family(client)
    for sid in SAMPLE_IDS:
        add_sample(client, ids["alex"], heads["alex"], sid)
    for item in get_reports(client, ids["alex"], heads["alex"])["items"]:
        ex = client.get(f"/members/{ids['alex']}/reports/{item['id']}/explain", headers=heads["alex"])
        assert ex.status_code == 200
        text = ex.text.lower()
        for bad in ("delay", "postpone", "wait until", "ignore"):
            assert bad not in text
        assert ex.json()["disclaimer"].startswith("This is an estimate.")


# ---------- sandboxes: clone, reset, delete ----------

def test_sandbox_clones_reports_and_families_are_independent(client, store):
    _hid, ids, heads = family(client)
    _hid2, ids2, heads2 = family(client)
    assert ids["alex"] != ids2["alex"]
    one = get_reports(client, ids["alex"], heads["alex"])["items"]
    two = get_reports(client, ids2["alex"], heads2["alex"])["items"]
    assert len(one) == len(two) == 5
    assert {i["id"] for i in one}.isdisjoint({i["id"] for i in two})
    assert all(i["member_id"] == ids["alex"] and i["id"].endswith(ids["alex"].split(".")[1]) for i in one)
    assert [i["data"] for i in one] == [i["data"] for i in two]
    assert get_reports(client, ids["jordan"], heads["jordan"])["totals"]["you_owe_open"] == 90.0
    add_sample(client, ids["alex"], heads["alex"], "sample-copay-visit")
    assert get_reports(client, ids2["alex"], heads2["alex"])["count"] == 5          # untouched
    assert rows(store, "SELECT COUNT(*) AS n FROM report_items WHERE member_id = 'm-alex'")[0]["n"] == 5


def test_demo_reset_restores_reports(client, store):
    _hid, ids, heads = family(client)
    gone = get_reports(client, ids["jordan"], heads["jordan"])["items"][0]["id"]
    assert client.delete(f"/members/{ids['jordan']}/reports/{gone}", headers=heads["jordan"]).status_code == 204
    add_sample(client, ids["alex"], heads["alex"], "sample-denied-claim")
    paid = next(i for i in get_reports(client, ids["alex"], heads["alex"])["items"] if i["code"] == "D2392" and i["kind"] == "eob")
    assert paid["paid_status"] == "paid"
    assert client.post("/demo/reset", headers=heads["jordan"]).status_code == 200
    assert get_reports(client, ids["alex"], heads["alex"])["count"] == 5
    j = get_reports(client, ids["jordan"], heads["jordan"])
    assert j["count"] == 1 and j["items"][0]["id"] == gone and j["items"][0]["paid_status"] == "unpaid"


def test_expired_sandbox_and_removed_member_delete_their_reports(client, store):
    from app.db import sandbox
    hid, ids, heads = family(client)
    assert rows(store, "SELECT COUNT(*) AS n FROM report_items WHERE member_id LIKE '%.%'")[0]["n"] == 6
    # Removing a person removes their reports too (Sophia has none; use AC).
    r = client.delete(f"/households/{hid}/members/{ids['alex']}", headers=heads["jordan"])
    assert r.status_code == 200, r.text
    assert rows(store, "SELECT COUNT(*) AS n FROM report_items WHERE member_id = ?", (ids["alex"],))[0]["n"] == 0
    assert rows(store, "SELECT COUNT(*) AS n FROM report_items WHERE member_id = ?", (ids["jordan"],))[0]["n"] == 1
    with connect(store.path) as c:
        sandbox._delete_household_rows(c, hid, keep_household=False)
        c.commit()
    assert rows(store, "SELECT COUNT(*) AS n FROM report_items WHERE member_id LIKE '%.%'")[0]["n"] == 0
    assert rows(store, "SELECT COUNT(*) AS n FROM report_items WHERE member_id = 'm-alex'")[0]["n"] == 5


# ---------- notifications ----------

def notes(client, mid, headers):
    r = client.get(f"/members/{mid}/notifications", headers=headers)
    assert r.status_code == 200
    return r.json()["notifications"]


def test_alex_notifications_unchanged_and_jordan_gets_eob_ready(client):
    _hid, ids, heads = family(client)
    alex = notes(client, ids["alex"], heads["alex"])
    assert len(alex) == 5 and not {"claim_update", "eob_ready"} & {n["kind"] for n in alex}
    jordan = [n for n in notes(client, ids["jordan"], heads["jordan"]) if n["kind"] == "eob_ready"]
    assert len(jordan) == 1
    n = jordan[0]
    assert n["title"] == "Your EOB is ready: you owe $90" and n["link"] == "/reports" and n["severity"] == "info"
    assert "This is an estimate" in n["body"] and "delay" not in n["body"].lower()


def test_claim_update_and_eob_ready_are_generated_once_per_item(client):
    _hid, ids, heads = family(client)
    mid, h = ids["alex"], heads["alex"]
    notes(client, mid, h)
    add_sample(client, mid, h, "sample-paid-claim")                     # a paid claim: no notification
    add_sample(client, mid, h, "sample-copay-visit")                    # nothing wrong
    assert len(notes(client, mid, h)) == 5
    add_sample(client, mid, h, "sample-denied-claim")
    eob = add_sample(client, mid, h, "sample-eob-deductible")
    first = notes(client, mid, h)
    assert len(first) == 7
    kinds = sorted(n["kind"] for n in first if n["kind"] in ("claim_update", "eob_ready"))
    assert kinds == ["claim_update", "eob_ready"]
    cu = next(n for n in first if n["kind"] == "claim_update")
    assert cu["severity"] == "warning" and cu["link"] == "/reports" and "did not pay" in cu["body"]
    assert "don't wait" in cu["body"] and "This is an estimate" in cu["body"]
    eo = next(n for n in first if n["kind"] == "eob_ready")
    assert eo["title"] == "Your EOB is ready: you owe $80"
    assert len(notes(client, mid, h)) == 7                               # again: no duplicates
    client.post(f"/members/{mid}/notifications/read-all", headers=h)
    assert len(notes(client, mid, h)) == 7 and client.get(
        f"/members/{mid}/notifications", headers=h).json()["unread_count"] == 0
    # Paying the EOB does not create anything new; a second EOB gets its own notification.
    client.post(f"/members/{mid}/reports/{eob['id']}/mark-paid", headers=h)
    assert len(notes(client, mid, h)) == 7
    add_sample(client, mid, h, "sample-eob-out-of-network")
    after = notes(client, mid, h)
    assert len(after) == 8
    assert sorted(n["title"] for n in after if n["kind"] == "eob_ready") == [
        "Your EOB is ready: you owe $80", "Your EOB is ready: you owe $925"]


def test_pending_claim_is_an_info_update(client):
    _hid, ids, heads = family(client)
    text = (f"{HEADER}\nType: Claim\nDate: 2026-10-20\nProvider: Plainsman Family Dental\nCode: D1110\n"
            "Description: Cleaning (adult)\nClaim number: SYN-CLM-PEND1\nBilled: 120.00\nStatus: pending\n")
    assert upload(client, ids["alex"], heads["alex"], text).status_code == 201
    cu = [n for n in notes(client, ids["alex"], heads["alex"]) if n["kind"] == "claim_update"]
    assert len(cu) == 1 and cu[0]["severity"] == "info" and "still reviewing" in cu[0]["body"]


def test_email_preview_for_a_new_eob_when_email_is_on(client):
    _hid, ids, heads = family(client)
    mid, h = ids["alex"], heads["alex"]
    notes(client, mid, h)
    assert client.put(f"/members/{mid}/notification-prefs", headers=h,
                      json={"app": True, "email": True, "sms": False, "types": None}).status_code == 200
    add_sample(client, mid, h, "sample-eob-deductible")
    notes(client, mid, h)
    out = client.get(f"/members/{mid}/outbox", headers=h).json()
    assert any("you owe $80" in m["body"] for m in out) and all(m["status"] == "preview" for m in out)


# ---------- engine helpers ----------

def test_parse_amount_cents():
    assert parse_amount_cents("120") == 12000
    assert parse_amount_cents("$1,200.5") == 120050
    assert parse_amount_cents(" 0.05 ") == 5
    assert parse_amount_cents("999999.99") == 99_999_999
    for bad in ("", "abc", "-5", "1.234", "1,20", "1000000.00", "$", "12.", "1e3"):
        assert parse_amount_cents(bad) is None


def test_lines_problems():
    eob = {"billed_cents": 26000, "allowed_cents": 20000, "deductible_applied_cents": 5000,
           "coinsurance_cents": 3000, "plan_paid_cents": 12000, "you_owe_cents": 8000, "balance_billing_cents": 0}
    assert lines_problems("eob", eob) == []
    assert lines_problems("eob", {**eob, "you_owe_cents": 8100})
    assert lines_problems("eob", {**eob, "plan_paid_cents": 12100})
    assert lines_problems("eob", {**eob, "balance_billing_cents": 6000, "you_owe_cents": 14000}) == []   # 26000-20000
    assert lines_problems("eob", {**eob, "balance_billing_cents": 5000, "you_owe_cents": 13000})       # not billed - allowed
    assert lines_problems("eob", {**eob, "billed_cents": 10000})                                   # allowed above billed
    assert lines_problems("claim", {"billed_cents": 5}) == []
    assert lines_problems("copay", {"allowed_cents": 15000, "plan_paid_cents": 12500, "copay_cents": 2500,
                                    "you_owe_cents": 2500}) == []
    assert lines_problems("copay", {"allowed_cents": 15000, "plan_paid_cents": 12500, "copay_cents": 2000,
                                    "you_owe_cents": 2500})


def test_totals_do_not_count_a_claim_twice():
    claim = {"kind": "claim", "paid_status": "not_applicable", "data": {"claim_number": "C1", "billed_cents": 10000}}
    eob = {"kind": "eob", "paid_status": "unpaid",
           "data": {"claim_number": "C1", "billed_cents": 10000, "allowed_cents": 9000, "plan_paid_cents": 7000,
                    "you_owe_cents": 2000}}
    lone = {"kind": "claim", "paid_status": "not_applicable", "data": {"claim_number": "C2", "billed_cents": 500}}
    assert totals([claim, eob, lone]) == {"billed": 10500, "allowed": 9000, "plan_paid": 7000,
                                          "you_paid": 0, "you_owe_open": 2000}
    assert totals([]) == {"billed": 0, "allowed": 0, "plan_paid": 0, "you_paid": 0, "you_owe_open": 0}


def test_sample_documents_are_internally_consistent():
    for s in list_samples():
        assert s.text.splitlines()[0] == HEADER and "<" not in s.text and ">" not in s.text


# ---------- database ----------

def test_report_items_check_constraints(store):
    import sqlite3
    base = ("INSERT INTO report_items (id, member_id, kind, service_date, title, provider_name, paid_status, created_at) "
            "VALUES ('x', 'm-alex', '{}', '2026-01-01', 't', 'p', '{}', 'now')")
    with connect(store.path) as c:
        c.execute(base.format("eob", "unpaid"))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(base.format("bill", "unpaid").replace("'x'", "'y'"))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(base.format("eob", "late").replace("'x'", "'z'"))


def test_migration_009_applies_to_an_older_database_and_backfills_the_template(tmp_path):
    from app.db import core
    old = tmp_path / "old.db"
    real = core.MIGRATIONS_DIR
    older = tmp_path / "mig"
    older.mkdir()
    for f in sorted(real.glob("00[1-8]_*.sql")):
        (older / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    core.MIGRATIONS_DIR = older
    try:
        core.migrate(old)
        core.seed(old)                          # seeded before reports existed
    finally:
        core.MIGRATIONS_DIR = real
    assert core.migrate(old) == ["009_reports.sql", "010_real_delivery.sql"]
    s = Store(old)
    assert rows(s, "SELECT COUNT(*) AS n FROM report_items")[0]["n"] == 6        # template family got its reports
    assert core.migrate(old) == []
    assert rows(s, "SELECT COUNT(*) AS n FROM report_items")[0]["n"] == 6        # not duplicated


def test_new_routes_are_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    present = {(m, p) for p, ops in paths.items() for m in ops}
    expected = {
        ("get", "/reports/samples"), ("post", "/members/{member_id}/reports/samples/{sample_id}"),
        ("post", "/members/{member_id}/reports/upload"), ("get", "/members/{member_id}/reports"),
        ("get", "/members/{member_id}/reports/{item_id}"), ("get", "/members/{member_id}/reports/{item_id}/explain"),
        ("post", "/members/{member_id}/reports/{item_id}/mark-paid"),
        ("delete", "/members/{member_id}/reports/{item_id}"), ("get", "/treatment-plan/samples"),
    }
    assert expected - present == set()
