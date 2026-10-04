"""Notifications (sprint 2, B2): generation, read state, preferences, delivery previews, access.
Temporary database, no network, no model, no key. Nothing is ever sent."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import ratelimit
from app.db import DEMO_TODAY, Store
from app.db.core import connect, load_seed_file, reset
from app.main import app
from app.routers.session import get_store

TEMPLATE_HH = "hh-rivera"


@pytest.fixture()
def store(tmp_path):
    path = tmp_path / "notifications.db"
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


def template_headers(client, member="m-jordan"):
    r = client.post("/auth/demo-login", json={"member_id": member})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def rows(store, sql, args=()):
    with connect(store.path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def kinds(body):
    return [n["kind"] for n in body["notifications"]]


def get_list(client, member, headers, query=""):
    r = client.get(f"/members/{member}/notifications{query}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def add_appointment(store, member_id, due, kind="appointment", title="Cleaning and exam"):
    with connect(store.path) as c:
        c.execute("INSERT INTO appointments (member_id, kind, due_date, title, note) VALUES (?,?,?,?,NULL)",
                  (member_id, kind, due, title))
        c.commit()


# ---- generation ---------------------------------------------------------------------------

def test_demo_clock_is_the_expected_november_date():
    assert DEMO_TODAY.isoformat() == "2026-11-01"


def test_alex_gets_expiring_benefits_with_the_engine_amount(client):
    _hid, ids, heads = family(client)
    body = get_list(client, ids["alex"], heads["alex"])
    by_kind = {n["kind"]: n for n in body["notifications"]}
    assert set(by_kind) == {"benefits_expiring", "preventive_unused", "deductible_met", "procedure_planned", "reminder"}
    expiring = by_kind["benefits_expiring"]
    assert expiring["title"] == "$400 of your yearly maximum is left"   # 1,500 - 1,100 (FEATURES.md S2)
    assert expiring["severity"] == "warning"
    assert "2 month(s) left" in expiring["body"]
    overview = client.get(f"/members/{ids['alex']}/overview", headers=heads["alex"]).json()
    assert overview["benefits"]["max_remaining"] == 400 and overview["benefits"]["months_left"] == 2
    unused = by_kind["preventive_unused"]
    assert "Cleaning" in unused["body"] and "1 of 2 left" in unused["body"]
    value = overview["benefits"]["unused_preventive_value"]
    assert f"${value:,.0f}" in unused["body"]
    assert by_kind["deductible_met"]["severity"] == "success"
    assert "Root canal, crown and two fillings" in by_kind["procedure_planned"]["title"]
    assert body["unread_count"] == 5 and body["app_enabled"] is True
    for n in body["notifications"]:
        assert n["read_at"] is None and n["member_id"] == ids["alex"] and "dedupe_key" not in n


def test_amounts_carry_the_disclaimer_and_never_say_delay(client):
    _hid, ids, heads = family(client)
    for n in get_list(client, ids["alex"], heads["alex"])["notifications"]:
        text = (n["title"] + " " + n["body"]).lower()
        assert "delay" not in text and "postpone" not in text
        if "$" in text:
            assert "this is an estimate" in text


def test_other_members_get_their_own_notifications(client):
    _hid, ids, heads = family(client)
    jordan = get_list(client, ids["jordan"], heads["jordan"])
    assert set(kinds(jordan)) == {"benefits_expiring", "preventive_unused", "upcoming_appointment",
                           "eob_ready"}               # eob_ready: Marc has an unpaid synthetic EOB (B4)
    assert "$1,290 of your yearly maximum is left" in [n["title"] for n in jordan["notifications"]]
    noah = get_list(client, ids["noah"], heads["noah"])
    assert kinds(noah) == ["reminder"]       # coverage is pending: no benefit notifications, his reminder shows


def test_window_defaults_to_45_days(monkeypatch):
    from app.notifications import notify_window_days
    monkeypatch.delenv("NOTIFY_WINDOW_DAYS", raising=False)
    assert notify_window_days() == 45
    for bad in ("abc", "-3", ""):
        monkeypatch.setenv("NOTIFY_WINDOW_DAYS", bad)
        assert notify_window_days() == 45


def test_upcoming_appointment_only_within_the_window(client, store, monkeypatch):
    monkeypatch.setenv("NOTIFY_WINDOW_DAYS", "14")
    _hid, ids, heads = family(client)
    add_appointment(store, ids["alex"], "2026-11-10")                              # in 9 days
    add_appointment(store, ids["alex"], "2026-11-15", title="Far enough")           # in 14 days: included
    add_appointment(store, ids["alex"], "2026-11-16", title="Too far")              # in 15 days
    add_appointment(store, ids["alex"], "2026-10-31", title="Already past")         # yesterday
    body = get_list(client, ids["alex"], heads["alex"])
    titles = [n["title"] for n in body["notifications"] if n["kind"] == "upcoming_appointment"]
    assert sorted(titles) == ["Cleaning and exam on Nov 10", "Far enough on Nov 15"]
    first = next(n for n in body["notifications"] if n["title"].endswith("Nov 10"))
    assert "in 9 days" in first["body"] and first["severity"] == "info"
    # With the 14-day window the seeded reminder (44 days away) is not generated.
    assert "reminder" not in kinds(body)


def test_demo_seed_items_show_with_default_settings(client):
    _hid, ids, heads = family(client)
    jordan = get_list(client, ids["jordan"], heads["jordan"])
    appt = next(n for n in jordan["notifications"] if n["kind"] == "upcoming_appointment")
    assert appt["title"] == "Cleaning and exam on Nov 18" and appt["severity"] == "info"
    assert "in 17 days" in appt["body"] and appt["link"] == "/"
    # Sophia is a managed child: the primary sees her notification.
    maya = get_list(client, ids["maya"], heads["jordan"])
    assert "Checkup and cleaning on Dec 4" in [n["title"] for n in maya["notifications"]]
    noah = get_list(client, ids["noah"], heads["noah"])
    rem = noah["notifications"][0]
    assert rem["kind"] == "reminder" and rem["title"] == "Reminder: Send student enrollment proof, due Nov 30"
    assert rem["severity"] == "info" and rem["link"] == "/" and "in 29 days" in rem["body"]
    assert "Needed to keep Hannah covered." in rem["body"]
    alex = get_list(client, ids["alex"], heads["alex"])
    arem = next(n for n in alex["notifications"] if n["kind"] == "reminder")
    assert arem["title"] == "Reminder: Use your remaining benefits, due Dec 15"
    assert "this is an estimate" in arem["body"].lower()       # the note has a dollar amount


def test_items_outside_the_window_are_not_generated(client, store):
    _hid, ids, heads = family(client)
    add_appointment(store, ids["alex"], "2026-12-16", title="Edge of window")         # 45 days: included
    add_appointment(store, ids["alex"], "2026-12-17", title="Way too far")            # 46 days: not
    add_appointment(store, ids["alex"], "2026-12-17", kind="reminder", title="Also far")
    add_appointment(store, ids["alex"], "2026-10-30", kind="reminder", title="Past due")
    titles = [n["title"] for n in get_list(client, ids["alex"], heads["alex"])["notifications"]]
    assert "Edge of window on Dec 16" in titles
    assert not any("Way too far" in t or "Also far" in t or "Past due" in t for t in titles)


def test_window_can_be_changed_with_the_environment(client, monkeypatch):
    monkeypatch.setenv("NOTIFY_WINDOW_DAYS", "20")
    _hid, ids, heads = family(client)
    assert "upcoming_appointment" in kinds(get_list(client, ids["jordan"], heads["jordan"]))   # 17 days
    assert get_list(client, ids["noah"], heads["noah"])["notifications"] == []                # 29 days
    monkeypatch.setenv("NOTIFY_WINDOW_DAYS", "60")
    assert kinds(get_list(client, ids["noah"], heads["noah"])) == ["reminder"]                # now in


def test_due_within_7_days_is_a_warning(client, store):
    _hid, ids, heads = family(client)
    add_appointment(store, ids["alex"], "2026-11-08", title="Soon")                    # 7 days
    add_appointment(store, ids["alex"], "2026-11-09", title="Next week")               # 8 days
    add_appointment(store, ids["alex"], "2026-11-03", kind="reminder", title="Call the office")
    by_title = {n["title"]: n for n in get_list(client, ids["alex"], heads["alex"])["notifications"]}
    assert by_title["Soon on Nov 8"]["severity"] == "warning"
    assert by_title["Next week on Nov 9"]["severity"] == "info"
    r = by_title["Reminder: Call the office, due Nov 3"]
    assert r["kind"] == "reminder" and r["severity"] == "warning" and "in 2 days" in r["body"]


def test_reminder_is_deduped_and_can_be_muted(client):
    _hid, ids, heads = family(client)
    first = get_list(client, ids["noah"], heads["noah"])
    again = get_list(client, ids["noah"], heads["noah"])
    assert first["notifications"] and [n["id"] for n in first["notifications"]] == [n["id"] for n in again["notifications"]]
    r = client.put(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"],
                   json={"app": True, "types": ["reminder", "deductible_met"]})
    assert r.status_code == 200 and r.json()["types"] == ["reminder", "deductible_met"]
    assert sorted(kinds(get_list(client, ids["alex"], heads["alex"]))) == ["deductible_met", "reminder"]


def test_reminder_kind_is_accepted_by_the_database(store):
    with connect(store.path) as c:
        c.execute("INSERT INTO notifications (member_id, kind, title, body, dedupe_key, created_at) "
                  "VALUES ('m-alex','reminder','t','b','reminder:1','2026-11-01T00:00:00+00:00')")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO notifications (member_id, kind, title, body, dedupe_key, created_at) "
                      "VALUES ('m-alex','nope','t','b','x','2026-11-01T00:00:00+00:00')")


def test_past_appointments_are_not_generated(client, store):
    _hid, ids, heads = family(client)
    with connect(store.path) as c:
        c.execute("UPDATE appointments SET due_date = '2026-10-01' WHERE member_id = ?", (ids["jordan"],))
        c.commit()
    assert "upcoming_appointment" not in kinds(get_list(client, ids["jordan"], heads["jordan"]))


def test_generation_is_idempotent_even_after_reading(client):
    _hid, ids, heads = family(client)
    first = get_list(client, ids["alex"], heads["alex"])
    again = get_list(client, ids["alex"], heads["alex"])
    assert [n["id"] for n in first["notifications"]] == [n["id"] for n in again["notifications"]]
    client.post(f"/members/{ids['alex']}/notifications/read-all", headers=heads["alex"])
    third = get_list(client, ids["alex"], heads["alex"])
    assert len(third["notifications"]) == len(first["notifications"]) and third["unread_count"] == 0
    # The overview also generates; it never creates a second copy.
    client.get(f"/members/{ids['alex']}/overview", headers=heads["alex"])
    assert len(get_list(client, ids["alex"], heads["alex"])["notifications"]) == len(first["notifications"])


def test_new_appointment_creates_one_more_notification_later(client, store):
    _hid, ids, heads = family(client)
    n0 = len(get_list(client, ids["alex"], heads["alex"])["notifications"])
    add_appointment(store, ids["alex"], "2026-11-05")
    body = get_list(client, ids["alex"], heads["alex"])
    assert len(body["notifications"]) == n0 + 1
    assert body["notifications"][0]["kind"] == "upcoming_appointment"          # newest first


# ---- read state ----------------------------------------------------------------------------

def test_mark_one_read_and_unread_filter(client):
    _hid, ids, heads = family(client)
    body = get_list(client, ids["alex"], heads["alex"])
    nid = body["notifications"][0]["id"]
    r = client.post(f"/members/{ids['alex']}/notifications/{nid}/read", headers=heads["alex"])
    assert r.status_code == 200 and r.json()["read_at"] is not None and r.json()["id"] == nid
    again = client.post(f"/members/{ids['alex']}/notifications/{nid}/read", headers=heads["alex"])
    assert again.json()["read_at"] == r.json()["read_at"]                        # reading twice changes nothing
    unread = get_list(client, ids["alex"], heads["alex"], "?unread=1")
    assert nid not in [n["id"] for n in unread["notifications"]]
    assert len(unread["notifications"]) == unread["unread_count"] == 4
    assert len(get_list(client, ids["alex"], heads["alex"])["notifications"]) == 5


def test_read_all(client):
    _hid, ids, heads = family(client)
    get_list(client, ids["alex"], heads["alex"])
    r = client.post(f"/members/{ids['alex']}/notifications/read-all", headers=heads["alex"])
    assert r.status_code == 200 and r.json() == {"ok": True, "marked": 5, "unread_count": 0}
    assert get_list(client, ids["alex"], heads["alex"], "?unread=1")["notifications"] == []
    assert client.post(f"/members/{ids['alex']}/notifications/read-all",
                       headers=heads["alex"]).json()["marked"] == 0


def test_read_unknown_notification_and_someone_elses_is_404(client):
    _hid, ids, heads = family(client)
    get_list(client, ids["alex"], heads["alex"])
    get_list(client, ids["jordan"], heads["jordan"])
    jordan_nid = get_list(client, ids["jordan"], heads["jordan"])["notifications"][0]["id"]
    assert client.post(f"/members/{ids['alex']}/notifications/99999/read",
                       headers=heads["alex"]).status_code == 404
    # Marc's notification id through AC's member path is not found.
    assert client.post(f"/members/{ids['alex']}/notifications/{jordan_nid}/read",
                       headers=heads["jordan"]).status_code == 404


# ---- preferences ---------------------------------------------------------------------------

def test_default_prefs_and_seed_rows(client, store):
    _hid, ids, heads = family(client)
    r = client.get(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"])
    assert r.status_code == 200
    assert r.json() == {"app": True, "email": False, "sms": False, "types": None,
                        "email_on_file": True, "phone_on_file": True}
    seed = {p["member_id"]: p for p in load_seed_file()["notification_prefs"]}
    assert set(seed) == {"m-jordan", "m-alex", "m-maya", "m-noah"}
    assert all(p["app"] == 1 and p["email"] == 0 and p["sms"] == 0 for p in seed.values())
    maya = client.get(f"/members/{ids['maya']}/notification-prefs", headers=heads["jordan"]).json()
    assert maya["email_on_file"] is False and maya["phone_on_file"] is False


def test_email_and_sms_need_a_contact_on_file(client):
    _hid, ids, heads = family(client)
    for key, word in (("email", "email address"), ("sms", "phone number")):
        r = client.put(f"/members/{ids['maya']}/notification-prefs", headers=heads["jordan"],
                       json={"app": True, "email": key == "email", "sms": key == "sms"})
        assert r.status_code == 422
        assert word in r.json()["detail"] and "Sophia" in r.json()["detail"]
    ok = client.put(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"],
                    json={"app": True, "email": True, "sms": True})
    assert ok.status_code == 200 and ok.json()["email"] is True and ok.json()["sms"] is True
    client.patch(f"/members/{ids['alex']}/profile", headers=heads["alex"], json={"email": None})
    bad = client.put(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"],
                     json={"app": True, "email": True, "sms": False})
    assert bad.status_code == 422


def test_prefs_validation_of_body(client):
    _hid, ids, heads = family(client)
    r = client.put(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"],
                   json={"app": True, "types": ["not_a_kind"]})
    assert r.status_code == 422
    r = client.put(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"],
                   json={"app": True, "types": ["deductible_met", "benefits_expiring"]})
    assert r.status_code == 200 and r.json()["types"] == ["benefits_expiring", "deductible_met"]


def test_muted_kinds_are_not_generated(client):
    _hid, ids, heads = family(client)
    client.put(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"],
               json={"app": True, "types": ["deductible_met"]})
    assert kinds(get_list(client, ids["alex"], heads["alex"])) == ["deductible_met"]


def test_app_channel_off_hides_the_list_and_the_overview_count(client):
    _hid, ids, heads = family(client)
    assert client.get(f"/members/{ids['alex']}/overview", headers=heads["alex"]).json()["notifications_unread"] == 5
    client.put(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"], json={"app": False})
    body = get_list(client, ids["alex"], heads["alex"])
    assert body == {"notifications": [], "unread_count": 0, "app_enabled": False}
    assert client.get(f"/members/{ids['alex']}/overview", headers=heads["alex"]).json()["notifications_unread"] == 0


def test_overview_has_unread_count(client):
    _hid, ids, heads = family(client)
    ov = client.get(f"/members/{ids['jordan']}/overview", headers=heads["jordan"]).json()
    assert ov["notifications_unread"] == 4   # includes eob_ready (B4)
    client.post(f"/members/{ids['jordan']}/notifications/read-all", headers=heads["jordan"])
    assert client.get(f"/members/{ids['jordan']}/overview", headers=heads["jordan"]).json()["notifications_unread"] == 0


# ---- delivery previews ---------------------------------------------------------------------

def test_no_previews_when_email_and_sms_are_off(client, store):
    _hid, ids, heads = family(client)
    get_list(client, ids["alex"], heads["alex"])
    assert client.get(f"/members/{ids['alex']}/outbox", headers=heads["alex"]).json() == []
    assert rows(store, "SELECT * FROM outbox") == []


def test_previews_written_only_when_enabled_and_never_sent(client, store):
    _hid, ids, heads = family(client)
    # Email on; SMS is no longer a delivery channel, so turning it on changes nothing.
    client.put(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"],
               json={"app": True, "email": True, "sms": True})
    body = get_list(client, ids["alex"], heads["alex"])
    out = client.get(f"/members/{ids['alex']}/outbox", headers=heads["alex"]).json()
    # With no push subscription, there is exactly one email preview per notification and no SMS.
    assert len(out) == len(body["notifications"])
    assert {m["status"] for m in out} == {"preview"}   # default notifier sends nothing
    emails = [m for m in out if m["channel"] == "email"]
    assert not [m for m in out if m["channel"] == "sms"]
    assert {m["to_address"] for m in emails} == {"ac.halog@example.test"}
    assert all(m["subject"] for m in emails)
    titles = {n["title"] for n in body["notifications"]}
    assert {m["subject"] for m in emails} == titles
    # Reading again creates no new previews (only new notifications do).
    get_list(client, ids["alex"], heads["alex"])
    assert len(client.get(f"/members/{ids['alex']}/outbox", headers=heads["alex"]).json()) == len(out)


def test_only_the_enabled_channel_gets_previews(client):
    _hid, ids, heads = family(client)
    client.put(f"/members/{ids['jordan']}/notification-prefs", headers=heads["jordan"],
               json={"app": True, "email": True, "sms": False})
    get_list(client, ids["jordan"], heads["jordan"])
    out = client.get(f"/members/{ids['jordan']}/outbox", headers=heads["jordan"]).json()
    assert out and {m["channel"] for m in out} == {"email"}


def test_outbox_status_allows_real_delivery_states_but_rejects_unknown(client, store):
    _hid, ids, _heads = family(client)
    with connect(store.path) as c:
        for status in ("preview", "queued", "sent", "failed"):
            c.execute("INSERT INTO outbox (member_id, channel, to_address, body, created_at, status) "
                      "VALUES (?, 'email', 'a@example.test', 'x', '2026-11-01T00:00:00+00:00', ?)",
                      (ids["alex"], status))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO outbox (member_id, channel, to_address, body, created_at, status) "
                      "VALUES (?, 'email', 'a@example.test', 'x', '2026-11-01T00:00:00+00:00', 'bogus')",
                      (ids["alex"],))


def test_notifier_default_is_the_preview_notifier(store):
    from app.notifier import OutboundMessage, PreviewNotifier, get_notifier
    assert isinstance(get_notifier(store), PreviewNotifier)
    with connect(store.path) as c:
        c.execute("INSERT INTO members (id, household_id, name, relationship, age, role, has_login) "
                  "VALUES ('m-tmp','hh-rivera','Tmp','other',30,'adult',0)")
        c.commit()
    row = PreviewNotifier(store).deliver(OutboundMessage("m-tmp", "sms", "3345550100", None, "hi"))
    assert row["status"] == "preview" and row["to_address"] == "3345550100"


def test_test_route_app_email_sms(client):
    _hid, ids, heads = family(client)
    r = client.post(f"/members/{ids['alex']}/notifications/test", headers=heads["alex"], json={"channel": "app"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["channel"] == "app" and d["outbox"] is None
    assert d["notification"]["kind"] == "test" and d["notification"]["read_at"] is None
    client.post(f"/members/{ids['alex']}/notifications/test", headers=heads["alex"], json={"channel": "app"})
    assert kinds(get_list(client, ids["alex"], heads["alex"])).count("test") == 2
    e = client.post(f"/members/{ids['alex']}/notifications/test", headers=heads["alex"],
                    json={"channel": "email"}).json()
    assert e["outbox"]["channel"] == "email" and e["outbox"]["status"] == "preview"
    assert e["outbox"]["to_address"] == "ac.halog@example.test" and e["notification"] is None
    s = client.post(f"/members/{ids['alex']}/notifications/test", headers=heads["alex"],
                    json={"channel": "sms"}).json()
    assert s["outbox"]["to_address"] == "3345550143" and s["outbox"]["subject"] is None
    assert len(client.get(f"/members/{ids['alex']}/outbox", headers=heads["alex"]).json()) == 2


def test_test_route_needs_a_contact_and_a_valid_channel(client):
    _hid, ids, heads = family(client)
    r = client.post(f"/members/{ids['maya']}/notifications/test", headers=heads["jordan"], json={"channel": "sms"})
    assert r.status_code == 422 and "phone number" in r.json()["detail"]
    r = client.post(f"/members/{ids['alex']}/notifications/test", headers=heads["alex"], json={"channel": "fax"})
    assert r.status_code == 422


def test_test_route_is_rate_limited(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("RATE_COMPUTE_PER_MINUTE", "2")
    ratelimit.reset_for_tests()
    _hid, ids, heads = family(client)
    codes = [client.post(f"/members/{ids['alex']}/notifications/test", headers=heads["alex"],
                         json={"channel": "app"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# ---- access --------------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("get", "/members/m-alex/notifications"),
    ("post", "/members/m-alex/notifications/read-all"),
    ("post", "/members/m-alex/notifications/1/read"),
    ("get", "/members/m-alex/notification-prefs"),
    ("get", "/members/m-alex/outbox"),
])
def test_requires_sign_in(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.put("/members/m-alex/notification-prefs", json={"app": True}).status_code == 401
    assert client.post("/members/m-alex/notifications/test", json={"channel": "app"}).status_code == 401


def test_adult_sees_only_themself_primary_sees_everyone(client):
    _hid, ids, heads = family(client)
    for path in ("notifications", "notification-prefs", "outbox"):
        assert client.get(f"/members/{ids['jordan']}/{path}", headers=heads["alex"]).status_code == 403
    assert client.post(f"/members/{ids['jordan']}/notifications/read-all", headers=heads["alex"]).status_code == 403
    assert client.put(f"/members/{ids['jordan']}/notification-prefs", headers=heads["alex"],
                      json={"app": True}).status_code == 403
    assert client.post(f"/members/{ids['jordan']}/notifications/test", headers=heads["alex"],
                       json={"channel": "app"}).status_code == 403
    for who in ("alex", "maya", "noah"):                      # the primary may act for everyone, Sophia included
        assert client.get(f"/members/{ids[who]}/notifications", headers=heads["jordan"]).status_code == 200
        assert client.get(f"/members/{ids[who]}/notification-prefs", headers=heads["jordan"]).status_code == 200
    assert client.put(f"/members/{ids['maya']}/notification-prefs", headers=heads["jordan"],
                      json={"app": True, "types": ["benefits_expiring"]}).status_code == 200


def test_unknown_member_is_404(client):
    _hid, _ids, heads = family(client)
    for path in ("notifications", "notification-prefs", "outbox"):
        assert client.get(f"/members/m-nobody/{path}", headers=heads["jordan"]).status_code == 404
    assert client.put("/members/m-nobody/notification-prefs", headers=heads["jordan"],
                      json={"app": True}).status_code == 404
    assert client.post("/members/m-nobody/notifications/test", headers=heads["jordan"],
                       json={"channel": "app"}).status_code == 404


def test_another_family_cannot_be_reached(client):
    _hid, ids, _heads = family(client)
    _hid2, _ids2, heads2 = family(client)
    assert client.get(f"/members/{ids['alex']}/notifications", headers=heads2["jordan"]).status_code == 403


def test_contact_details_stay_out_of_notification_text(client):
    _hid, ids, heads = family(client)
    blob = json.dumps(get_list(client, ids["alex"], heads["alex"]))
    assert "example.test" not in blob and "3345550143" not in blob
    assert "example.test" not in client.get(f"/members/{ids['alex']}/notification-prefs",
                                            headers=heads["alex"]).text


# ---- template family, sandboxes, delete, reset ---------------------------------------------

def test_template_family_can_read_but_not_change_settings(client):
    h = template_headers(client)
    body = get_list(client, "m-alex", h)
    assert "benefits_expiring" in kinds(body)
    nid = body["notifications"][0]["id"]
    assert client.post(f"/members/m-alex/notifications/{nid}/read", headers=h).status_code == 200
    assert client.get("/members/m-alex/notification-prefs", headers=h).status_code == 200
    assert client.get("/members/m-alex/outbox", headers=h).json() == []
    put = client.put("/members/m-alex/notification-prefs", headers=h, json={"app": True, "email": True})
    assert put.status_code == 403 and "start your own demo family" in put.json()["detail"]
    test = client.post("/members/m-alex/notifications/test", headers=h, json={"channel": "email"})
    assert test.status_code == 403


def test_sandbox_clone_copies_prefs_not_notifications(client, store):
    _hid, ids, heads = family(client)
    get_list(client, ids["alex"], heads["alex"])
    prefs = rows(store, "SELECT member_id, app, email, sms FROM notification_prefs WHERE member_id LIKE '%.%'")
    assert len(prefs) == 4 and all(p["app"] == 1 and p["email"] == 0 and p["sms"] == 0 for p in prefs)
    _h2, ids2, heads2 = family(client)
    assert rows(store, "SELECT COUNT(*) AS n FROM notifications WHERE member_id = ?", (ids2["alex"],))[0]["n"] == 0
    assert len(get_list(client, ids2["alex"], heads2["alex"])["notifications"]) == 5   # independent copy
    # The sandbox's own read state does not touch the other family's.
    client.post(f"/members/{ids2['alex']}/notifications/read-all", headers=heads2["alex"])
    assert get_list(client, ids["alex"], heads["alex"])["unread_count"] == 5


def test_removing_a_member_deletes_their_notification_rows(client, store):
    hid, ids, heads = family(client)
    client.put(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"],
               json={"app": True, "email": True, "sms": False})
    get_list(client, ids["alex"], heads["alex"])
    assert rows(store, "SELECT COUNT(*) AS n FROM outbox WHERE member_id = ?", (ids["alex"],))[0]["n"] > 0
    r = client.delete(f"/households/{hid}/members/{ids['alex']}", headers=heads["jordan"])
    assert r.status_code == 200, r.text
    for table in ("notifications", "outbox", "notification_prefs"):
        assert rows(store, f"SELECT COUNT(*) AS n FROM {table} WHERE member_id = ?", (ids["alex"],))[0]["n"] == 0
    assert rows(store, "SELECT COUNT(*) AS n FROM notifications WHERE member_id = ?", (ids["jordan"],)) is not None


def test_sandbox_expiry_cleanup_removes_notification_rows(client, store):
    from app.db import sandbox as sb
    hid, ids, heads = family(client)
    get_list(client, ids["alex"], heads["alex"])
    with connect(store.path) as c:
        sb._delete_household_rows(c, hid, keep_household=False)
        c.commit()
    left = rows(store, "SELECT COUNT(*) AS n FROM notifications WHERE member_id LIKE '%.%'")[0]["n"]
    assert left == 0
    assert rows(store, "SELECT COUNT(*) AS n FROM notification_prefs WHERE member_id LIKE '%.%'")[0]["n"] == 0


def test_new_members_default_to_app_on(client):
    hid, _ids, heads = family(client)
    r = client.post(f"/households/{hid}/members", headers=heads["jordan"],
                    json={"name": "Sam", "relationship": "other", "dob": "1990-01-01",
                          "email": "sam@example.test"})
    assert r.status_code == 201
    mid = r.json()["id"]
    p = client.get(f"/members/{mid}/notification-prefs", headers=heads["jordan"]).json()
    assert p["app"] is True and p["email"] is False and p["email_on_file"] is True
    assert len(get_list(client, mid, heads["jordan"])["notifications"]) >= 1


def test_reset_restores_default_prefs_and_clears_notifications(client, store):
    _hid, ids, heads = family(client)
    client.put(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"],
               json={"app": False, "email": True, "sms": True, "types": ["deductible_met"]})
    client.post(f"/members/{ids['alex']}/notifications/test", headers=heads["alex"], json={"channel": "email"})
    assert client.post("/demo/reset", headers=heads["jordan"]).status_code == 200
    p = client.get(f"/members/{ids['alex']}/notification-prefs", headers=heads["alex"]).json()
    assert (p["app"], p["email"], p["sms"], p["types"]) == (True, False, False, None)
    assert rows(store, "SELECT COUNT(*) AS n FROM outbox WHERE member_id LIKE '%.%'")[0]["n"] == 0
    assert rows(store, "SELECT COUNT(*) AS n FROM notifications WHERE member_id = ?", (ids["alex"],))[0]["n"] == 0
    assert len(get_list(client, ids["alex"], heads["alex"])["notifications"]) == 5


def test_migration_006_applies_to_an_older_database(tmp_path):
    from app.db import core
    path = tmp_path / "old.db"
    real = core.MIGRATIONS_DIR
    older = tmp_path / "mig"
    older.mkdir()
    for f in sorted(real.glob("00[1-5]_*.sql")):
        (older / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    core.MIGRATIONS_DIR = older
    try:
        core.migrate(path)
    finally:
        core.MIGRATIONS_DIR = real
    assert core.migrate(path) == ["006_notifications.sql", "007_notification_reminder_kind.sql",
                                  "008_providers.sql", "009_reports.sql", "010_real_delivery.sql"]
    with connect(path) as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"notification_prefs", "notifications", "outbox", "push_subscriptions"} <= names


def test_unique_member_and_dedupe_key(store):
    with connect(store.path) as c:
        c.execute("INSERT INTO notifications (member_id, kind, title, body, dedupe_key, created_at) "
                  "VALUES ('m-alex','test','t','b','k','2026-11-01T00:00:00+00:00')")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO notifications (member_id, kind, title, body, dedupe_key, created_at) "
                      "VALUES ('m-alex','test','t2','b2','k','2026-11-01T00:00:00+00:00')")
        c.execute("INSERT INTO notifications (member_id, kind, title, body, dedupe_key, created_at) "
                  "VALUES ('m-jordan','test','t','b','k','2026-11-01T00:00:00+00:00')")  # other member: fine


def test_golden_numbers_unaffected(client):
    """Generating notifications never changes usage or the engine's numbers."""
    _hid, ids, heads = family(client)
    before = client.get(f"/members/{ids['alex']}/overview", headers=heads["alex"]).json()
    get_list(client, ids["alex"], heads["alex"])
    after = client.get(f"/members/{ids['alex']}/overview", headers=heads["alex"]).json()
    for k in ("usage", "benefits"):
        assert before[k] == after[k]
    assert after["benefits"]["max_remaining"] == 400 and after["usage"]["max_used"] == 1100
    est = client.post("/estimate", json={"code": "D1110", "plan_id": "demo_ppo"})
    if est.status_code == 200:
        assert est.json()["you_pay"] == 0                      # G1
