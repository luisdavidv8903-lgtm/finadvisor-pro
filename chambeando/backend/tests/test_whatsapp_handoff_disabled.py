"""
Phase 2D.1 section 11 — "Keep real wallet-signing handoff disabled unless
there is a genuine HTTPS Chambeando test endpoint... it is acceptable for
signature-required actions to reply with a safe test message indicating
that secure wallet handoff is not yet enabled."
"""
from backend.messaging.router import SAFE_HANDOFF_DISABLED_TEXT, ConversationRouter
from backend.messaging.whatsapp_adapter import SandboxMetaClient, WhatsAppAdapter


def test_join_handoff_disabled_sends_safe_message_not_a_dead_link(db_session):
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=False)

    router.handle_inbound(db_session, "wa-handoff-disabled-1", "JOIN")

    to, text = mock.sent[-1]
    assert text == SAFE_HANDOFF_DISABLED_TEXT
    assert "http" not in text  # no dead link sent


def test_join_handoff_enabled_by_default_still_sends_the_link(db_session):
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock))  # wallet_handoff_enabled defaults to True

    router.handle_inbound(db_session, "wa-handoff-enabled-1", "JOIN")

    to, text = mock.sent[-1]
    assert "token=" in text


def test_get_conversation_router_reads_wallet_handoff_enabled_independently_of_provider(monkeypatch):
    """Revision 2026-09-25: WALLET_HANDOFF_ENABLED es su propia variable --
    ya NO se deriva de WHATSAPP_PROVIDER. Antes de este cambio, cambiar el
    proveedor de mensajeria a "meta" apagaba el handoff como efecto
    colateral (y "sandbox" lo prendia como efecto colateral); ahora ninguna
    de las dos cosas depende de la otra."""
    from backend import messaging
    from backend.config import settings

    # WHATSAPP_PROVIDER=sandbox + WALLET_HANDOFF_ENABLED=False (default) -> deshabilitado
    monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "sandbox")
    monkeypatch.setattr(settings, "WALLET_HANDOFF_ENABLED", False)
    messaging.reset_conversation_router_for_tests()
    router = messaging.get_conversation_router()
    assert router._wallet_handoff_enabled is False
    messaging.reset_conversation_router_for_tests()

    # WHATSAPP_PROVIDER=meta + WALLET_HANDOFF_ENABLED=False (default) -> sigue deshabilitado
    monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "meta")
    monkeypatch.setattr(settings, "WHATSAPP_ACCESS_TOKEN", "synthetic-token")
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "synthetic-phone-id")
    router = messaging.get_conversation_router()
    assert router._wallet_handoff_enabled is False
    messaging.reset_conversation_router_for_tests()

    # WHATSAPP_PROVIDER=sandbox + WALLET_HANDOFF_ENABLED=True -- el handoff
    # SI se activa, y sigue siendo True incluso si el provider cambia despues,
    # porque ya no hay ningun acoplamiento entre las dos variables.
    monkeypatch.setattr(settings, "WHATSAPP_PROVIDER", "sandbox")
    monkeypatch.setattr(settings, "WALLET_HANDOFF_ENABLED", True)
    messaging.reset_conversation_router_for_tests()
    router = messaging.get_conversation_router()
    assert router._wallet_handoff_enabled is True
    messaging.reset_conversation_router_for_tests()


def test_get_conversation_router_uses_configured_dapp_base_url(monkeypatch):
    from backend import messaging
    from backend.config import settings

    monkeypatch.setattr(settings, "DAPP_BASE_URL", "http://localhost:5173/app")
    messaging.reset_conversation_router_for_tests()
    router = messaging.get_conversation_router()
    assert router._dapp_base_url == "http://localhost:5173/app"
    messaging.reset_conversation_router_for_tests()
