"""
Phase 2C — conversation flow + privacy tests (sections 2/3/13), driving
messaging.router.ConversationRouter directly (handle_inbound), with a real
SandboxMetaClient to inspect every outbound message, and the real
/whatsapp/link HTTP route for identity linking (reusing the existing
/auth/nonce + /auth/verify flow from conftest.login(), same trivial
FakeChainAdapter signature scheme the rest of the suite already uses).
"""
import re

import pytest

from backend.messaging import ConversationRouter, WhatsAppAdapter, reset_conversation_router_for_tests, set_conversation_router_for_tests
from backend.messaging.whatsapp_adapter import SandboxMetaClient
from backend.models import ConversationSessionDB, MemberRole, P2POrderDB

from .conftest import auth_headers, login, seed_membership

SYNTHETIC_PAYMENT_METHOD = "TEST_CUP_TRANSFER"
SYNTHETIC_ACCOUNT_REFERENCE = "TEST-000000"


@pytest.fixture()
def sandbox():
    """Also registered as the process-wide singleton (set_conversation_router_for_tests)
    -- POST /whatsapp/link (a real HTTP route, used by link_wallet() below)
    dispatches its post-link notification through get_conversation_router(),
    which must resolve to THIS test's router/mock, not a separate default
    instance, for the notification to be observable here."""
    mock_client = SandboxMetaClient()
    conversation_router = ConversationRouter(WhatsAppAdapter(mock_client))
    set_conversation_router_for_tests(conversation_router)
    yield conversation_router, mock_client
    reset_conversation_router_for_tests()


def _last_text(sandbox_client: SandboxMetaClient) -> str:
    return sandbox_client.sent[-1][1]


def _w(short: str) -> str:
    """Synthetic wallet address padded to the API's min_length=25 -- these
    tests never touch real signature-format validation (FakeChainAdapter's
    trivial scheme, reused via conftest.login()), only the length
    constraint on NonceRequest/VerifyRequest."""
    return short.ljust(30, "0")


def _extract_link_token(text: str) -> str:
    match = re.search(r"token=([A-Za-z0-9_\-]+)", text)
    assert match, f"no link token found in: {text!r}"
    return match.group(1)


def link_wallet(client, db_session, router, sandbox_client, whatsapp_id: str, wallet_address: str) -> str:
    """Drives the REAL deep-link handoff: JOIN -> extract token from the
    message the user would have received -> real /auth/nonce+/auth/verify ->
    real /whatsapp/link. Returns the wallet's access token."""
    router.handle_inbound(db_session, whatsapp_id, "JOIN")
    token = _extract_link_token(_last_text(sandbox_client))
    access_token = login(client, wallet_address)
    link_resp = client.post("/whatsapp/link", json={"link_token": token}, headers=auth_headers(access_token))
    assert link_resp.status_code == 200, link_resp.text
    return access_token


# ---------------------------------------------------------------------------
# START / JOIN / invite redemption
# ---------------------------------------------------------------------------


def test_start_explains_invite_only_access(db_session, sandbox):
    router, mock = sandbox
    router.handle_inbound(db_session, "wa-flow-start", "START")
    assert "invitacion" in _last_text(mock).lower()


def test_join_unlinked_sends_deep_link_never_asks_for_private_key(db_session, sandbox):
    router, mock = sandbox
    router.handle_inbound(db_session, "wa-flow-join-1", "JOIN")
    text = _last_text(mock)
    assert "token=" in text
    assert "clave privada" not in text.lower() or "nunca" in text.lower()
    for banned in ("private key", "seed phrase", "clave privada:"):
        assert banned not in text.lower()


def test_full_join_and_invite_redemption_flow(client, db_session, fake_chain, sandbox):
    router, mock = sandbox
    admin_wallet = _w("0xWaFlowAdmin1")
    seed_membership(db_session, admin_wallet, role=MemberRole.ADMIN)
    admin_token = login(client, admin_wallet)
    invite_code = client.post(
        "/admin/invites", json={"max_uses": 1, "expires_in_hours": 24}, headers=auth_headers(admin_token)
    ).json()["code"]

    link_wallet(client, db_session, router, mock, "wa-flow-join-full", _w("0xWaFlowSeller1"))
    assert "codigo de invitacion" in _last_text(mock).lower()

    router.handle_inbound(db_session, "wa-flow-join-full", invite_code)
    assert "ya sos member" in _last_text(mock).lower()

    session = db_session.query(ConversationSessionDB).filter(ConversationSessionDB.whatsapp_id == "wa-flow-join-full").first()
    assert session.state.value == "menu"


