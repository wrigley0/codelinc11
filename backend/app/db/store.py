"""Access layer for households, members, usage, history, schedule and context.

Every read and write takes a `viewer_id` (the signed-in member). Visibility:
- the primary account holder can see everyone in their household;
- an adult with a login sees only their own data;
- a managed member has no login and so no viewer rights.
Anything else raises AccessDenied. Context is always filtered by member_id,
so one person's context can never come back under another person's id.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import sandbox
from .core import db_path, load_seed_file, migrate, session

DEMO_TODAY = date.fromisoformat(load_seed_file()["demo_today"])
CLEANING_CODES = {"D1110", "D1120"}


MAX_MEMBERS = 8
MAX_REPORT_ITEMS = 100


def age_on(dob: date, today: date = DEMO_TODAY) -> int:
    """Whole years on `today` (the demo clock by default)."""
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


class AccessDenied(Exception):
    """The viewer may not see or change this member's data."""


class NotFound(Exception):
    pass


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else db_path()
        migrate(self.path)

    # ---- access rules -------------------------------------------------
    def _viewer(self, conn: sqlite3.Connection, viewer_id: str) -> dict[str, Any]:
        v = _row(conn.execute("SELECT * FROM members WHERE id = ?", (viewer_id,)).fetchone())
        if v is None:
            raise NotFound(f"member {viewer_id}")
        if not v["has_login"]:
            raise AccessDenied("this member has no login")
        return v

    def _target(self, conn: sqlite3.Connection, viewer_id: str, member_id: str) -> dict[str, Any]:
        viewer = self._viewer(conn, viewer_id)
        target = _row(conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone())
        if target is None:
            raise NotFound(f"member {member_id}")
        same_house = target["household_id"] == viewer["household_id"]
        if viewer["role"] == "primary" and same_house:
            return target
        if viewer["id"] == target["id"]:
            return target
        raise AccessDenied("you can only see your own information")

    def can_access(self, viewer_id: str, member_id: str) -> bool:
        try:
            with session(self.path) as conn:
                self._target(conn, viewer_id, member_id)
            return True
        except (AccessDenied, NotFound):
            return False

    # ---- demo accounts and households ----------------------------------
    def list_demo_accounts(self, household_id: str | None = None) -> list[dict[str, Any]]:
        """Accounts for the demo sign-in screen (no viewer needed). Without a household id: the
        shared template accounts (sandbox accounts are never listed). With one: that household's."""
        sql = ("SELECT a.id AS account_id, a.email, a.display_name, m.id AS member_id, "
               "m.role, m.household_id, m.status FROM accounts a JOIN members m ON m.id = a.member_id ")
        order = "ORDER BY m.household_id, CASE m.role WHEN 'primary' THEN 0 ELSE 1 END, m.name"
        with session(self.path) as conn:
            if household_id is None:
                rows = conn.execute(sql + "WHERE m.household_id NOT IN (SELECT household_id FROM sandboxes) "
                                    + order).fetchall()
            else:
                rows = conn.execute(sql + "WHERE m.household_id = ? " + order, (household_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- demo sandboxes (see db/sandbox.py) -----------------------------
    def create_sandbox(self) -> dict[str, Any]:
        """Clone the template household for a new visitor: {household_id, expires_at}."""
        return sandbox.create(self.path)

    def get_sandbox(self, household_id: str) -> dict[str, Any] | None:
        """{household_id, expires_at} if that sandbox exists and has not expired, else None."""
        return sandbox.get(self.path, household_id)

    def is_sandbox(self, household_id: str) -> bool:
        with session(self.path) as conn:
            return sandbox.is_sandbox(conn, household_id)

    def reset_household(self, viewer_id: str) -> None:
        """Primary only: restore the caller's own household (sandbox or template) to the template
        state in place. Ids stay the same, so signed-in sessions stay valid. Other households,
        sandboxes included, are not touched."""
        with session(self.path) as conn:
            viewer = self._viewer(conn, viewer_id)
            if viewer["role"] != "primary":
                raise AccessDenied("only the primary account holder can reset the demo")
            sandbox.restore(conn, viewer["household_id"])

    def rename_household(self, viewer_id: str, household_id: str, household_name: str | None,
                         names: dict[str, str]) -> dict[str, Any]:
        """Primary only, sandbox households only: change display names. Ids, roles, ages, plan and
        usage never change. `household_name` is a surname e.g. "Halog"."""
        with session(self.path) as conn:
            viewer = self._viewer(conn, viewer_id)
            if viewer["household_id"] != household_id or viewer["role"] != "primary":
                raise AccessDenied("only the primary account holder can rename the family")
            if not sandbox.is_sandbox(conn, household_id):
                raise AccessDenied("the shared demo family can't be renamed; start your own demo family")
            ids = {r["id"] for r in conn.execute("SELECT id FROM members WHERE household_id = ?",
                                                 (household_id,))}
            for mid in names:
                if mid not in ids:
                    raise NotFound(f"member {mid}")
            for mid, name in names.items():
                conn.execute("UPDATE members SET name = ? WHERE id = ?", (name, mid))
                conn.execute("UPDATE accounts SET display_name = ? WHERE member_id = ?", (name, mid))
            if household_name is not None:
                full = household_name if household_name.lower().endswith("household")                     else f"{household_name} household"
                conn.execute("UPDATE households SET name = ? WHERE id = ?", (full, household_id))
        return self.get_household(viewer_id, household_id)

    # ---- profiles and family members (demo family only) -------------------
    def _editable_target(self, conn: sqlite3.Connection, viewer_id: str, member_id: str) -> dict[str, Any]:
        """Member the viewer may edit: primary edits anyone in the household, an adult only themself.
        Only in a demo (sandbox) family, never in the shared template."""
        target = self._target(conn, viewer_id, member_id)
        if not sandbox.is_sandbox(conn, target["household_id"]):
            raise AccessDenied("the shared demo family can't be edited; start your own demo family")
        return target

    def _primary_of(self, conn: sqlite3.Connection, viewer_id: str, household_id: str) -> dict[str, Any]:
        viewer = self._viewer(conn, viewer_id)
        if viewer["household_id"] != household_id:
            raise AccessDenied("not your household")
        if viewer["role"] != "primary":
            raise AccessDenied("only the primary account holder can add or remove family members")
        if not sandbox.is_sandbox(conn, household_id):
            raise AccessDenied("the shared demo family can't be edited; start your own demo family")
        return viewer

    def check_profile_access(self, viewer_id: str, member_id: str) -> None:
        """Raise AccessDenied or NotFound unless the viewer may edit this profile."""
        with session(self.path) as conn:
            self._editable_target(conn, viewer_id, member_id)

    def check_family_access(self, viewer_id: str, household_id: str) -> None:
        """Raise AccessDenied unless the viewer is the primary of this demo family."""
        with session(self.path) as conn:
            self._primary_of(conn, viewer_id, household_id)

    def update_member_profile(self, viewer_id: str, member_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply already validated profile changes. Keys: name, dob (ISO string), email, phone, zip,
        notes (None clears). Changing dob re-derives `age` and, across the 18 boundary, the role:
        a managed child who turns 18 becomes an adult (no login yet), and an adult without a login
        who becomes under 18 becomes managed. Raises ValueError (plain message) when not allowed."""
        with session(self.path) as conn:
            target = self._editable_target(conn, viewer_id, member_id)
            sets: dict[str, Any] = {k: v for k, v in changes.items()
                                    if k in ("name", "email", "phone", "zip", "notes")}
            if "primary_dentist_id" in changes:
                # No foreign key (SQLite cannot add one to an existing column): checked here.
                pid = changes["primary_dentist_id"]
                if pid is not None and conn.execute("SELECT 1 FROM providers WHERE id = ?", (pid,)).fetchone() is None:
                    raise ValueError("We could not find that dentist in the directory.")
                sets["primary_dentist_id"] = pid
            if "dob" in changes:
                age = age_on(date.fromisoformat(changes["dob"]))
                sets["dob"], sets["age"] = changes["dob"], age
                first = target["name"].split()[0]
                if age >= 18 and target["role"] == "managed":
                    sets["role"] = "adult"
                elif age < 18 and target["role"] != "managed":
                    if target["role"] == "primary":
                        raise ValueError("The primary account holder must be 18 or older.")
                    if target["has_login"]:
                        raise ValueError(f"{first} has a login, and logins are for adults 18 and over. "
                                         "Choose a date of birth that makes them 18 or older.")
                    if target["relationship"] in ("spouse", "partner"):
                        raise ValueError(f"A {target['relationship']} must be 18 or older.")
                    sets["role"] = "managed"
                    conn.execute("UPDATE invites SET status = 'cancelled' WHERE member_id = ? "
                                 "AND status = 'pending'", (member_id,))
            if sets:
                cols = ", ".join(f"{k} = ?" for k in sets)
                conn.execute(f"UPDATE members SET {cols} WHERE id = ?", [*sets.values(), member_id])
            if "name" in sets:
                conn.execute("UPDATE accounts SET display_name = ? WHERE member_id = ?",
                             (sets["name"], member_id))
            return _row(conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone())  # type: ignore[return-value]

    # ---- providers (global reference data, read only) -----------------------
    def list_providers(self) -> list[dict[str, Any]]:
        with session(self.path) as conn:
            return [_provider(r) for r in conn.execute("SELECT * FROM providers ORDER BY id")]

    def get_provider(self, provider_id: str) -> dict[str, Any]:
        with session(self.path) as conn:
            r = conn.execute("SELECT * FROM providers WHERE id = ?", (provider_id,)).fetchone()
        if r is None:
            raise NotFound(f"provider {provider_id}")
        return _provider(r)

    def zip_centroid(self, zip_code: str) -> dict[str, Any] | None:
        with session(self.path) as conn:
            r = conn.execute("SELECT * FROM zip_centroids WHERE zip = ?", (zip_code,)).fetchone()
        return dict(r) if r else None

    def add_member(self, viewer_id: str, household_id: str, name: str, relationship: str, dob: str,
                   email: str | None = None, phone: str | None = None,
                   zip_code: str | None = None) -> dict[str, Any]:
        """Primary adds a person to their own demo family. Under 18: managed. Otherwise: an adult
        without a login. Starts with zero usage. Raises ValueError (plain message) when not allowed."""
        with session(self.path) as conn:
            self._primary_of(conn, viewer_id, household_id)
            count = conn.execute("SELECT COUNT(*) FROM members WHERE household_id = ?",
                                 (household_id,)).fetchone()[0]
            if count >= MAX_MEMBERS:
                raise ValueError(f"A family can have up to {MAX_MEMBERS} people. Remove someone first.")
            age = age_on(date.fromisoformat(dob))
            if relationship in ("spouse", "partner") and age < 18:
                raise ValueError(f"A {relationship} must be 18 or older.")
            sid = sandbox.split_id(household_id)[1]
            mid = f"m-{secrets.token_hex(3)}" + (f".{sid}" if sid else "")
            role = "managed" if age < 18 else "adult"
            conn.execute(
                "INSERT INTO members (id, household_id, name, relationship, age, full_time_student, "
                "status, role, has_login, dob, email, phone, zip) VALUES (?,?,?,?,?,0,'active',?,0,?,?,?,?)",
                (mid, household_id, name, relationship, age, role, dob, email, phone, zip_code))
            conn.execute("INSERT INTO member_usage (member_id, plan_year) VALUES (?, ?)",
                         (mid, DEMO_TODAY.year))
            return _row(conn.execute("SELECT * FROM members WHERE id = ?", (mid,)).fetchone())  # type: ignore[return-value]

    def remove_member(self, viewer_id: str, household_id: str, member_id: str) -> None:
        """Primary removes someone (never themself) and every row that belongs to them."""
        with session(self.path) as conn:
            viewer = self._primary_of(conn, viewer_id, household_id)
            target = _row(conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone())
            if target is None or target["household_id"] != household_id:
                raise NotFound(f"member {member_id}")
            if target["id"] == viewer["id"] or target["role"] == "primary":
                raise ValueError("The primary account holder can't be removed.")
            sandbox.delete_member_rows(conn, member_id)

    def get_account_member(self, account_id: str) -> dict[str, Any]:
        """Resolve a demo account to its member (used by demo-login)."""
        with session(self.path) as conn:
            r = conn.execute(
                "SELECT m.* FROM members m JOIN accounts a ON a.member_id = m.id WHERE a.id = ?",
                (account_id,),
            ).fetchone()
        if r is None:
            raise NotFound(f"account {account_id}")
        return dict(r)

    def get_household(self, viewer_id: str, household_id: str) -> dict[str, Any]:
        """The household and the members this viewer may see."""
        with session(self.path) as conn:
            viewer = self._viewer(conn, viewer_id)
            if viewer["household_id"] != household_id:
                raise AccessDenied("not your household")
            hh = _row(
                conn.execute(
                    "SELECT h.id AS hid, h.name AS hname, t.id AS tid, t.name AS tname, t.monthly_cents, "
                    "t.annual_max_cents, t.deductible_cents, t.preventive_pct, t.basic_pct, t.major_pct, "
                    "t.ortho_pct FROM households h JOIN plan_tiers t ON t.id = h.plan_tier_id "
                    "WHERE h.id = ?",
                    (household_id,),
                ).fetchone()
            )
            if hh is None:
                raise NotFound(f"household {household_id}")
            if viewer["role"] == "primary":
                members = conn.execute(
                    "SELECT * FROM members WHERE household_id = ? ORDER BY rowid", (household_id,)
                ).fetchall()
            else:
                members = [viewer]
            household = {"id": hh["hid"], "name": hh["hname"], "plan_tier": _tier(hh)}
            household["members"] = [dict(m) for m in members]
        return household

    def set_household_plan(self, viewer_id: str, household_id: str, tier_id: str) -> dict[str, Any]:
        """Primary only: switch the household's plan tier. Returns the updated household."""
        with session(self.path) as conn:
            viewer = self._viewer(conn, viewer_id)
            if viewer["household_id"] != household_id or viewer["role"] != "primary":
                raise AccessDenied("only the primary account holder can change the plan")
            if conn.execute("SELECT 1 FROM plan_tiers WHERE id = ?", (tier_id,)).fetchone() is None:
                raise NotFound(f"plan tier {tier_id}")
            conn.execute("UPDATE households SET plan_tier_id = ? WHERE id = ?", (tier_id, household_id))
        return self.get_household(viewer_id, household_id)

    def get_member(self, viewer_id: str, member_id: str) -> dict[str, Any]:
        with session(self.path) as conn:
            return self._target(conn, viewer_id, member_id)

    # ---- usage and visits ----------------------------------------------
    def get_member_usage(self, viewer_id: str, member_id: str, plan_year: int | None = None) -> dict[str, Any]:
        """Usage for one person in one plan year (defaults to the demo year). Zeros if none."""
        year = plan_year or DEMO_TODAY.year
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            return _usage(conn, member_id, year)

    def record_visit(
        self,
        viewer_id: str,
        member_id: str,
        visit_date: str,
        description: str,
        billed_cents: int,
        plan_paid_cents: int,
        patient_paid_cents: int,
        procedure_code: str | None = None,
        deductible_applied_cents: int = 0,
    ) -> dict[str, Any]:
        """Add a visit and update only this person's usage for that plan year."""
        if min(billed_cents, plan_paid_cents, patient_paid_cents, deductible_applied_cents) < 0:
            raise ValueError("amounts must be zero or more (integer cents)")
        year = date.fromisoformat(visit_date).year
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            conn.execute(
                "INSERT INTO visits (member_id, plan_year, visit_date, procedure_code, description, "
                "billed_cents, plan_paid_cents, patient_paid_cents) VALUES (?,?,?,?,?,?,?,?)",
                (member_id, year, visit_date, procedure_code, description,
                 billed_cents, plan_paid_cents, patient_paid_cents),
            )
            cleaning = 1 if procedure_code in CLEANING_CODES else 0
            conn.execute(
                "INSERT INTO member_usage (member_id, plan_year, max_used_cents, deductible_met_cents, "
                "visits, cleanings_used) VALUES (?,?,?,?,1,?) "
                "ON CONFLICT(member_id, plan_year) DO UPDATE SET "
                "max_used_cents = max_used_cents + excluded.max_used_cents, "
                "deductible_met_cents = deductible_met_cents + excluded.deductible_met_cents, "
                "visits = visits + 1, cleanings_used = cleanings_used + excluded.cleanings_used",
                (member_id, year, plan_paid_cents, deductible_applied_cents, cleaning),
            )
            return _usage(conn, member_id, year)

    def list_visits(self, viewer_id: str, member_id: str) -> list[dict[str, Any]]:
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            rows = conn.execute(
                "SELECT * FROM visits WHERE member_id = ? ORDER BY visit_date, id", (member_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- context -------------------------------------------------------
    def get_member_context(self, viewer_id: str, member_id: str, chat_limit: int = 20) -> dict[str, Any]:
        """Everything the assistant may use for this one person."""
        with session(self.path) as conn:
            member = self._target(conn, viewer_id, member_id)
            ctx = conn.execute(
                "SELECT plan_highlights FROM member_context WHERE member_id = ?", (member_id,)
            ).fetchone()
            prefs = conn.execute(
                "SELECT kind, text FROM member_preferences WHERE member_id = ? ORDER BY id", (member_id,)
            ).fetchall()
            chat = conn.execute(
                "SELECT role, content, created_at FROM chat_memory WHERE member_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (member_id, chat_limit),
            ).fetchall()
            history = conn.execute(
                "SELECT visit_date, procedure_code, description, billed_cents, plan_paid_cents, "
                "patient_paid_cents FROM visits WHERE member_id = ? ORDER BY visit_date, id",
                (member_id,),
            ).fetchall()
            usage = _usage(conn, member_id, DEMO_TODAY.year)
        return {
            "member_id": member_id,
            "name": member["name"],
            "plan_highlights": ctx["plan_highlights"] if ctx else "",
            "usage": usage,
            "history": [dict(r) for r in history],
            "preferences": [r["text"] for r in prefs if r["kind"] == "preference"],
            "must_haves": [r["text"] for r in prefs if r["kind"] == "must_have"],
            "chat_memory": [dict(r) for r in reversed(chat)],
        }

    def append_member_context(
        self, viewer_id: str, member_id: str, kind: str, text: str, role: str | None = None
    ) -> None:
        """Append to one person's context.

        kind: "preference" | "must_have" | "chat" (chat needs role "user" or "assistant").
        """
        if not text.strip():
            raise ValueError("text is empty")
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            if kind in ("preference", "must_have"):
                conn.execute(
                    "INSERT INTO member_preferences (member_id, kind, text) VALUES (?,?,?)",
                    (member_id, kind, text),
                )
            elif kind == "chat":
                if role not in ("user", "assistant"):
                    raise ValueError("chat needs role 'user' or 'assistant'")
                conn.execute(
                    "INSERT INTO chat_memory (member_id, role, content, created_at) VALUES (?,?,?,?)",
                    (member_id, role, text, datetime.now(timezone.utc).isoformat()),
                )
            else:
                raise ValueError(f"unknown context kind {kind!r}")

    def clear_chat_memory(self, viewer_id: str, member_id: str) -> int:
        """Delete one person's saved assistant chat. Returns how many messages were removed."""
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            cur = conn.execute("DELETE FROM chat_memory WHERE member_id = ?", (member_id,))
            return cur.rowcount

    # ---- schedule ------------------------------------------------------
    def list_upcoming_schedule(
        self, viewer_id: str, member_id: str | None = None, today: date | None = None
    ) -> list[dict[str, Any]]:
        """Upcoming appointments and reminders on or after today (demo clock by default).

        With no member_id: the primary sees everyone, an adult sees only themself.
        """
        cutoff = (today or DEMO_TODAY).isoformat()
        with session(self.path) as conn:
            viewer = self._viewer(conn, viewer_id)
            if member_id is not None:
                self._target(conn, viewer_id, member_id)
                ids = [member_id]
            elif viewer["role"] == "primary":
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM members WHERE household_id = ?", (viewer["household_id"],))]
            else:
                ids = [viewer_id]
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT a.id, a.member_id, m.name AS member_name, a.kind, a.due_date, a.title, a.note "
                f"FROM appointments a JOIN members m ON m.id = a.member_id "
                f"WHERE a.member_id IN ({marks}) AND a.due_date >= ? ORDER BY a.due_date, a.id",
                (*ids, cutoff),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- notifications, preferences and the delivery preview log (migration 006) ------------
    def get_notification_prefs(self, viewer_id: str, member_id: str) -> dict[str, Any]:
        """{app, email, sms, types} (types None means every kind). Defaults when nothing is saved:
        app on, email and sms off."""
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            return _prefs(conn, member_id)

    def set_notification_prefs(self, viewer_id: str, member_id: str, app: bool, email: bool, sms: bool,
                               types: list[str] | None) -> dict[str, Any]:
        """Save preferences (demo family only, same rule as profile edits). The route has already
        checked that email and sms have a contact on file."""
        with session(self.path) as conn:
            self._editable_target(conn, viewer_id, member_id)
            conn.execute(
                "INSERT INTO notification_prefs (member_id, app, email, sms, types_json) VALUES (?,?,?,?,?) "
                "ON CONFLICT(member_id) DO UPDATE SET app = excluded.app, email = excluded.email, "
                "sms = excluded.sms, types_json = excluded.types_json",
                (member_id, int(app), int(email), int(sms), None if types is None else json.dumps(types)))
            return _prefs(conn, member_id)

    def add_notifications(self, viewer_id: str, member_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Insert generated notifications that do not exist yet (unique per member and dedupe_key,
        so a read one is never recreated). Returns only the newly created rows, in the given order."""
        created: list[dict[str, Any]] = []
        now = _now_iso()
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            # Look first, so a poll that finds nothing new never opens a write transaction.
            have = {r[0] for r in conn.execute(
                "SELECT dedupe_key FROM notifications WHERE member_id = ?", (member_id,))}
            fresh_items, seen = [], set()
            for it in items:
                if it["dedupe_key"] not in have and it["dedupe_key"] not in seen:
                    seen.add(it["dedupe_key"])
                    fresh_items.append(it)
            for it in fresh_items:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO notifications (member_id, kind, title, body, severity, link, "
                    "dedupe_key, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (member_id, it["kind"], it["title"], it["body"], it.get("severity", "info"),
                     it.get("link"), it["dedupe_key"], now))
                if cur.rowcount:
                    created.append(_notification(conn.execute(
                        "SELECT * FROM notifications WHERE id = ?", (cur.lastrowid,)).fetchone()))
        return created

    def list_notifications(self, viewer_id: str, member_id: str, unread_only: bool = False) -> list[dict[str, Any]]:
        """Newest first (a batch created together keeps its generated order)."""
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            rows = conn.execute(
                "SELECT * FROM notifications WHERE member_id = ?" + (" AND read_at IS NULL" if unread_only else "")
                + " ORDER BY created_at DESC, id ASC", (member_id,)).fetchall()
        return [_notification(r) for r in rows]

    def count_unread_notifications(self, viewer_id: str, member_id: str) -> int:
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            return conn.execute("SELECT COUNT(*) FROM notifications WHERE member_id = ? AND read_at IS NULL",
                                (member_id,)).fetchone()[0]

    def mark_notification_read(self, viewer_id: str, member_id: str, notification_id: int) -> dict[str, Any]:
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            r = conn.execute("SELECT * FROM notifications WHERE id = ? AND member_id = ?",
                             (notification_id, member_id)).fetchone()
            if r is None:
                raise NotFound(f"notification {notification_id}")
            if r["read_at"] is None:
                conn.execute("UPDATE notifications SET read_at = ? WHERE id = ?", (_now_iso(), notification_id))
            return _notification(conn.execute("SELECT * FROM notifications WHERE id = ?",
                                              (notification_id,)).fetchone())

    def mark_all_notifications_read(self, viewer_id: str, member_id: str) -> int:
        """Returns how many were unread."""
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            return conn.execute("UPDATE notifications SET read_at = ? WHERE member_id = ? AND read_at IS NULL",
                                (_now_iso(), member_id)).rowcount

    def add_outbox(self, member_id: str, channel: str, to_address: str, subject: str | None,
                   body: str) -> dict[str, Any]:
        """Back-compat: a preview row. New callers should use record_outbox with an explicit status."""
        return self.record_outbox(member_id, channel, to_address, subject, body, status="preview")

    def record_outbox(self, member_id: str, channel: str, to_address: str, subject: str | None,
                      body: str, *, status: str = "preview", provider_message_id: str | None = None,
                      error: str | None = None) -> dict[str, Any]:
        """Write one delivery row recording the outcome of a send (or a 'preview' when nothing was
        sent). Notifiers call this; the status is validated by a database CHECK."""
        with session(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO outbox (member_id, channel, to_address, subject, body, created_at, "
                "status, provider_message_id, error) VALUES (?,?,?,?,?,?,?,?,?)",
                (member_id, channel, to_address, subject, body, _now_iso(), status,
                 provider_message_id, error))
            return dict(conn.execute("SELECT * FROM outbox WHERE id = ?", (cur.lastrowid,)).fetchone())

    def list_outbox(self, viewer_id: str, member_id: str) -> list[dict[str, Any]]:
        """Delivery log for one person, newest first (statuses: preview, queued, sent, failed)."""
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            rows = conn.execute("SELECT * FROM outbox WHERE member_id = ? ORDER BY created_at DESC, id DESC",
                                (member_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- web-push subscriptions (migration 010) ----------------------------------------------
    def add_push_subscription(self, viewer_id: str, member_id: str, endpoint: str,
                              p256dh: str, auth: str) -> dict[str, Any]:
        """Save (or refresh) one browser's push subscription for a member the viewer may edit."""
        with session(self.path) as conn:
            self._editable_target(conn, viewer_id, member_id)
            conn.execute(
                "INSERT INTO push_subscriptions (member_id, endpoint, p256dh, auth, created_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET "
                "member_id = excluded.member_id, p256dh = excluded.p256dh, auth = excluded.auth",
                (member_id, endpoint, p256dh, auth, _now_iso()))
            row = conn.execute("SELECT * FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
            return dict(row)

    def remove_push_subscription(self, viewer_id: str, member_id: str, endpoint: str) -> int:
        """Delete one subscription (used on unsubscribe or when the push service reports it gone)."""
        with session(self.path) as conn:
            self._editable_target(conn, viewer_id, member_id)
            return conn.execute("DELETE FROM push_subscriptions WHERE member_id = ? AND endpoint = ?",
                                (member_id, endpoint)).rowcount

    def list_push_subscriptions(self, member_id: str) -> list[dict[str, Any]]:
        """Every push subscription for a member (used by delivery; no viewer check needed here)."""
        with session(self.path) as conn:
            rows = conn.execute("SELECT * FROM push_subscriptions WHERE member_id = ?",
                                (member_id,)).fetchall()
        return [dict(r) for r in rows]

    def check_prefs_access(self, viewer_id: str, member_id: str) -> None:
        """Raise AccessDenied or NotFound unless the viewer may change this person's notification
        settings (demo family only)."""
        with session(self.path) as conn:
            self._editable_target(conn, viewer_id, member_id)

    # ---- reports: synthetic claims, EOBs and copay visits (migration 009) --------------------
    def list_report_items(self, viewer_id: str, member_id: str, kind: str | None = None,
                          date_from: str | None = None, date_to: str | None = None,
                          newest_first: bool = False) -> list[dict[str, Any]]:
        """One person's report items by service date (oldest first unless `newest_first`). `data`
        holds the stored fields, amounts in integer cents. Same visibility as the person's other data."""
        sql, args = "SELECT * FROM report_items WHERE member_id = ?", [member_id]
        if kind:
            sql, args = sql + " AND kind = ?", args + [kind]
        if date_from:
            sql, args = sql + " AND service_date >= ?", args + [date_from]
        if date_to:
            sql, args = sql + " AND service_date <= ?", args + [date_to]
        sql += " ORDER BY service_date DESC, rowid DESC" if newest_first else " ORDER BY service_date, rowid"
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            rows = conn.execute(sql, args).fetchall()
        return [_report_item(r) for r in rows]

    def get_report_item(self, viewer_id: str, member_id: str, item_id: str) -> dict[str, Any]:
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            r = conn.execute("SELECT * FROM report_items WHERE id = ? AND member_id = ?",
                             (item_id, member_id)).fetchone()
        if r is None:
            raise NotFound(f"report {item_id}")
        return _report_item(r)

    def add_report_item(self, viewer_id: str, member_id: str, item: dict[str, Any]) -> dict[str, Any]:
        """Save an already validated document (keys: kind, service_date, title, provider_id,
        provider_name, code, description, data in cents, paid_status). Demo family only; at most
        MAX_REPORT_ITEMS per person (ValueError with a plain message)."""
        with session(self.path) as conn:
            self._editable_target(conn, viewer_id, member_id)
            n = conn.execute("SELECT COUNT(*) FROM report_items WHERE member_id = ?", (member_id,)).fetchone()[0]
            if n >= MAX_REPORT_ITEMS:
                raise ValueError(f"You can keep up to {MAX_REPORT_ITEMS} documents per person. Delete one first.")
            sid = sandbox.split_id(member_id)[1]
            item_id = "ri-" + secrets.token_hex(5) + (f".{sid}" if sid else "")
            conn.execute(
                "INSERT INTO report_items (id, member_id, kind, service_date, title, provider_id, "
                "provider_name, code, description, data_json, paid_status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, member_id, item["kind"], item["service_date"], item["title"], item.get("provider_id"),
                 item["provider_name"], item.get("code"), item.get("description", ""),
                 json.dumps(item["data"]), item["paid_status"], _now_iso()))
            return _report_item(conn.execute("SELECT * FROM report_items WHERE id = ?", (item_id,)).fetchone())

    def mark_report_paid(self, viewer_id: str, member_id: str, item_id: str) -> dict[str, Any]:
        """Mark what you owe on a document as paid (demo family only). Already paid is fine."""
        with session(self.path) as conn:
            self._editable_target(conn, viewer_id, member_id)
            r = conn.execute("SELECT * FROM report_items WHERE id = ? AND member_id = ?",
                             (item_id, member_id)).fetchone()
            if r is None:
                raise NotFound(f"report {item_id}")
            if r["paid_status"] == "not_applicable":
                raise ValueError("There is nothing to pay on this document.")
            conn.execute("UPDATE report_items SET paid_status = 'paid' WHERE id = ?", (item_id,))
            return _report_item(conn.execute("SELECT * FROM report_items WHERE id = ?", (item_id,)).fetchone())

    def delete_report_item(self, viewer_id: str, member_id: str, item_id: str) -> None:
        with session(self.path) as conn:
            self._editable_target(conn, viewer_id, member_id)
            cur = conn.execute("DELETE FROM report_items WHERE id = ? AND member_id = ?", (item_id, member_id))
            if cur.rowcount == 0:
                raise NotFound(f"report {item_id}")

    # ---- invites -------------------------------------------------------
    def create_invite(
        self, viewer_id: str, email: str, member_id: str | None = None
    ) -> dict[str, Any]:
        """Primary invites an adult (18+). member_id names an existing adult profile, if any."""
        with session(self.path) as conn:
            viewer = self._viewer(conn, viewer_id)
            if viewer["role"] != "primary":
                raise AccessDenied("only the primary account holder can invite")
            if member_id is not None:
                m = self._target(conn, viewer_id, member_id)
                if m["age"] < 18:
                    raise ValueError("login is for adults 18 and over")
                if m["has_login"]:
                    raise ValueError("this person already has a login")
            invite = {
                "id": "inv-" + secrets.token_hex(4),
                "household_id": viewer["household_id"],
                "invited_by": viewer_id,
                "member_id": member_id,
                "email": email,
                "token": secrets.token_urlsafe(16),
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            conn.execute(
                "INSERT INTO invites (id, household_id, invited_by, member_id, email, token, status, created_at) "
                "VALUES (:id, :household_id, :invited_by, :member_id, :email, :token, :status, :created_at)",
                invite,
            )
        return invite

    def list_invites(self, viewer_id: str) -> list[dict[str, Any]]:
        with session(self.path) as conn:
            viewer = self._viewer(conn, viewer_id)
            if viewer["role"] != "primary":
                raise AccessDenied("only the primary account holder can see invites")
            rows = conn.execute(
                "SELECT * FROM invites WHERE household_id = ? ORDER BY created_at", (viewer["household_id"],)
            ).fetchall()
        return [dict(r) for r in rows]

    def accept_invite(self, token: str) -> dict[str, Any]:
        """Give the invited adult profile a login (demo level, no password)."""
        with session(self.path) as conn:
            inv = conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
            if inv is None or inv["status"] != "pending":
                raise NotFound("invite")
            if inv["member_id"] is None:
                raise ValueError("this invite is not linked to a profile")
            m = conn.execute("SELECT * FROM members WHERE id = ?", (inv["member_id"],)).fetchone()
            if m["age"] < 18:
                raise ValueError("login is for adults 18 and over")
            conn.execute("UPDATE members SET has_login = 1 WHERE id = ?", (m["id"],))
            conn.execute(
                "INSERT INTO accounts (id, member_id, email, display_name) VALUES (?,?,?,?)",
                ("acct-" + m["id"].removeprefix("m-"), m["id"], inv["email"], m["name"]),
            )
            conn.execute("UPDATE invites SET status = 'accepted' WHERE id = ?", (inv["id"],))
            return dict(conn.execute("SELECT * FROM members WHERE id = ?", (m["id"],)).fetchone())

    # ---- saved Plan My Year plans ---------------------------------------
    def list_saved_plans(self, viewer_id: str, member_id: str) -> list[dict[str, Any]]:
        """Saved plans for one person, newest first. `items` is a list of dicts."""
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            rows = conn.execute(
                "SELECT * FROM saved_plans WHERE member_id = ? ORDER BY created_at DESC, rowid DESC",
                (member_id,),
            ).fetchall()
        return [_saved_plan(r) for r in rows]

    def create_saved_plan(
        self, viewer_id: str, member_id: str, name: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        plan_id = "sp-" + secrets.token_hex(6)
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            conn.execute(
                "INSERT INTO saved_plans (id, member_id, name, items_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (plan_id, member_id, name, json.dumps(items), now, now),
            )
            return _saved_plan(conn.execute("SELECT * FROM saved_plans WHERE id = ?", (plan_id,)).fetchone())

    def update_saved_plan(
        self, viewer_id: str, member_id: str, plan_id: str,
        name: str | None = None, items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            row = conn.execute(
                "SELECT * FROM saved_plans WHERE id = ? AND member_id = ?", (plan_id, member_id)
            ).fetchone()
            if row is None:
                raise NotFound(f"saved plan {plan_id}")
            new_name = row["name"] if name is None else name
            new_items = row["items_json"] if items is None else json.dumps(items)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE saved_plans SET name = ?, items_json = ?, updated_at = ? WHERE id = ?",
                (new_name, new_items, now, plan_id),
            )
            return _saved_plan(conn.execute("SELECT * FROM saved_plans WHERE id = ?", (plan_id,)).fetchone())

    def delete_saved_plan(self, viewer_id: str, member_id: str, plan_id: str) -> None:
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            cur = conn.execute(
                "DELETE FROM saved_plans WHERE id = ? AND member_id = ?", (plan_id, member_id)
            )
            if cur.rowcount == 0:
                raise NotFound(f"saved plan {plan_id}")

    # ---- saved "Which plan fits us?" comparisons --------------------------
    def household_plan_id(self, viewer_id: str, member_id: str) -> str:
        """The plan tier id of the household this member belongs to (access checked)."""
        with session(self.path) as conn:
            target = self._target(conn, viewer_id, member_id)
            r = conn.execute(
                "SELECT plan_tier_id FROM households WHERE id = ?", (target["household_id"],)
            ).fetchone()
        return r["plan_tier_id"]

    def count_saved_simulations(self, viewer_id: str, member_id: str) -> int:
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            return conn.execute(
                "SELECT COUNT(*) AS n FROM saved_simulations WHERE member_id = ?", (member_id,)
            ).fetchone()["n"]

    def list_saved_simulations(self, viewer_id: str, member_id: str) -> list[dict[str, Any]]:
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            rows = conn.execute(
                "SELECT * FROM saved_simulations WHERE member_id = ? ORDER BY created_at DESC, rowid DESC",
                (member_id,),
            ).fetchall()
        return [_saved_simulation(r) for r in rows]

    def create_saved_simulation(
        self, viewer_id: str, member_id: str, name: str,
        request: dict[str, Any], summary: dict[str, Any], max_per_member: int | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sim_id = "ss-" + secrets.token_hex(6)
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            if max_per_member is not None:
                n = conn.execute(
                    "SELECT COUNT(*) AS n FROM saved_simulations WHERE member_id = ?", (member_id,)
                ).fetchone()["n"]
                if n >= max_per_member:
                    raise ValueError(f"You can save up to {max_per_member} comparisons. Delete one first.")
            conn.execute(
                "INSERT INTO saved_simulations (id, member_id, name, request_json, summary_json, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (sim_id, member_id, name, json.dumps(request), json.dumps(summary), now, now),
            )
            return _saved_simulation(
                conn.execute("SELECT * FROM saved_simulations WHERE id = ?", (sim_id,)).fetchone()
            )

    def get_saved_simulation(self, viewer_id: str, member_id: str, sim_id: str) -> dict[str, Any]:
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            row = conn.execute(
                "SELECT * FROM saved_simulations WHERE id = ? AND member_id = ?", (sim_id, member_id)
            ).fetchone()
            if row is None:
                raise NotFound(f"saved comparison {sim_id}")
        return _saved_simulation(row)

    def update_saved_simulation(
        self, viewer_id: str, member_id: str, sim_id: str, name: str | None = None,
        request: dict[str, Any] | None = None, summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            row = conn.execute(
                "SELECT * FROM saved_simulations WHERE id = ? AND member_id = ?", (sim_id, member_id)
            ).fetchone()
            if row is None:
                raise NotFound(f"saved comparison {sim_id}")
            new_name = row["name"] if name is None else name
            new_req = row["request_json"] if request is None else json.dumps(request)
            new_sum = row["summary_json"] if summary is None else json.dumps(summary)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE saved_simulations SET name = ?, request_json = ?, summary_json = ?, "
                "updated_at = ? WHERE id = ?",
                (new_name, new_req, new_sum, now, sim_id),
            )
            return _saved_simulation(
                conn.execute("SELECT * FROM saved_simulations WHERE id = ?", (sim_id,)).fetchone()
            )

    def delete_saved_simulation(self, viewer_id: str, member_id: str, sim_id: str) -> None:
        with session(self.path) as conn:
            self._target(conn, viewer_id, member_id)
            cur = conn.execute(
                "DELETE FROM saved_simulations WHERE id = ? AND member_id = ?", (sim_id, member_id)
            )
            if cur.rowcount == 0:
                raise NotFound(f"saved comparison {sim_id}")


def _saved_simulation(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["request"] = json.loads(d.pop("request_json"))
    d["summary"] = json.loads(d.pop("summary_json"))
    return d


def _saved_plan(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["items"] = json.loads(d.pop("items_json"))
    return d


def _report_item(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["data"] = json.loads(d.pop("data_json"))
    return d


def _provider(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["accepting_new"] = bool(d["accepting_new"])
    d["languages"] = json.loads(d["languages"])
    d["network_plan_ids"] = json.loads(d["network_plan_ids"])
    return d


def _tier(r: dict[str, Any]) -> dict[str, Any]:
    keys = ("monthly_cents", "annual_max_cents", "deductible_cents",
            "preventive_pct", "basic_pct", "major_pct", "ortho_pct")
    return {"id": r["tid"], "name": r["tname"], **{k: r[k] for k in keys}}


def _usage(conn: sqlite3.Connection, member_id: str, year: int) -> dict[str, Any]:
    r = conn.execute(
        "SELECT * FROM member_usage WHERE member_id = ? AND plan_year = ?", (member_id, year)
    ).fetchone()
    if r is None:
        return {"member_id": member_id, "plan_year": year, "max_used_cents": 0,
                "deductible_met_cents": 0, "visits": 0, "cleanings_used": 0}
    return dict(r)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _prefs(conn: sqlite3.Connection, member_id: str) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM notification_prefs WHERE member_id = ?", (member_id,)).fetchone()
    if r is None:
        return {"app": True, "email": False, "sms": False, "types": None}
    return {"app": bool(r["app"]), "email": bool(r["email"]), "sms": bool(r["sms"]),
            "types": None if r["types_json"] is None else json.loads(r["types_json"])}


def _notification(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d.pop("dedupe_key", None)
    return d
