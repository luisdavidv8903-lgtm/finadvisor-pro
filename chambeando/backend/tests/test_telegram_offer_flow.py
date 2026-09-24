"""
Telegram V1 pilot smoke test -- drives TelegramConversationRouter.handle_inbound
directly (same pattern as test_whatsapp_conversation_flow.py), with a
SandboxTelegramClient to inspect every outbound message. Covers the full
COMPRO/VENDO -> MATCH -> transition path end to end, plus the suspended-
identity rejection gate.
"""
import pytest

from backend.messaging.telegram_adapter import SandboxTelegramClient, TelegramAdapter
from backend.messaging.telegram_router import TelegramConversationRouter
from backend.models import ChannelIdentityDB, MembershipStatus, OfferStatus, P2POfferDB, TelegramMembershipDB
from backend.services.telegram_offers import transition_offer_status


@pytest.fixture()
def sandbox():
    client = SandboxTelegramClient()
    router = TelegramConversationRouter(TelegramAdapter(client))
    return router, client


def _last_text(client: SandboxTelegramClient) -> str:
    return client.sent[-1][1]


def _create_offer(db_session, router, client, chat_id: str, side_command: str, amount: str, currency: str, rate: str, method: str, location: str) -> None:
    router.handle_inbound(db_session, chat_id, "/START")
    router.handle_inbound(db_session, chat_id, side_command)
    router.handle_inbound(db_session, chat_id, amount)
    router.handle_inbound(db_session, chat_id, currency)
    router.handle_inbound(db_session, chat_id, rate)
    router.handle_inbound(db_session, chat_id, method)
    router.handle_inbound(db_session, chat_id, location)
    router.handle_inbound(db_session, chat_id, "SI")


def test_full_compro_vendo_match_flow(db_session, sandbox):
    router, client = sandbox

    _create_offer(db_session, router, client, "1001", "COMPRO", "100", "USD", "320", "cash", "Havana")
    assert "publicada" in _last_text(client).lower()

    _create_offer(db_session, router, client, "2002", "VENDO", "100", "USD", "320", "cash", "Havana")
    assert "publicada" in _last_text(client).lower()

    offers = db_session.query(P2POfferDB).order_by(P2POfferDB.id).all()
    assert len(offers) == 2
    assert all(o.status == OfferStatus.OPEN for o in offers)

    router.handle_inbound(db_session, "1001", "MATCH")
    assert "1." in _last_text(client)

    router.handle_inbound(db_session, "1001", "1")
    assert "match confirmado" in _last_text(client).lower()

    db_session.refresh(offers[0])
    db_session.refresh(offers[1])
    assert offers[0].status == OfferStatus.MATCHED
    assert offers[1].status == OfferStatus.MATCHED
    assert offers[0].matched_offer_id == offers[1].id
    assert offers[1].matched_offer_id == offers[0].id

    buyer_identity = db_session.query(ChannelIdentityDB).filter(ChannelIdentityDB.channel_user_id == "1001").first()
    updated = transition_offer_status(db_session, buyer_identity, offers[0], OfferStatus.PAYMENT_PENDING)
    assert updated.status == OfferStatus.PAYMENT_PENDING


def test_suspended_identity_cannot_create_offer(db_session, sandbox):
    router, client = sandbox

    router.handle_inbound(db_session, "3003", "/START")
    identity = db_session.query(ChannelIdentityDB).filter(ChannelIdentityDB.channel_user_id == "3003").first()
    membership = db_session.query(TelegramMembershipDB).filter(TelegramMembershipDB.channel_identity_id == identity.id).first()

    membership.status = MembershipStatus.SUSPENDED
    db_session.commit()

    router.handle_inbound(db_session, "3003", "COMPRO")
    assert "suspendida" in _last_text(client).lower()
    assert db_session.query(P2POfferDB).count() == 0


