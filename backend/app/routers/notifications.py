"""Notifications and preferences (sprint 2, B2).

GET  /members/{id}/notifications?unread=1        generates, then lists newest first
POST /members/{id}/notifications/{nid}/read
POST /members/{id}/notifications/read-all
GET  /members/{id}/notification-prefs            app on by default; email and sms off
PUT  /members/{id}/notification-prefs            demo family only; email/sms need a contact on file
POST /members/{id}/notifications/test            {channel}: creates a preview (demo family only)
GET  /members/{id}/outbox                        delivery previews (never sent)

Visibility is the usual rule: a primary sees everyone in the household, an adult only themself.
Reading is allowed in the shared template family; changing settings or sending a test is not.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .. import ratelimit
from ..models import (
    Notification,
    NotificationList,
    NotificationPrefs,
    NotificationPrefsUpdate,
    OutboxMessage,
    PushConfig,
    PushSubscriptionRequest,
    TestNotificationRequest,
    TestNotificationResponse,
)
from ..notifications import KINDS, sync_notifications
from ..notifier import OutboundMessage, get_notifier, push_public_key
from .profiles import EMAIL_RE
from .session import StoreDep, Viewer, guarded

router = APIRouter(tags=["notifications"])


def _valid_email(v: str | None) -> bool:
    return bool(v) and len(v) <= 80 and EMAIL_RE.match(v) is not None  # type: ignore[arg-type]


def _valid_phone(v: str | None) -> bool:
    return bool(v) and v.isascii() and v.isdigit() and 10 <= len(v) <= 15  # type: ignore[union-attr]


def _prefs_model(prefs: dict[str, Any], member: dict[str, Any]) -> NotificationPrefs:
    return NotificationPrefs(app=prefs["app"], email=prefs["email"], sms=prefs["sms"], types=prefs["types"],
                             email_on_file=_valid_email(member.get("email")),
                             phone_on_file=_valid_phone(member.get("phone")))


@router.get("/members/{member_id}/notifications", response_model=NotificationList)
def list_notifications(member_id: str, viewer: Viewer, store: StoreDep, unread: bool = False) -> NotificationList:
    """Make any new notifications from this person's data, then list them newest first.
    `unread_count` counts every unread one. When the person turned the in-app channel off the list
    is empty and `app_enabled` is false (nothing is deleted)."""
    guarded(lambda: sync_notifications(store, viewer, member_id))
    prefs = guarded(lambda: store.get_notification_prefs(viewer, member_id))
    if not prefs["app"]:
        return NotificationList(notifications=[], unread_count=0, app_enabled=False)
    rows = guarded(lambda: store.list_notifications(viewer, member_id, unread_only=unread))
    count = guarded(lambda: store.count_unread_notifications(viewer, member_id))
    return NotificationList(notifications=[Notification(**r) for r in rows], unread_count=count)


@router.post("/members/{member_id}/notifications/read-all")
def read_all(member_id: str, viewer: Viewer, store: StoreDep) -> dict[str, Any]:
    n = guarded(lambda: store.mark_all_notifications_read(viewer, member_id))
    return {"ok": True, "marked": n, "unread_count": 0}


@router.post("/members/{member_id}/notifications/test", response_model=TestNotificationResponse,
             dependencies=[Depends(ratelimit.limit_compute)])
def send_test(member_id: str, req: TestNotificationRequest, viewer: Viewer,
              store: StoreDep) -> TestNotificationResponse:
    """Create a sample. app: a notification in the list. email or sms: a preview row in the outbox
    addressed to the stored contact (422 if none). Nothing is sent. Demo family only."""
    guarded(lambda: store.check_prefs_access(viewer, member_id))
    member = guarded(lambda: store.get_member(viewer, member_id))
    first = member["name"].split()[0]
    title = "This is a test notification"
    body = "Nothing is wrong. This is just a preview. Nothing was sent."
    if req.channel == "app":
        made = guarded(lambda: store.add_notifications(viewer, member_id, [{
            "kind": "test", "title": title, "body": body, "severity": "info", "link": None,
            "dedupe_key": f"test:{uuid.uuid4().hex[:12]}"}]))
        return TestNotificationResponse(channel="app", notification=Notification(**made[0]))
    contact = member.get("email") if req.channel == "email" else member.get("phone")
    ok = _valid_email(contact) if req.channel == "email" else _valid_phone(contact)
    if not ok:
        what = "an email address" if req.channel == "email" else "a phone number"
        raise HTTPException(status_code=422, detail=f"Add {what} to {first}'s profile first.")
    row = get_notifier(store).deliver(OutboundMessage(
        member_id, req.channel, contact,  # type: ignore[arg-type]
        title if req.channel == "email" else None, f"{title}. {body}"))
    return TestNotificationResponse(channel=req.channel, outbox=OutboxMessage(**row))


@router.post("/members/{member_id}/notifications/{notification_id}/read", response_model=Notification)
def mark_read(member_id: str, notification_id: int, viewer: Viewer, store: StoreDep) -> Notification:
    return Notification(**guarded(lambda: store.mark_notification_read(viewer, member_id, notification_id)))


@router.get("/members/{member_id}/notification-prefs", response_model=NotificationPrefs)
def get_prefs(member_id: str, viewer: Viewer, store: StoreDep) -> NotificationPrefs:
    member = guarded(lambda: store.get_member(viewer, member_id))
    return _prefs_model(guarded(lambda: store.get_notification_prefs(viewer, member_id)), member)


@router.put("/members/{member_id}/notification-prefs", response_model=NotificationPrefs)
def put_prefs(member_id: str, req: NotificationPrefsUpdate, viewer: Viewer,
              store: StoreDep) -> NotificationPrefs:
    """Save preferences (demo family only). Turning email or text on needs a valid email or phone
    saved on the person's profile; otherwise 422 with a plain message."""
    guarded(lambda: store.check_prefs_access(viewer, member_id))
    member = guarded(lambda: store.get_member(viewer, member_id))
    first = member["name"].split()[0]
    if req.email and not _valid_email(member.get("email")):
        raise HTTPException(status_code=422,
                            detail=f"Add an email address to {first}'s profile before turning on email.")
    if req.sms and not _valid_phone(member.get("phone")):
        raise HTTPException(status_code=422,
                            detail=f"Add a phone number to {first}'s profile before turning on text messages.")
    types = None if req.types is None else [k for k in KINDS if k in set(req.types)]
    saved = guarded(lambda: store.set_notification_prefs(viewer, member_id, req.app, req.email, req.sms, types))
    return _prefs_model(saved, member)


