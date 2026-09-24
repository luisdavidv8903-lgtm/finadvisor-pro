# Incident: Real WhatsApp webhook events not dispatched to Chambeando's callback

**Status:** Open / unresolved. **Meta debugging frozen as of 2026-09-22** after a dedicated-WABA registration and a two-independent-app control experiment both reproduced the same failure. No further sends, retries, WABA changes, app changes, token generation, registrations, subscriptions, or webhook changes are authorized until this incident is revisited. Development focus has shifted to a Telegram-based pilot (see `TELEGRAM_PILOT.md`, once created) while this stays open as evidence.

## Identifiers

- App ID: `2278373642738035` (Chambeando)
- Test WABA ID: `1270120011621786`
- Phone Number ID: `1177098292148866`
- Demo number: `+1 555-678-4943`
- Authorized test recipient: `+1 561-679-0314`
- Stable callback (Named Tunnel, Cloudflare): `https://chambeando-webhook.link-credit.com/whatsapp/webhook`

## What's confirmed working

- Callback handshake (`GET` with `hub.mode=subscribe` + correct verify token, issued by Meta itself right after the callback was updated): **HTTP 200**.
- Backend public health check (`GET /docs` via the stable callback domain): **HTTP 200**.
- `messages` field subscribed; `override_callback_uri` for Chambeando on the Test WABA correctly points to the stable callback (verified via `GET /{waba-id}/subscribed_apps`).
- Named Tunnel (`chambeando-webhook`, Cloudflare Named Tunnel, not a quick/ephemeral tunnel) stayed connected and stable throughout every observation window used below — no reconnects or errors during the actual test periods.
- Outbound `hello_world` template sent from the demo number `+1 555-678-4943` to `+1 561-679-0314` via Meta's native test panel: delivered and visibly received on the physical phone.
- Meta's own dashboard event log ("Comprobar webhooks de prueba") did generate a real `messages` field event with `"status": "delivered"` for that outbound message:
  - Timestamp: **2026-09-21, 10:08:32 PM ET**
  - wamid: `wamid.HBgLMTU2MTY3OTAzMTQVAgARGBIyN0FEMkNERDlGOUE0QzUwNjgA`
- Synthetic/verification events reliably reach the backend: the callback-verification `GET` handshake succeeded, and Meta's dashboard-triggered synthetic "Test" sends have historically returned HTTP 200 from our backend (separately confirmed in an earlier session against the previous tunnel).

## What's confirmed broken

- **No real webhook POST of any kind has reached the tunnel or backend** during any monitored observation window on the new stable callback, despite:
  - the inbound real `START` message sent from `+1 561-679-0314`, which Meta's own dashboard captured (wamid `wamid.HBgLMTU2MTY3OTAzMTQVAgASGBYzRUIwRjY3RjBCMTI3NDZGOEU2NDA2AA==`, timestamp 2026-09-21 9:53:28 PM ET, ~7 minutes after the new tunnel was live and already verified stable) — zero POST arrived.
  - the outbound `hello_world` control test above: phone received the message, Meta's dashboard logged a `delivered` status event, but that status webhook also never reached the backend (checked over a 3+ minute window immediately after the 10:08:32 PM ET timestamp).
- This rules out: dead/expired tunnel (new tunnel independently verified live and stable both times), wrong `override_callback_uri` (verified correct via API), wrong field subscription (verified `messages` is subscribed), and tunnel instability at the moment of the event (no reconnects logged during either window).

## Interpretation

Real (non-synthetic) `messages` field events — both inbound (`START`) and outbound status updates (`delivered`) — are captured by Meta's own infrastructure but never dispatched as an HTTP POST to a correctly configured, verified-reachable, verified-subscribed callback. Only Meta-initiated synthetic/verification requests (the dashboard "Test" button, the webhook verification handshake) reliably reach us. This looks like a dispatch-path issue specific to real events for this WABA/subscription, not a configuration or infrastructure problem on our side.

## Second control test — 2026-09-22

