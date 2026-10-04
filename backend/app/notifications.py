"""Generates a person's notifications from their data, deterministically and idempotently.

Every dollar amount comes from the benefits engine (`benefits_status`); nothing here computes
money. Each notification has a `dedupe_key` (plan year, appointment id or saved plan id), so
generating again never creates a duplicate, even after the person has read the first one.

Kinds generated today: benefits_expiring, preventive_unused, upcoming_appointment, reminder,
procedure_planned, deductible_met, claim_update, eob_ready. Appointments and reminders show up to `NOTIFY_WINDOW_DAYS`
(default 45) ahead of the demo clock; anything due in 7 days or fewer is a warning. `claim_update` (a denied or pending claim) and `eob_ready` (an EOB with an
unpaid balance) come from the person's stored reports and are keyed per document. Members whose coverage is pending get no benefit notifications.
Wording is calm and plain, never tells anyone to put off urgent care, and carries the estimate
disclaimer wherever an amount appears.
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

from .data import load_catalog, load_plans
from .db import DEMO_TODAY, Store
from .engine.estimate import money
from .engine.status import benefits_status
from .models import Usage
from .notifier import OutboundMessage, get_notifier

KINDS = ["benefits_expiring", "preventive_unused", "upcoming_appointment", "reminder",
         "procedure_planned", "deductible_met", "claim_update", "eob_ready", "test"]
GENERATED_KINDS = ["benefits_expiring", "preventive_unused", "upcoming_appointment", "reminder",
                   "procedure_planned", "deductible_met", "claim_update", "eob_ready"]
DEFAULT_NOTIFY_WINDOW_DAYS = 45   # appointments and reminders this many days ahead (or fewer) get a notification
SOON_DAYS = 7                     # due within this many days: severity "warning"
EXPIRY_MONTHS = 3          # "plan year ending soon": this many months left or fewer
DISCLAIMER = "This is an estimate. Your actual cost depends on your dentist's charges and claim review."
URGENT = "If something hurts or feels urgent, call your dentist now; don't wait."


def notify_window_days() -> int:
    """`NOTIFY_WINDOW_DAYS` from the environment (default 45); a bad or negative value falls back."""
    try:
        n = int(os.environ.get("NOTIFY_WINDOW_DAYS", DEFAULT_NOTIFY_WINDOW_DAYS))
    except ValueError:
        return DEFAULT_NOTIFY_WINDOW_DAYS
    return n if n >= 0 else DEFAULT_NOTIFY_WINDOW_DAYS


def _short_date(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _days_phrase(days: int) -> str:
    return "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"


def build_notifications(*, member: dict[str, Any], plan_id: str, usage_row: dict[str, Any],
                        appointments: list[dict[str, Any]], saved_plans: list[dict[str, Any]],
                        reports: list[dict[str, Any]] | None = None, today: date = DEMO_TODAY) -> list[dict[str, Any]]:
    """The notifications this person should have right now (a pure function of the data)."""
    out: list[dict[str, Any]] = []
    year = today.year
    plan = load_plans().get(plan_id)
    catalog = load_catalog()
    if plan is not None and member.get("status") != "pending":
        usage = Usage(max_used=usage_row["max_used_cents"] / 100,
                      deductible_met=usage_row["deductible_met_cents"] / 100,
                      history=["D1110"] * usage_row["cleanings_used"])
        st = benefits_status(plan, usage, catalog, today.month)
        soon = st.months_left <= EXPIRY_MONTHS
        if soon and st.max_remaining > 0:
            out.append({
                "kind": "benefits_expiring", "severity": "warning", "link": "/",
                "dedupe_key": f"benefits_expiring:{year}",
                "title": f"{money(st.max_remaining)} of your yearly maximum is left",
                "body": (f"Your plan year ends Dec 31 ({st.months_left} month(s) left, counting this one). "
                         f"The {money(st.max_remaining)} left of your {money(st.annual_max)} yearly maximum "
                         "does not carry over. If you need care, booking it before then lets your plan help "
                         f"pay. {URGENT} {DISCLAIMER}")})
        left = [f for f in st.frequencies
                if f.remaining > 0 and f.code in catalog and catalog[f.code].category == "preventive"]
        if soon and left:
            lines = "; ".join(f"{f.name}: {f.remaining} of {f.limit} left" for f in left)
            out.append({
                "kind": "preventive_unused", "severity": "info", "link": "/plans",
                "dedupe_key": f"preventive_unused:{year}",
                "title": "You still have covered checkups and cleanings this year",
                "body": (f"{lines}. That is about {money(st.unused_preventive_value)} of covered care "
                         f"that goes away on Jan 1. {DISCLAIMER}")})
        if st.deductible > 0 and st.deductible_met > 0 and st.deductible_remaining == 0:
            out.append({
                "kind": "deductible_met", "severity": "success", "link": "/",
                "dedupe_key": f"deductible_met:{year}",
                "title": "Your deductible is met for this year",
                "body": (f"You have paid your {money(st.deductible)} deductible, so your plan starts "
                         f"sharing the cost of covered care right away for the rest of {year}. {DISCLAIMER}")})
    window = notify_window_days()
    for a in appointments:
        if a.get("kind") not in ("appointment", "reminder"):
            continue
        due = date.fromisoformat(a["due_date"])
        days = (due - today).days
        if not 0 <= days <= window:
            continue
        note = f" {a['note']}" if a.get("note") else ""
        if "$" in note:                       # a dollar amount in a note carries the estimate disclaimer
            note += f" {DISCLAIMER}"
        severity = "warning" if days <= SOON_DAYS else "info"
        if a["kind"] == "appointment":
            out.append({
                "kind": "upcoming_appointment", "severity": severity, "link": "/",
                "dedupe_key": f"upcoming_appointment:{a['id']}",
                "title": f"{a['title']} on {_short_date(due)}",
                "body": f"Your appointment is {_days_phrase(days)}, on {_short_date(due)}.{note}"})
        else:
            out.append({
                "kind": "reminder", "severity": severity, "link": "/",
                "dedupe_key": f"reminder:{a['id']}",
                "title": f"Reminder: {a['title']}, due {_short_date(due)}",
                "body": f"This is due {_days_phrase(days)}, on {_short_date(due)}.{note}"})
    for sp in saved_plans:
        n = len(sp.get("items") or [])
        what = f"{n} treatment(s)" if n else "a treatment plan"
        out.append({
            "kind": "procedure_planned", "severity": "info", "link": "/plan-year",
            "dedupe_key": f"procedure_planned:{sp['id']}",
            "title": f"Planned treatment: {sp['name']}",
            "body": (f"You saved {what}. Open Plan My Year to see when to schedule each one to pay the "
                     f"least. {URGENT} {DISCLAIMER}")})
    for r in reports or []:
        d = r["data"]
        if r["kind"] == "claim" and d.get("status") in ("denied", "pending"):
            denied = d["status"] == "denied"
            out.append({
                "kind": "claim_update", "severity": "warning" if denied else "info", "link": "/reports",
                "dedupe_key": f"claim_update:{r['id']}",
                "title": f"Claim {'denied' if denied else 'still pending'}: {r['description']}",
                "body": (f"Your plan {'did not pay' if denied else 'is still reviewing'} the claim from "
                         f"{r['provider_name']} on {_short_date(date.fromisoformat(r['service_date']))}. "
                         "Open Reports to see what it means and what you can do. "
                         f"{URGENT} This is made-up demo data. {DISCLAIMER}")})
        elif r["kind"] == "eob" and r["paid_status"] == "unpaid" and d.get("you_owe_cents", 0) > 0:
            owed = money(d["you_owe_cents"] / 100)
            out.append({
                "kind": "eob_ready", "severity": "info", "link": "/reports",
                "dedupe_key": f"eob_ready:{r['id']}",
                "title": f"Your EOB is ready: you owe {owed}",
                "body": (f"The explanation of benefits for {r['description'].lower()} at {r['provider_name']} "
                         f"shows you owe {owed}. Open Reports to see how it was worked out. "
                         f"This is made-up demo data. {DISCLAIMER}")})
    return out


def sync_notifications(store: Store, viewer: str, member_id: str) -> list[dict[str, Any]]:
    """Generate this person's notifications (only new ones are stored) and, for each new one, write
    a delivery preview if email or text is turned on and a contact is on file. Returns the new
    notification rows."""
    member = store.get_member(viewer, member_id)
    prefs = store.get_notification_prefs(viewer, member_id)
    wanted = build_notifications(
        member=member, plan_id=store.household_plan_id(viewer, member_id),
        usage_row=store.get_member_usage(viewer, member_id),
        appointments=store.list_upcoming_schedule(viewer, member_id),
        saved_plans=store.list_saved_plans(viewer, member_id),
        reports=store.list_report_items(viewer, member_id))
    if prefs["types"] is not None:
        wanted = [n for n in wanted if n["kind"] in prefs["types"]]
    created = store.add_notifications(viewer, member_id, wanted)
    notifier = get_notifier(store)
    push_subs = store.list_push_subscriptions(member_id) if prefs["app"] else []
    for n in created:
        text = f"{n['title']}. {n['body']}"
        if prefs["email"] and member.get("email"):
            notifier.deliver(OutboundMessage(member_id, "email", member["email"], n["title"], text))
        # Web push goes to every browser the person turned notifications on in. The whole
        # subscription (endpoint + keys) travels in to_address so the push notifier can use it.
        for sub in push_subs:
            payload = json.dumps({"endpoint": sub["endpoint"],
                                  "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}})
            notifier.deliver(OutboundMessage(member_id, "push", payload, n["title"], text[:320]))
    return created
