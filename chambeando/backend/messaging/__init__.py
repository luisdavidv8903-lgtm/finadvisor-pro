"""
Single entry point for the process-wide ConversationRouter -- same singleton
pattern as backend.chain.get_chain_adapter(), for the same reason (business
code depends on the accessor, never constructs a WhatsAppAdapter/
SandboxMetaClient itself; tests inject a fake via set_conversation_router_for_tests).
"""
from .adapter import MessagingAdapter, OutboundMessage
from .router import ConversationRouter
from .whatsapp_adapter import MetaCloudWhatsAppClient, SandboxMetaClient, WhatsAppAdapter

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
]

_router: ConversationRouter | None = None


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

        # Phase 2D.1 section 11: no genuine HTTPS Chambeando test endpoint
        # exists yet -- keep the real wallet-signing handoff disabled
        # whenever the real Meta transport is selected, regardless of
        # whether credentials are configured.
        wallet_handoff_enabled = settings.WHATSAPP_PROVIDER != "meta"
        _router = ConversationRouter(WhatsAppAdapter(_build_default_client()), wallet_handoff_enabled=wallet_handoff_enabled)
    return _router


def set_conversation_router_for_tests(router: ConversationRouter) -> None:
    global _router
    _router = router


def reset_conversation_router_for_tests() -> None:
    global _router
    _router = None