def test_numeric_menu_routes_to_compro_flow(db_session, sandbox):
    router, client = sandbox

    router.handle_inbound(db_session, "4004", "/START")
    router.handle_inbound(db_session, "4004", "1")
    assert "monto" in _last_text(client).lower()

    router.handle_inbound(db_session, "4004", "100")
    router.handle_inbound(db_session, "4004", "USD")
    router.handle_inbound(db_session, "4004", "320")
    router.handle_inbound(db_session, "4004", "cash")
    router.handle_inbound(db_session, "4004", "Havana")
    router.handle_inbound(db_session, "4004", "SI")
    assert "publicada" in _last_text(client).lower()


def test_numeric_menu_rejects_out_of_range(db_session, sandbox):
    router, client = sandbox

    router.handle_inbound(db_session, "4005", "/START")
    router.handle_inbound(db_session, "4005", "9")
    assert "invalida" in _last_text(client).lower()


def test_offers_browse_is_read_only(db_session, sandbox):
    router, client = sandbox

    _create_offer(db_session, router, client, "4006", "VENDO", "250", "USD", "330", "zelle", "Matanzas")

    router.handle_inbound(db_session, "4007", "/START")
    router.handle_inbound(db_session, "4007", "3")
    assert "ofertas disponibles" in _last_text(client).lower()
    assert "vendo" in _last_text(client).lower()

    # Purely informational: no selection state, "MENU" still works normally after.
    router.handle_inbound(db_session, "4007", "MENU")
    assert "menu principal" in _last_text(client).lower()


def test_my_offers_numeric_action_menu_cancel_flow(db_session, sandbox):
    router, client = sandbox

    _create_offer(db_session, router, client, "4008", "COMPRO", "100", "USD", "320", "cash", "Havana")

    router.handle_inbound(db_session, "4008", "MIS OFERTAS")
    assert "tus ofertas" in _last_text(client).lower()

    router.handle_inbound(db_session, "4008", "1")
    assert "cancelar oferta" in _last_text(client).lower()

    router.handle_inbound(db_session, "4008", "1")  # select "Cancelar oferta"
    assert "cancelar la oferta" in _last_text(client).lower()

    router.handle_inbound(db_session, "4008", "2")  # "No, volver"
    assert "menu principal" in _last_text(client).lower()

    offer = db_session.query(P2POfferDB).filter(P2POfferDB.channel_identity_id.isnot(None)).order_by(P2POfferDB.id.desc()).first()
    assert offer.status == OfferStatus.OPEN  # untouched -- declined the confirmation

    router.handle_inbound(db_session, "4008", "MIS OFERTAS")
    router.handle_inbound(db_session, "4008", "1")
    router.handle_inbound(db_session, "4008", "1")
    router.handle_inbound(db_session, "4008", "1")  # "Si, cancelar"
    assert "cancelada" in _last_text(client).lower()

    db_session.refresh(offer)
    assert offer.status == OfferStatus.CANCELLED


def test_my_offers_selection_out_of_range_resets_to_menu(db_session, sandbox):
    router, client = sandbox

    _create_offer(db_session, router, client, "4009", "COMPRO", "100", "USD", "320", "cash", "Havana")

    router.handle_inbound(db_session, "4009", "MIS OFERTAS")
    router.handle_inbound(db_session, "4009", "9")
    assert "invalido" in _last_text(client).lower()


def test_my_offers_stale_offer_revalidated(db_session, sandbox):
    router, client = sandbox

    _create_offer(db_session, router, client, "4010", "COMPRO", "100", "USD", "320", "cash", "Havana")
    router.handle_inbound(db_session, "4010", "MIS OFERTAS")

    offer = db_session.query(P2POfferDB).order_by(P2POfferDB.id.desc()).first()
    db_session.delete(offer)  # simulate the offer no longer being available by the time it's selected
    db_session.commit()

    router.handle_inbound(db_session, "4010", "1")
    assert "ya no esta disponible" in _last_text(client).lower()
