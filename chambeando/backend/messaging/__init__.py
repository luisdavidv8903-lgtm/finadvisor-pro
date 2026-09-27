"""
Single entry point for the process-wide ConversationRouter -- same singleton
pattern as backend.chain.get_chain_adapter(), for the same reason (business
code depends on the accessor, never constructs a WhatsAppAdapter/
SandboxMetaClient itself; tests inject a fake via set_conversation_router_for_tests).
"""
from .adapter import MessagingAdapter, OutboundMessage
from .router import ConversationRouter
from .telegram_adapter import RealTelegramClient, SandboxTelegramClient, TelegramAdapter
from .telegram_router import TelegramConversationRouter
from .telegram_vip_router import TelegramVipRouter
from .whatsapp_adapter import MetaCloudWhatsAppClient, SandboxMetaClient, WhatsAppAdapter
from .whatsapp_vip_router import WhatsAppVipRouter

__all__ = [
    "MessagingAdapter",
    "OutboundMessage",
    "ConversationRouter",
    "WhatsAppAdapter",
    "SandboxMetaClient",
    "MetaCloudWhatsAppClient",
    "get_conversation_router",
    "set_conversation_router_for_tests",
    "reset_conversation_router_for_tests",
    "TelegramConversationRouter",
    "TelegramAdapter",
    "SandboxTelegramClient",
    "RealTelegramClient",
    "get_telegram_router",
    "set_telegram_router_for_tests",
    "reset_telegram_router_for_tests",
    "WhatsAppVipRouter",
    "get_whatsapp_vip_router",
    "set_whatsapp_vip_router_for_tests",
    "reset_whatsapp_vip_router_for_tests",
    "TelegramVipRouter",
    "get_telegram_vip_router",
    "set_telegram_vip_router_for_tests",
    "reset_telegram_vip_router_for_tests",
]

_router: ConversationRouter | None = None
_telegram_router: TelegramConversationRouter | None = None
_whatsapp_vip_router: WhatsAppVipRouter | None = None
_telegram_vip_router: TelegramVipRouter | None = None


def _build_default_client():
    """Config-driven provider selection (Phase 2D.1 section 2) -- reads
    `settings.WHATSAPP_PROVIDER` explicitly; NEVER inferred from "are
    WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID set", so exporting those
    env vars for local testing can never silently flip a session over to
    sending real messages."""
    from ..config import settings

    if settings.WHATSAPP_PROVIDER == "meta":
        return MetaCloudWhatsAppClient(
            access_token=settings.WHATSAPP_ACCESS_TOKEN or "",
            phone_number_id=settings.WHATSAPP_PHONE_NUMBER_ID or "",
            graph_api_version=settings.WHATSAPP_GRAPH_API_VERSION,
        )
    return SandboxMetaClient()


def get_conversation_router() -> ConversationRouter:
    global _router
    if _router is None:
        from ..config import settings

        # Revision 2026-09-25: WALLET_HANDOFF_ENABLED es su propia variable,
        # separada de WHATSAPP_PROVIDER -- activar el transporte real de Meta
        # (mensajeria) nunca activa por si solo el handoff de firma de
        # wallet. Default False (ver config.py) hasta que el dApp HTTPS real
        # exista y este probado; DAPP_BASE_URL nunca aparenta ser un dominio
        # real por defecto.
        _router = ConversationRouter(
            WhatsAppAdapter(_build_default_client()),
            dapp_base_url=settings.DAPP_BASE_URL,
            wallet_handoff_enabled=settings.WALLET_HANDOFF_ENABLED,
        )
    return _router


def set_conversation_router_for_tests(router: ConversationRouter) -> None:
    global _router
    _router = router


def reset_conversation_router_for_tests() -> None:
    global _router
    _router = None


def _build_default_telegram_client():
    """Same config-driven-never-inferred rule as _build_default_client
    above, applied to Telegram's own settings.TELEGRAM_PROVIDER."""
    from ..config import settings

    if settings.TELEGRAM_PROVIDER == "telegram":
        return RealTelegramClient(bot_token=settings.TELEGRAM_BOT_TOKEN or "")
    return SandboxTelegramClient()


def get_telegram_router() -> TelegramConversationRouter:
    global _telegram_router
    if _telegram_router is None:
        _telegram_router = TelegramConversationRouter(TelegramAdapter(_build_default_telegram_client()))
    return _telegram_router


def set_telegram_router_for_tests(router: TelegramConversationRouter) -> None:
    global _telegram_router
    _telegram_router = router


def reset_telegram_router_for_tests() -> None:
    global _telegram_router
    _telegram_router = None


def get_whatsapp_vip_router() -> WhatsAppVipRouter:
    """Deliberately built from the SAME underlying MetaClient as
    get_conversation_router() (_build_default_client) -- one real transport,
    two independent routers/domains on top of it. Constructed with the raw
    MetaClient (not a WhatsAppAdapter) because the VIP outbox needs
    send_message_with_id, which is a MetaClient-level capability (see
    services/whatsapp_outbox.py)."""
    global _whatsapp_vip_router
    if _whatsapp_vip_router is None:
        _whatsapp_vip_router = WhatsAppVipRouter(_build_default_client())
    return _whatsapp_vip_router


def set_whatsapp_vip_router_for_tests(router: WhatsAppVipRouter) -> None:
    global _whatsapp_vip_router
    _whatsapp_vip_router = router


def reset_whatsapp_vip_router_for_tests() -> None:
    global _whatsapp_vip_router
    _whatsapp_vip_router = None


def get_telegram_vip_router() -> TelegramVipRouter:
    """Deliberately built from the SAME underlying TelegramClient as
    get_telegram_router() (_build_default_telegram_client) -- one real
    transport, two independent routers/domains on top of it (open
    marketplace vs. invite-only VIP pilot), exactly mirroring the WhatsApp
    side's get_conversation_router()/get_whatsapp_vip_router() split.
    Constructed with the raw TelegramClient (not a TelegramAdapter),
    matching WhatsAppVipRouter's own shape -- see
    telegram_vip_router.py's module docstring (2026-09-27 revision) for why:
    the outbox needs a client whose send_message() actually raises
    TelegramApiError, which TelegramAdapter.send() swallows."""
    global _telegram_vip_router
    if _telegram_vip_router is None:
        _telegram_vip_router = TelegramVipRouter(_build_default_telegram_client())
    return _telegram_vip_router


def set_telegram_vip_router_for_tests(router: TelegramVipRouter) -> None:
    global _telegram_vip_router
    _telegram_vip_router = router


def reset_telegram_vip_router_for_tests() -> None:
    global _telegram_vip_router
    _telegram_vip_router = None