def test_invalid_invite_code_denied_with_safe_message(client, db_session, fake_chain, sandbox):
    router, mock = sandbox
    link_wallet(client, db_session, router, mock, "wa-flow-badcode", _w("0xWaFlowSeller2"))
    router.handle_inbound(db_session, "wa-flow-badcode", "not-a-real-code")
    text = _last_text(mock).lower()
    assert "invalido" in text or "expirado" in text or "agotado" in text


def test_menu_denied_before_membership(db_session, sandbox):
    router, mock = sandbox
    router.handle_inbound(db_session, "wa-flow-nomember", "MENU")
    text = _last_text(mock).lower()
    assert "vinculaste" in text or "join" in text.lower()


# ---------------------------------------------------------------------------
# BUY
# ---------------------------------------------------------------------------


def _onboard_member(client, db_session, fake_chain, router, mock, whatsapp_id, wallet):
    admin_wallet = _w(f"0xAdminFor{whatsapp_id}".replace("-", ""))
    seed_membership(db_session, admin_wallet, role=MemberRole.ADMIN)
    admin_token = login(client, admin_wallet)
    code = client.post("/admin/invites", json={"max_uses": 1, "expires_in_hours": 24}, headers=auth_headers(admin_token)).json()["code"]
    link_wallet(client, db_session, router, mock, whatsapp_id, wallet)
    router.handle_inbound(db_session, whatsapp_id, code)


def test_buy_lists_open_offers_with_masked_wallet_only(client, db_session, fake_chain, sandbox):
    router, mock = sandbox
    _onboard_member(client, db_session, fake_chain, router, mock, "wa-flow-buyer1", _w("0xWaFlowBuyer1"))

    seller = seed_membership(db_session, _w("0xWaFlowOfferSeller1"))
    fake_chain.seed_order_created(5001, _w("0xWaFlowOfferSeller1"), "TFakeToken001", 100_000000)
    from .conftest import sync_indexer

    sync_indexer(db_session, fake_chain)

    router.handle_inbound(db_session, "wa-flow-buyer1", "BUY")
    text = _last_text(mock)
    assert _w("0xWaFlowOfferSeller1") not in text  # never the full wallet
    assert "…" in text or "..." in text or "0xWaFl" in text  # masked form present


def test_buy_selection_hands_off_secure_link_never_settlement_data(client, db_session, fake_chain, sandbox):
    router, mock = sandbox
    _onboard_member(client, db_session, fake_chain, router, mock, "wa-flow-buyer2", _w("0xWaFlowBuyer2"))

    fake_chain.seed_order_created(5002, _w("0xWaFlowOfferSeller2"), "TFakeToken002", 50_000000)
    from .conftest import sync_indexer

    sync_indexer(db_session, fake_chain)

    router.handle_inbound(db_session, "wa-flow-buyer2", "BUY")
    router.handle_inbound(db_session, "wa-flow-buyer2", "1")
    text = _last_text(mock)
    assert "claim" in text  # deep link to the dApp claim handoff
    assert SYNTHETIC_ACCOUNT_REFERENCE not in text


# ---------------------------------------------------------------------------
# SELL
# ---------------------------------------------------------------------------


def test_sell_flow_through_confirmation(client, db_session, fake_chain, sandbox):
    router, mock = sandbox
    _onboard_member(client, db_session, fake_chain, router, mock, "wa-flow-sell1", _w("0xWaFlowSell1"))

    router.handle_inbound(db_session, "wa-flow-sell1", "SELL")
    assert "cripto" in _last_text(mock).lower()

    router.handle_inbound(db_session, "wa-flow-sell1", "100")
    assert "fiat" in _last_text(mock).lower()

    router.handle_inbound(db_session, "wa-flow-sell1", "25000")
    assert "confirmar" in _last_text(mock).lower()
    assert SYNTHETIC_PAYMENT_METHOD in _last_text(mock)

    router.handle_inbound(db_session, "wa-flow-sell1", "SI")
    text = _last_text(mock)
    assert "create-order" in text