@router.get("/members/{member_id}/outbox", response_model=list[OutboxMessage])
def list_outbox(member_id: str, viewer: Viewer, store: StoreDep) -> list[OutboxMessage]:
    """Delivery log, newest first. Status is preview (nothing sent) or sent/failed when a real
    email or web-push provider is configured."""
    return [OutboxMessage(**r) for r in guarded(lambda: store.list_outbox(viewer, member_id))]


# ---- web push ---------------------------------------------------------------------------------

@router.get("/notifications/push-config", response_model=PushConfig)
def get_push_config() -> PushConfig:
    """The VAPID public key the browser needs to subscribe, or enabled=false when push is off."""
    key = push_public_key()
    return PushConfig(enabled=key is not None, public_key=key)


@router.post("/members/{member_id}/push-subscriptions", status_code=204)
def subscribe_push(member_id: str, sub: PushSubscriptionRequest, viewer: Viewer,
                   store: StoreDep) -> None:
    """Save this browser's web-push subscription for the member (demo family only)."""
    guarded(lambda: store.add_push_subscription(
        viewer, member_id, sub.endpoint, sub.keys.p256dh, sub.keys.auth))


@router.delete("/members/{member_id}/push-subscriptions", status_code=204)
def unsubscribe_push(member_id: str, sub: PushSubscriptionRequest, viewer: Viewer,
                     store: StoreDep) -> None:
    """Remove this browser's subscription (on opt-out)."""
    guarded(lambda: store.remove_push_subscription(viewer, member_id, sub.endpoint))
