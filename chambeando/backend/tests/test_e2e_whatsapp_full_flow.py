"""
Phase 2C section 10/11 — full synthetic WhatsApp E2E (seller + buyer, happy
path and dispute/refund) plus the failure-case matrix. Reuses
test_whatsapp_conversation_flow.py's link_wallet()/_w() helpers rather than
re-deriving the identity-linking dance, and conftest's client/db_session/
fake_chain/login/seed_membership -- exactly the same layering discipline as
Phase 2C's non-WhatsApp E2E suite (test_e2e_full_flow.py): every state
transition goes through either a real HTTP route or a direct chain-adapter
event (representing the on-chain wallet action WhatsApp itself never
performs).
"""
import pytest

from backend.messaging import ConversationRouter, WhatsAppAdapter, reset_conversation_router_for_tests, set_conversation_router_for_tests
from backend.messaging.notifications import OrderNotificationEvent, notify_order_event
from backend.messaging.whatsapp_adapter import SandboxMetaClient
from backend.models import MemberRole, MembershipStatus, OrderStatus, P2POrderDB

from .conftest import auth_headers, login, seed_membership, sync_indexer
from .test_whatsapp_conversation_flow import SYNTHETIC_ACCOUNT_REFERENCE, SYNTHETIC_PAYMENT_METHOD, _w, link_wallet

SYNTHETIC_TOKEN_ADDRESS = "TFakeE2EWhatsAppToken001"


@pytest.fixture()
def wa(db_session):
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock))
    set_conversation_router_for_tests(router)
    yield router, mock
    reset_conversation_router_for_tests()


def _last(mock: SandboxMetaClient, whatsapp_id: str | None = None) -> str:
    if whatsapp_id is None:
        return mock.sent[-1][1]
    for to, text in reversed(mock.sent):
        if to == whatsapp_id:
            return text
    raise AssertionError(f"no message sent to {whatsapp_id!r}")


def _make_admin_and_invite(client, db_session, suffix: str) -> str:
    admin_wallet = _w(f"0xE2EWaAdmin{suffix}")
    seed_membership(db_session, admin_wallet, role=MemberRole.ADMIN)
    admin_token = login(client, admin_wallet)
    return client.post("/admin/invites", json={"max_uses": 1, "expires_in_hours": 24}, headers=auth_headers(admin_token)).json()["code"]


def _onboard(client, db_session, router, mock, whatsapp_id: str, wallet: str, invite_code: str) -> str:
    """Full real onboarding: JOIN -> real wallet link (nonce+verify+/whatsapp/link)
    -> invite redemption. Returns the wallet's access token (for the parts of
    the flow this test still drives via direct HTTP, mirroring the mocked
    dApp -- see module docstring)."""
    token = link_wallet(client, db_session, router, mock, whatsapp_id, wallet)
    router.handle_inbound(db_session, whatsapp_id, invite_code)
    return token


# ---------------------------------------------------------------------------
# Section 10 — happy path
# ---------------------------------------------------------------------------


