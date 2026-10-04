# Notifications: real delivery (email + web push)

The app generates notifications from each person's data (benefits expiring, unused preventive
visits, upcoming appointments, claim updates, …). Generation is deterministic and deduplicated, so
running it repeatedly never creates duplicates.

**Delivery is off by default and safe.** With no provider keys set, every send writes an `outbox`
row with status `preview` and nothing leaves the server. Turning on a provider's keys activates
that channel. SMS is intentionally not implemented (US A2P 10DLC registration makes it impractical
for this project).

## Channels

| Channel | Provider | Turns on when these env vars are set |
|---|---|---|
| Email | [Resend](https://resend.com) | `RESEND_API_KEY`, `RESEND_FROM` |
| Web push | Browser Push API (VAPID) | `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` |

Who receives it: the signed-in person's own email; in the shared demo, the demo member's seed
email. Web push goes to every browser where the person clicked "Enable notifications".

## 1. Email (Resend)

1. Create a Resend account and **verify a sending domain** (or use their test sender for a demo).
2. Create an API key.
3. Set on the server (never in the repo):
   ```
   RESEND_API_KEY=re_...
   RESEND_FROM=Dental Benefits <noreply@your-verified-domain>
   ```
Restart the backend. New notifications for people with email turned on now send; the `outbox` row
shows `sent` (with the provider message id) or `failed` (with the error).

## 2. Web push (VAPID)

Generate a VAPID keypair once (no account needed):
```bash
python -c "from pywebpush import Vapid01; v=Vapid01(); v.generate_keys(); \
import base64; \
print('public =', base64.urlsafe_b64encode(v.public_key.public_bytes(\
__import__('cryptography').hazmat.primitives.serialization.Encoding.X962, \
__import__('cryptography').hazmat.primitives.serialization.PublicFormat.UncompressedPoint)).rstrip(b'=').decode()); \
print('private =', base64.urlsafe_b64encode(v.private_key.private_numbers().private_value.to_bytes(32,'big')).rstrip(b'=').decode())"
```
(or use any VAPID generator). Then set:
```
VAPID_PUBLIC_KEY=...
VAPID_PRIVATE_KEY=...
VAPID_SUBJECT=mailto:you@example.com
```
The frontend fetches the public key from `GET /notifications/push-config`, asks the browser for
permission, and registers a service worker that subscribes and `POST`s the subscription to
`/members/{id}/push-subscriptions`. Delivery then pushes to each saved subscription.

## 3. Sending on a schedule

Generation + delivery happen whenever the notifications list is fetched
(`GET /members/{id}/notifications`). For reminders to go out on their own, hit that endpoint on a
schedule, e.g. a daily cron on the server:
```
# crontab -e  (runs daily at 8am; adjust the member id / auth as needed)
0 8 * * *  curl -s https://YOUR-API/members/<id>/notifications >/dev/null
```
or a scheduled GitHub Action. (A dedicated "run for everyone" endpoint can be added later.)

## Safety notes

- **Consent:** only send to people who opted in. Email/web-push are off until the person enables
  them in notification preferences, and web push additionally requires the browser permission grant.
- **Keys** live only in `backend/.env` or the host's env/secret store — never in code or chat.
- The `outbox` is a full delivery log (`preview` / `queued` / `sent` / `failed`), viewable per
  member at `GET /members/{id}/outbox`.
- SMS: not wired. US carriers require A2P 10DLC brand/campaign registration before sending to
  arbitrary numbers; a Twilio trial can only reach one pre-verified number. Out of scope here.