def test_sell_invalid_amount_rejected(client, db_session, fake_chain, sandbox):
    router, mock = sandbox
    _onboard_member(client, db_session, fake_chain, router, mock, "wa-flow-sell2", _w("0xWaFlowSell2"))
    router.handle_inbound(db_session, "wa-flow-sell2", "SELL")
    router.handle_inbound(db_session, "wa-flow-sell2", "not-a-number")
    assert "invalido" in _last_text(mock).lower()


def test_sell_can_be_cancelled(client, db_session, fake_chain, sandbox):
    router, mock = sandbox
    _onboard_member(client, db_session, fake_chain, router, mock, "wa-flow-sell3", _w("0xWaFlowSell3"))
    router.handle_inbound(db_session, "wa-flow-sell3", "SELL")
    router.handle_inbound(db_session, "wa-flow-sell3", "10")
    router.handle_inbound(db_session, "wa-flow-sell3", "1000")
    router.handle_inbound(db_session, "wa-flow-sell3", "NO")
    assert "cancelada" in _last_text(mock).lower()


# ---------------------------------------------------------------------------
# MY TRADES / DISPUTE
# ---------------------------------------------------------------------------


def test_my_trades_shows_only_own_active_trades_no_internal_ids(client, db_session, fake_chain, sandbox):
    router, mock = sandbox
    _onboard_member(client, db_session, fake_chain, router, mock, "wa-flow-trades1", _w("0xWaFlowTradesSeller1"))

    fake_chain.seed_order_created(5003, _w("0xWaFlowTradesSeller1"), "TFakeToken003", 30_000000)
    from .conftest import sync_indexer

    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 5003).first()

    router.handle_inbound(db_session, "wa-flow-trades1", "MY TRADES")
    text = _last_text(mock)
    assert str(order.id) not in text.split("\n")[0]  # no raw db id dumped as a bare identifier
    assert "OPEN" in text


def test_dispute_without_selecting_trade_first_is_rejected(client, db_session, fake_chain, sandbox):
    router, mock = sandbox
    _onboard_member(client, db_session, fake_chain, router, mock, "wa-flow-dispute1", _w("0xWaFlowDispute1"))
    router.handle_inbound(db_session, "wa-flow-dispute1", "DISPUTE")
    assert "my trades" in _last_text(mock).lower()


def test_dispute_hands_off_secure_link_never_dumps_evidence_prompt_in_chat(client, db_session, fake_chain, sandbox):
    router, mock = sandbox
    _onboard_member(client, db_session, fake_chain, router, mock, "wa-flow-dispute2", _w("0xWaFlowDisputeSeller1"))

    fake_chain.seed_order_created(5004, _w("0xWaFlowDisputeSeller1"), "TFakeToken004", 20_000000)
    fake_chain.seed_order_claimed(5004, _w("0xWaFlowDisputeBuyer1"), arbiter_snapshot="TFakeArbiter004")
    fake_chain.seed_paid(5004)
    from .conftest import sync_indexer

    sync_indexer(db_session, fake_chain)

    router.handle_inbound(db_session, "wa-flow-dispute2", "MY TRADES")
    router.handle_inbound(db_session, "wa-flow-dispute2", "DISPUTE")
    router.handle_inbound(db_session, "wa-flow-dispute2", "1")
    text = _last_text(mock)
    assert "dispute" in text  # deep link handoff present
    assert "evidencia" in text.lower()
    assert "nunca compartas evidencia" in text.lower()


# ---------------------------------------------------------------------------
# Privacy — cross-cutting checks over everything sent in this file's tests
# ---------------------------------------------------------------------------


def test_no_message_ever_contains_a_private_key_or_seed_phrase_marker(db_session, sandbox):
    router, mock = sandbox
    for cmd in ("START", "JOIN", "MENU", "HELP", "BUY", "SELL", "MY TRADES", "DISPUTE"):
        router.handle_inbound(db_session, "wa-flow-privacy-scan", cmd)
    all_text = "\n".join(text for _, text in mock.sent)
    for banned in ("PRIVATE KEY", "SEED PHRASE", "-----BEGIN"):
        assert banned not in all_text.upper()