def test_synthetic_whatsapp_happy_path_full_trade(client, db_session, fake_chain, wa):
    router, mock = wa
    seller_wallet, buyer_wallet = _w("0xE2EWaSeller1"), _w("0xE2EWaBuyer1")

    seller_token = _onboard(client, db_session, router, mock, "wa-e2e-seller", seller_wallet, _make_admin_and_invite(client, db_session, "HP1"))
    buyer_token = _onboard(client, db_session, router, mock, "wa-e2e-buyer", buyer_wallet, _make_admin_and_invite(client, db_session, "HP2"))

    # --- SELLER creates offer via WhatsApp ---
    router.handle_inbound(db_session, "wa-e2e-seller", "SELL")
    router.handle_inbound(db_session, "wa-e2e-seller", "100")
    router.handle_inbound(db_session, "wa-e2e-seller", "25000")
    router.handle_inbound(db_session, "wa-e2e-seller", "SI")
    assert "create-order" in _last(mock, "wa-e2e-seller")

    # --- (mocked dApp) wallet signs createOrder on-chain -> indexer picks it up ---
    onchain_id = 7001
    fake_chain.seed_order_created(onchain_id, seller_wallet, SYNTHETIC_TOKEN_ADDRESS, 100_000000)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()
    assert order.onchain_status == OrderStatus.OPEN

    # --- (mocked dApp) attach the metadata the seller drafted over WhatsApp ---
    detail = client.post(
        "/settlement-details",
        json={"payment_method": SYNTHETIC_PAYMENT_METHOD, "currency": "CUP", "payload": {"account_reference": SYNTHETIC_ACCOUNT_REFERENCE}},
        headers=auth_headers(seller_token),
    ).json()
    meta = client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": onchain_id,
            "fiat_amount": "25000.00",
            "fiat_currency": "CUP",
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "settlement_detail_id": detail["id"],
        },
        headers=auth_headers(seller_token),
    )
    assert meta.status_code == 201, meta.text

    # --- BUYER sees the offer over WhatsApp ---
    router.handle_inbound(db_session, "wa-e2e-buyer", "BUY")
    assert "cripto" in _last(mock, "wa-e2e-buyer").lower()
    router.handle_inbound(db_session, "wa-e2e-buyer", "1")
    assert "claim" in _last(mock, "wa-e2e-buyer")

    # --- (mocked dApp) buyer's wallet claims on-chain ---
    fake_chain.seed_order_claimed(onchain_id, buyer_wallet, arbiter_snapshot="TFakeE2EArbiter001")
    sync_indexer(db_session, fake_chain)
    notify_order_event(router._adapter, db_session, order, OrderNotificationEvent.CLAIMED, recipients=("seller",))
    assert "claimed" in _last(mock, "wa-e2e-seller").lower() or "tomada" in _last(mock, "wa-e2e-seller").lower()
    assert SYNTHETIC_ACCOUNT_REFERENCE not in _last(mock, "wa-e2e-seller")

    # --- secure settlement handoff: buyer reveals via the real, authenticated route ---
    reveal = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(buyer_token))
    assert reveal.status_code == 200
    assert reveal.json()["payload"]["account_reference"] == SYNTHETIC_ACCOUNT_REFERENCE

    # --- (mocked dApp) buyer marks fiat paid on-chain ---
    fake_chain.seed_paid(onchain_id)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).get(order.id)
    notify_order_event(router._adapter, db_session, order, OrderNotificationEvent.PAID, recipients=("seller",))
    assert SYNTHETIC_ACCOUNT_REFERENCE not in _last(mock, "wa-e2e-seller")

    # --- (mocked dApp) seller releases on-chain ---
    fake_chain.seed_settled(onchain_id, "RELEASED")
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).get(order.id)
    assert order.onchain_status == OrderStatus.RELEASED
    notify_order_event(router._adapter, db_session, order, OrderNotificationEvent.RELEASED, recipients=("seller", "buyer"))

    for wid in ("wa-e2e-seller", "wa-e2e-buyer"):
        text = _last(mock, wid)
        assert "released" in text.lower() or "liberad" in text.lower()
        assert SYNTHETIC_ACCOUNT_REFERENCE not in text
        assert str(order.id) not in text.split("#")[0]  # ref number only ever shown as "#<id>", never bare


# ---------------------------------------------------------------------------
# Section 10 — dispute flow (RELEASED and REFUNDED as separate tests)
# ---------------------------------------------------------------------------


