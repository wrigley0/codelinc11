-- 010: enable REAL delivery (email via Resend, web push via VAPID). SMS is intentionally not wired.
-- Widens outbox.status beyond 'preview' and records the provider's result, and adds a table of
-- browser web-push subscriptions. The default sender is still the preview notifier; a real sender
-- only activates when its API keys are present in the environment (see backend/app/notifier.py).

-- Rebuild outbox: allow 'push' channel, allow real statuses, record the provider message id/error.
-- (SQLite can't ALTER a CHECK constraint, so recreate the table and copy the rows.)
ALTER TABLE outbox RENAME TO outbox_old;

CREATE TABLE outbox (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id            TEXT NOT NULL REFERENCES members(id),
    channel              TEXT NOT NULL CHECK (channel IN ('email','sms','push')),
    to_address           TEXT NOT NULL,
    subject              TEXT,
    body                 TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'preview'
                              CHECK (status IN ('preview','queued','sent','failed')),
    provider_message_id  TEXT,
    error                TEXT
);

INSERT INTO outbox (id, member_id, channel, to_address, subject, body, created_at, status)
    SELECT id, member_id, channel, to_address, subject, body, created_at, status FROM outbox_old;

DROP TABLE outbox_old;
CREATE INDEX idx_outbox_member ON outbox(member_id, created_at);

-- Browser web-push subscriptions (one per browser/device the person opted in on).
-- endpoint is unique; p256dh and auth are the subscription's public keys from the Push API.
CREATE TABLE push_subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   TEXT NOT NULL REFERENCES members(id),
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_push_subscriptions_member ON push_subscriptions(member_id);
