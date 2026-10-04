"""Delivery of notifications outside the app: email (Resend) and web push (VAPID).

Safe by default: with no provider keys in the environment, `get_notifier` returns `PreviewNotifier`,
which only writes an outbox row with status 'preview' and sends nothing. A real sender activates
only when its keys are set (RESEND_API_KEY for email; VAPID_PRIVATE_KEY + VAPID_PUBLIC_KEY +
VAPID_SUBJECT for web push). SMS is intentionally not implemented.

Every send writes one outbox row recording the outcome:
  preview  nothing was sent (no provider configured for that channel)
  sent     the provider accepted it (provider_message_id stored when returned)
  failed   the provider rejected it (error stored)

Keys are read from the environment (backend/.env) and never logged or returned.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from dotenv import load_dotenv

from .db import Store

load_dotenv()

RESEND_URL = "https://api.resend.com/emails"
DELIVERY_TIMEOUT = 15.0


@dataclass(frozen=True)
class OutboundMessage:
    member_id: str
    channel: str                 # "email", "sms" or "push"
    to_address: str              # email, phone, or a push-subscription endpoint
    subject: str | None
    body: str


class Notifier(Protocol):
    def deliver(self, message: OutboundMessage) -> dict[str, Any]:
        """Hand one message to a delivery channel and return its outbox row."""
        ...


# --------------------------------------------------------------------- preview (default, safe)

class PreviewNotifier:
    """Writes an outbox row with status 'preview'. Never sends anything."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def deliver(self, message: OutboundMessage) -> dict[str, Any]:
        return self.store.record_outbox(
            message.member_id, message.channel, message.to_address,
            message.subject, message.body, status="preview")


# --------------------------------------------------------------------- email (Resend)

class ResendNotifier:
    """Sends email through Resend. Falls back to a preview row for non-email channels."""

    def __init__(self, store: Store, api_key: str, sender: str,
                 url: str = RESEND_URL, timeout: float = DELIVERY_TIMEOUT) -> None:
        self.store = store
        self._key = api_key
        self.sender = sender
        self.url = url
        self.timeout = timeout

    def deliver(self, message: OutboundMessage) -> dict[str, Any]:
        if message.channel != "email":
            return self.store.record_outbox(message.member_id, message.channel,
                                            message.to_address, message.subject, message.body,
                                            status="preview")
        payload = {
            "from": self.sender,
            "to": [message.to_address],
            "subject": message.subject or "A note about your dental benefits",
            "text": message.body,
        }
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        try:
            r = httpx.post(self.url, json=payload, headers=headers, timeout=self.timeout)
            r.raise_for_status()
            data = r.json() if r.content else {}
            msg_id = data.get("id") if isinstance(data, dict) else None
            return self.store.record_outbox(message.member_id, "email", message.to_address,
                                            message.subject, message.body, status="sent",
                                            provider_message_id=msg_id)
        except Exception as exc:  # noqa: BLE001  network/provider failure -> record, don't crash
            return self.store.record_outbox(message.member_id, "email", message.to_address,
                                            message.subject, message.body, status="failed",
                                            error=f"{type(exc).__name__}")


# --------------------------------------------------------------------- web push (VAPID)

class WebPushNotifier:
    """Sends a browser web-push message to a subscription endpoint via pywebpush/VAPID."""

    def __init__(self, store: Store, private_key: str, public_key: str, subject: str,
                 timeout: float = DELIVERY_TIMEOUT) -> None:
        self.store = store
        self._private_key = private_key
        self.public_key = public_key
        self.subject = subject  # a mailto: or https: contact required by the push spec
        self.timeout = timeout

    def deliver(self, message: OutboundMessage) -> dict[str, Any]:
        if message.channel != "push":
            return self.store.record_outbox(message.member_id, message.channel,
                                            message.to_address, message.subject, message.body,
                                            status="preview")
        # to_address holds the full subscription JSON (endpoint + keys) for this device.
        try:
            from pywebpush import webpush  # imported lazily so the dep is optional
            subscription = json.loads(message.to_address)
            payload = json.dumps({"title": message.subject or "Dental benefits",
                                  "body": message.body})
            webpush(subscription_info=subscription, data=payload,
                    vapid_private_key=self._private_key,
                    vapid_claims={"sub": self.subject}, timeout=self.timeout)
            endpoint = subscription.get("endpoint", "")
            return self.store.record_outbox(message.member_id, "push", endpoint,
                                            message.subject, message.body, status="sent")
        except Exception as exc:  # noqa: BLE001
            # A gone/expired subscription (404/410) should be pruned by the caller via the error.
            return self.store.record_outbox(message.member_id, "push", _endpoint_of(message.to_address),
                                            message.subject, message.body, status="failed",
                                            error=f"{type(exc).__name__}")


def _endpoint_of(raw: str) -> str:
    try:
        return json.loads(raw).get("endpoint", "")[:500]
    except Exception:  # noqa: BLE001
        return raw[:500]


# --------------------------------------------------------------------- routing + selection

class CompositeNotifier:
    """Routes each message to the sender for its channel; preview for anything unconfigured."""

    def __init__(self, store: Store, by_channel: dict[str, Notifier]) -> None:
        self.store = store
        self.by_channel = by_channel
        self._preview = PreviewNotifier(store)

    def deliver(self, message: OutboundMessage) -> dict[str, Any]:
        return self.by_channel.get(message.channel, self._preview).deliver(message)


def email_sender_configured(env: dict | None = None) -> str | None:
    env = os.environ if env is None else env
    key = env.get("RESEND_API_KEY")
    sender = env.get("RESEND_FROM")
    return sender if key and sender else None


def push_public_key(env: dict | None = None) -> str | None:
    """The VAPID public key the frontend needs to subscribe, when web push is configured."""
    env = os.environ if env is None else env
    if env.get("VAPID_PRIVATE_KEY") and env.get("VAPID_PUBLIC_KEY") and env.get("VAPID_SUBJECT"):
        return env["VAPID_PUBLIC_KEY"]
    return None


def get_notifier(store: Store, env: dict | None = None) -> Notifier:
    """The notifier the app uses. Real senders activate per channel when their keys are set;
    otherwise that channel (and the default) is the preview notifier, which sends nothing."""
    env = os.environ if env is None else env
    channels: dict[str, Notifier] = {}
    key, sender = env.get("RESEND_API_KEY"), env.get("RESEND_FROM")
    if key and sender:
        channels["email"] = ResendNotifier(store, key, sender)
    if env.get("VAPID_PRIVATE_KEY") and env.get("VAPID_PUBLIC_KEY") and env.get("VAPID_SUBJECT"):
        channels["push"] = WebPushNotifier(store, env["VAPID_PRIVATE_KEY"],
                                           env["VAPID_PUBLIC_KEY"], env["VAPID_SUBJECT"])
    if not channels:
        return PreviewNotifier(store)
    return CompositeNotifier(store, channels)