def _build_disputed_trade(client, db_session, fake_chain, router, mock, suffix: str):
    seller_wallet, buyer_wallet = _w(f"0xE2EWaDispSeller{suffix}"), _w(f"0xE2EWaDispBuyer{suffix}")
    seller_token = _onboard(client, db_session, router, mock, f"wa-e2e-disp-seller-{suffix}", seller_wallet, _make_admin_and_invite(client, db_session, f"D{suffix}A"))
    buyer_token = _onboard(client, db_session, router, mock, f"wa-e2e-disp-buyer-{suffix}", buyer_wallet, _make_admin_and_invite(client, db_session, f"D{suffix}B"))

    onchain_id = int(f"70{suffix}")
    fake_chain.seed_order_created(onchain_id, seller_wallet, SYNTHETIC_TOKEN_ADDRESS, 40_000000)
    sync_indexer(db_session, fake_chain)
    fake_chain.seed_order_claimed(onchain_id, buyer_wallet, arbiter_snapshot=f"TFakeE2EArbiterD{suffix}")
    sync_indexer(db_session, fake_chain)
    fake_chain.seed_paid(onchain_id)
    sync_indexer(db_session, fake_chain)
    fake_chain.seed_disputed(onchain_id)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()

    notify_order_event(router._adapter, db_session, order, OrderNotificationEvent.DISPUTE_OPENED, recipients=("seller", "buyer"))

    router.handle_inbound(db_session, f"wa-e2e-disp-buyer-{suffix}", "MY TRADES")
    router.handle_inbound(db_session, f"wa-e2e-disp-buyer-{suffix}", "DISPUTE")
    router.handle_inbound(db_session, f"wa-e2e-disp-buyer-{suffix}", "1")
    handoff_text = _last(mock, f"wa-e2e-disp-buyer-{suffix}")
    assert "evidencia" in handoff_text.lower()

    # (mocked secure page) buyer submits evidence via the real route
    ev = client.post(
        "/disputes/evidence",
        json={"order_id": order.id, "evidence_type": "note", "note": "synthetic: goods not delivered"},
        headers=auth_headers(buyer_token),
    )
    assert ev.status_code == 201

    return order, onchain_id, seller_token, buyer_token


def test_synthetic_whatsapp_dispute_resolves_to_released(client, db_session, fake_chain, wa):
    router, mock = wa
    order, onchain_id, seller_token, buyer_token = _build_disputed_trade(client, db_session, fake_chain, router, mock, "1")

    # scoped moderator review still gated by DisputeAssignment (not WhatsApp/role)
    mod_wallet = _w("0xE2EWaDispMod1")
    mod_token = login(client, mod_wallet)
    seed_membership(db_session, mod_wallet, role=MemberRole.MODERATOR)
    unrelated = client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(mod_token))
    assert unrelated.status_code == 403

    fake_chain.seed_settled(onchain_id, "RELEASED")
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).get(order.id)
    assert order.onchain_status == OrderStatus.RELEASED

    notify_order_event(router._adapter, db_session, order, OrderNotificationEvent.DISPUTE_RESOLVED, recipients=("seller", "buyer"))
    notify_order_event(router._adapter, db_session, order, OrderNotificationEvent.RELEASED, recipients=("seller", "buyer"))
    for wid in ("wa-e2e-disp-seller-1", "wa-e2e-disp-buyer-1"):
        assert SYNTHETIC_ACCOUNT_REFERENCE not in _last(mock, wid)


def test_synthetic_whatsapp_dispute_resolves_to_refunded(client, db_session, fake_chain, wa):
    router, mock = wa
    order, onchain_id, seller_token, buyer_token = _build_disputed_trade(client, db_session, fake_chain, router, mock, "2")

    fake_chain.seed_settled(onchain_id, "REFUNDED")
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).get(order.id)
    assert order.onchain_status == OrderStatus.REFUNDED

    notify_order_event(router._adapter, db_session, order, OrderNotificationEvent.DISPUTE_RESOLVED, recipients=("seller", "buyer"))
    notify_order_event(router._adapter, db_session, order, OrderNotificationEvent.REFUNDED, recipients=("seller", "buyer"))
    for wid in ("wa-e2e-disp-seller-2", "wa-e2e-disp-buyer-2"):
        text = _last(mock, wid)
        assert "refund" in text.lower() or "reembols" in text.lower()
        assert SYNTHETIC_ACCOUNT_REFERENCE not in text


# ---------------------------------------------------------------------------
# Section 11 — failure cases
# ---------------------------------------------------------------------------


def test_unknown_whatsapp_user_denied(db_session, wa):
    router, mock = wa
    router.handle_inbound(db_session, "wa-unknown-never-seen", "BUY")
    assert "vinculaste" in _last(mock).lower()