Re-ran the pre-flight checks and one more outbound control test, with configuration still frozen (nothing changed since 2026-09-21).

Pre-flight (read-only):

- `BACKEND_ALIVE = YES`
- `PUBLIC_CALLBACK_ALIVE = YES`
- `TUNNEL_CONNECTED = YES`
- `SUBSCRIPTION_OK = YES`

Control test (outbound `hello_world`, demo number `+1 555-678-4943` → `+1 561-679-0314`):

- `OUTBOUND_SENT = YES`
- `PHONE_RECEIVED = YES`
- `META_STATUS_EVENT_GENERATED = YES`
- `REAL_STATUS_WEBHOOK_RECEIVED = NO`
- Meta timestamp: `22/9/2026 8:00:48 AM ET` (epoch `1790078447`)
- wamid: `wamid.HBgLMTU2MTY3OTAzMTQVAgARGBI4QzkzRDgyRjEzQzI4RTJFMUMA`
- Named Tunnel stayed at 4/4 ready connections for the full observation window — no reconnects, no errors.
- Zero requests of any kind reached `/whatsapp/webhook` during the 3+ minute observation window following the timestamp above.
- No configuration was changed before, during, or after this test.

## Three confirmed reproductions of the same pattern

1. **Inbound real `START`** (2026-09-21, 9:53:28 PM ET) — Meta's own dashboard captured/generated the event; zero POST reached the callback.
2. **Outbound real `hello_world`** (2026-09-21, 10:08:32 PM ET) — phone received the message; Meta generated a `delivered` status event; zero POST reached the callback.
3. **Outbound real `hello_world`, second run** (2026-09-22, 8:00:48 AM ET) — same pattern exactly: phone received it, Meta generated `delivered`, zero POST reached the callback.

All three occurred against the same stable, independently-verified-healthy Named Tunnel and callback, with unchanged configuration throughout.

## Classification

- `VERIFIED = real-event dispatch failure reproduced` — three independent real events (1 inbound, 2 outbound status), on two different days, all failed to dispatch to a verified-correct, verified-stable callback.
- `INFERENCE = issue is upstream of our stable callback/backend` — our endpoint, tunnel, subscription, and field configuration have all been independently verified correct and healthy at the moment of each failure; only Meta-initiated synthetic/verification traffic (dashboard "Test" button, webhook verification handshake) reliably arrives.
- `UNKNOWN = exact Meta-side cause` — no visibility into why Meta's own infrastructure generates/captures these real events internally but does not attempt the corresponding HTTP dispatch to a correctly subscribed, correctly configured callback.

## Next steps (not yet executed)

- Decide whether to re-escalate to Meta Support/Developer Community with this accumulated evidence, or attempt a different WABA (e.g. a WABA not shared with other Meta system apps) to see if the anomaly is specific to this particular WABA.

## Configuration frozen at time of writing (do not change without explicit authorization)

- App mode: Live/Published
- Business Verification: Verified
- `override_callback_uri`: `https://chambeando-webhook.link-credit.com/whatsapp/webhook`
- `messages` field: subscribed
- Named Tunnel: `chambeando-webhook`, routed to `http://127.0.0.1:8000`
- AutomatIA Bot: not subscribed to this WABA, untouched throughout

## Phase 2: dedicated WABA + independent control app (2026-09-22)

To rule out anything specific to the shared Test WABA or to Chambeando's own app/token/System User, the investigation moved to Chambeando's own **business-owned dedicated WABA** and built a second, fully independent app as a control.

### Identifiers (Phase 2)