def test_wallet_linked_but_no_membership_denied(client, db_session, wa):
    router, mock = wa
    wallet = _w("0xE2EWaNoMembership1")
    router.handle_inbound(db_session, "wa-nomembership-1", "JOIN")
    token = login(client, wallet)
    link_token = mock.sent[-1][1].split("token=")[1]
    r = client.post("/whatsapp/link", json={"link_token": link_token}, headers=auth_headers(token))
    assert r.status_code == 200

    router.handle_inbound(db_session, "wa-nomembership-1", "BUY")
    text = _last(mock).lower()
    assert "join" in text and "invite" in text  # denied + told to redeem an invite, never shown the order book


def test_suspended_member_denied_via_whatsapp(client, db_session, wa):
    router, mock = wa
    wallet = _w("0xE2EWaSuspended1")
    code = _make_admin_and_invite(client, db_session, "SUSP1")
    _onboard(client, db_session, router, mock, "wa-suspended-1", wallet, code)

    from backend.models import MembershipDB, UserDB

    user = db_session.query(UserDB).filter(UserDB.wallet_address == wallet).first()
    membership = db_session.query(MembershipDB).filter(MembershipDB.user_id == user.id).first()
    membership.status = MembershipStatus.SUSPENDED
    db_session.commit()

    router.handle_inbound(db_session, "wa-suspended-1", "BUY")
    assert "suspendida" in _last(mock).lower()


def test_expired_invitation_denied_via_whatsapp(client, db_session, wa):
    router, mock = wa
    wallet = _w("0xE2EWaExpiredInv1")
    from datetime import timedelta

    from backend import timeutils

    admin_wallet = _w("0xE2EWaAdminExp1")
    seed_membership(db_session, admin_wallet, role=MemberRole.ADMIN)
    admin_token = login(client, admin_wallet)
    code = client.post("/admin/invites", json={"max_uses": 1, "expires_in_hours": 1}, headers=auth_headers(admin_token)).json()["code"]

    from backend.models import InviteDB
    from backend.services.invites import hash_invite_code

    invite = db_session.query(InviteDB).filter(InviteDB.code_hash == hash_invite_code(code)).first()
    invite.expires_at = timeutils.utcnow() - timedelta(hours=1)
    db_session.commit()

    token = link_wallet(client, db_session, router, mock, "wa-expiredinv-1", wallet)
    router.handle_inbound(db_session, "wa-expiredinv-1", code)
    assert "invalido" in _last(mock).lower() or "expirado" in _last(mock).lower() or "agotado" in _last(mock).lower()


def test_stale_buy_selection_after_order_claimed_by_someone_else(client, db_session, fake_chain, wa):
    """Section 11: "order already claimed" / "stale conversation action" --
    the buyer listed an offer, someone else claimed it before they replied
    with the selection number."""
    router, mock = wa
    seller_wallet = _w("0xE2EWaStaleSeller1")
    buyer_wallet = _w("0xE2EWaStaleBuyer1")
    third_wallet = _w("0xE2EWaStaleThird1")

    _onboard(client, db_session, router, mock, "wa-stale-seller", seller_wallet, _make_admin_and_invite(client, db_session, "ST1"))
    _onboard(client, db_session, router, mock, "wa-stale-buyer", buyer_wallet, _make_admin_and_invite(client, db_session, "ST2"))

    onchain_id = 7301
    fake_chain.seed_order_created(onchain_id, seller_wallet, SYNTHETIC_TOKEN_ADDRESS, 10_000000)
    sync_indexer(db_session, fake_chain)

    router.handle_inbound(db_session, "wa-stale-buyer", "BUY")

    # someone else claims it out from under the buyer before they reply
    fake_chain.seed_order_claimed(onchain_id, third_wallet, arbiter_snapshot="TFakeE2EArbiterStale1")
    sync_indexer(db_session, fake_chain)

    router.handle_inbound(db_session, "wa-stale-buyer", "1")
    assert "ya no esta disponible" in _last(mock, "wa-stale-buyer").lower()


def test_dispute_on_already_finalized_trade_denied(client, db_session, fake_chain, wa):
    router, mock = wa
    seller_wallet = _w("0xE2EWaFinalSeller1")
    buyer_wallet = _w("0xE2EWaFinalBuyer1")
    _onboard(client, db_session, router, mock, "wa-final-seller", seller_wallet, _make_admin_and_invite(client, db_session, "FIN1"))
    _onboard(client, db_session, router, mock, "wa-final-buyer", buyer_wallet, _make_admin_and_invite(client, db_session, "FIN2"))

    onchain_id = 7401
    fake_chain.seed_order_created(onchain_id, seller_wallet, SYNTHETIC_TOKEN_ADDRESS, 10_000000)
    sync_indexer(db_session, fake_chain)
    fake_chain.seed_order_claimed(onchain_id, buyer_wallet, arbiter_snapshot="TFakeE2EArbiterFin1")
    sync_indexer(db_session, fake_chain)
    fake_chain.seed_paid(onchain_id)
    sync_indexer(db_session, fake_chain)
    fake_chain.seed_settled(onchain_id, "RELEASED")
    sync_indexer(db_session, fake_chain)

    router.handle_inbound(db_session, "wa-final-buyer", "MY TRADES")
    router.handle_inbound(db_session, "wa-final-buyer", "DISPUTE")
    router.handle_inbound(db_session, "wa-final-buyer", "1")
    assert "no se puede disputar" in _last(mock, "wa-final-buyer").lower()


def test_unauthorized_settlement_access_via_whatsapp_flow_denied(client, db_session, fake_chain, wa):
    router, mock = wa
    seller_wallet = _w("0xE2EWaUnauthSeller1")
    buyer_wallet = _w("0xE2EWaUnauthBuyer1")
    third_wallet = _w("0xE2EWaUnauthThird1")

    seller_token = _onboard(client, db_session, router, mock, "wa-unauth-seller", seller_wallet, _make_admin_and_invite(client, db_session, "UA1"))
    _onboard(client, db_session, router, mock, "wa-unauth-buyer", buyer_wallet, _make_admin_and_invite(client, db_session, "UA2"))
    third_token = _onboard(client, db_session, router, mock, "wa-unauth-third", third_wallet, _make_admin_and_invite(client, db_session, "UA3"))

    onchain_id = 7501
    fake_chain.seed_order_created(onchain_id, seller_wallet, SYNTHETIC_TOKEN_ADDRESS, 10_000000)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()

    detail = client.post(
        "/settlement-details",
        json={"payment_method": SYNTHETIC_PAYMENT_METHOD, "currency": "CUP", "payload": {"account_reference": SYNTHETIC_ACCOUNT_REFERENCE}},
        headers=auth_headers(seller_token),
    ).json()
    client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": onchain_id,
            "fiat_amount": "10.00",
            "fiat_currency": "CUP",
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "settlement_detail_id": detail["id"],
        },
        headers=auth_headers(seller_token),
    )

    # third party never sees this order in "BUY" as a claimable offer once
    # it doesn't exist yet as OPEN for them either way -- the real check is
    # that the settlement route itself fails closed for a non-party
    r = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(third_token))
    assert r.status_code == 403


def test_unauthorized_dispute_access_via_whatsapp_flow_denied(client, db_session, fake_chain, wa):
    router, mock = wa
    order, onchain_id, seller_token, buyer_token = _build_disputed_trade(client, db_session, fake_chain, router, mock, "9")

    unrelated_wallet = _w("0xE2EWaUnauthDisp1")
    unrelated_token = _onboard(client, db_session, router, mock, "wa-unauth-disp-1", unrelated_wallet, _make_admin_and_invite(client, db_session, "UAD1"))
    r = client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(unrelated_token))
    assert r.status_code == 403


def test_duplicate_webhook_during_whatsapp_flow_is_noop(client, db_session, fake_chain, wa):
    import hashlib
    import hmac
    import json

    from backend.config import settings
    from backend.security.rate_limit import reset_rate_limiter_for_tests

    router, mock = wa
    reset_rate_limiter_for_tests()
    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "TEST_WABA_ID",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {"messages": [{"id": "wamid.e2edup1", "from": "wa-e2e-dup-1", "type": "text", "text": {"body": "START"}}]}},
                    ],
                }
            ],
        }
    ).encode()
    signature = "sha256=" + hmac.new(settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()

    first = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    second = client.post("/whatsapp/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
    assert first.json()["processed"] == 1
    assert second.json() == {"processed": 0, "duplicates": 1}
    assert len(mock.sent) == 1