- Dedicated WABA ID: `1718523339223795` (`account_review_status: APPROVED`, `business_verification_status: verified`, owned by DELIVERYLINK LLC portfolio `1321330802770654`)
- Dedicated Phone Number ID: `1333620743164204`, `+1 561-559-5352` — registered to Cloud API this session (`platform_type: CLOUD_API`, `status: CONNECTED`, `code_verification_status: VERIFIED`, `name_status: NON_EXISTS`)
- Original app: Chambeando, `2278373642738035` — existing System User `chambeando-system`, existing token, existing tunnel (`chambeando-webhook`, port 8000)
- **Control app**: Chambeando Cloud Clean, `1803570893998369` — brand-new app, own System User `chambeando-cleansystem` (id `61594361894512`, scopes `whatsapp_business_management` + `whatsapp_business_messaging` only), own token, own Named Tunnel (`chambeando-clean-webhook`, port 8001), own minimal receiver (no router, no database, no auto-replies — verification handshake + signature validation + sanitized event logging only)
- Both apps additively coexist in `GET /1718523339223795/subscribed_apps` with no `override_callback_uri` on either entry — confirmed via read-only GET immediately after subscribing the control app.

### Test A — inbound real `START`, both apps monitored simultaneously

Luis sent one real `START` from `+1 561-679-0314` to `+1 561-559-5352`, with both receivers/tunnels independently verified alive beforehand and both logs monitored live for 3 minutes.

- `CLEAN_WEBHOOK_POST_RECEIVED = NO`
- `ORIGINAL_WEBHOOK_POST_RECEIVED = NO`

Zero real webhook POSTs reached either app. (One stale `POST /whatsapp/webhook` line pre-existing in the original app's log, from an earlier reproduction, was re-verified as stale — not a new event — via file mtime/line-count comparison before concluding.)

### Test B — outbound free-form text, control app only

Using only the control app's own token (never used for anything else), one plain free-form text (`"Prueba Chambeando Clean"`, no template) was sent from the dedicated number to `+1 561-679-0314`.

- `CLEAN_SEND_HTTP_STATUS = 500`
- `CLEAN_SEND_ACCEPTED = NO`
- Error: `{"error":{"message":"An unknown error has occurred.","code":1,"type":"OAuthException","fbtrace_id":"AWPZV0F7PZIvh4Ns-OOBu9T"}}`

Identical failure signature to the original app's own outbound attempts.

### All four outbound `fbtrace_id`s on record

1. `ASHpTzbPXMFuVD8GrEmhYAO` — original app, template send (`hello_world`), immediately after dedicated-phone registration
2. `AA4u2vi5kUIhuz_Kc5Yqj3_` — original app, free-form text, ~25 min after registration
3. `Aiwoh9BlkgQVsZ91ZmQkDXK` — original app, free-form text, ~62–64 min after registration (rules out propagation delay)
4. `AWPZV0F7PZIvh4Ns-OOBu9T` — **control app** (Chambeando Cloud Clean), free-form text, first-ever call from this app/token

## Final classification (2026-09-22, frozen)

- `VERIFIED`:
  - Two independent Meta apps (`2278373642738035` original, `1803570893998369` control), two independent System Users/tokens, two independent webhook receivers/tunnels, tested against the same dedicated WABA/phone.
  - Both apps receive **no real inbound webhook** (Test A, simultaneous monitoring, zero POSTs on either side).
  - Outbound `/messages` on **both** apps returns the identical `HTTP 500 / OAuthException code 1` (4 reproductions total across both apps).
  - WABA `1718523339223795`: `APPROVED`. Phone `1333620743164204`: `VERIFIED` / `CONNECTED` / `CLOUD_API`.
- `INFERENCE`: the failure is upstream of any app-specific code or configuration — nothing shared between the two failing apps except the WABA/phone/business asset itself.
- `UNKNOWN`: whether the exact root cause sits at the WABA level, the phone asset level, the Business Portfolio level, or in Meta's own infrastructure for this business — no visibility into Meta's internal systems from outside.

**Evidence preserved as-is, nothing deleted, no further Meta-side actions authorized:** original app, control app, dedicated WABA, dedicated phone, both webhook receivers/tunnels (`chambeando-webhook` port 8000, `chambeando-clean-webhook` port 8001), all logs, all four `fbtrace_id`s, Test A and Test B results as recorded above.
