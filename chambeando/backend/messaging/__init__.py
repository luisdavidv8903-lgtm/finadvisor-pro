"""
Single entry point for the process-wide ConversationRouter -- same singleton
pattern as backend.chain.get_chain_adapter(), for the same reason (business
code depends on the accessor, never constructs a WhatsAppAdapter/
SandboxMetaClient itself; tests inject a fake via set_conversation_router_for_tests).
"""
from .adapter import MessagingAdapter, OutboundMessage
from .router import ConversationRouter
from .whatsapp_adapter import SandboxMetaClient, WhatsAppAdapter

__all__ = [
    "MessagingAdapter",
    "OutboundMessage",
    "ConversationRouter",
    "WhatsAppAdapter",
    "SandboxMetaClient",
    "get_conversation_router",
    "set_conversation_router_for_tests",
    "reset_conversation_router_for_tests",
]

_router: ConversationRouter | None = None


def get_conversation_router() -> ConversationRouter:
    global _router
    if _router is None:
        # Sandbox-only default: a WhatsAppAdapter backed by SandboxMetaClient,
        # which never makes a network call (see whatsapp_adapter.py). A real
        # deployment wires a genuine MetaClient before ever taking real
        # traffic -- that wiring lives outside this phase's scope.
        _router = ConversationRouter(WhatsAppAdapter())
    return _router


def set_conversation_router_for_tests(router: ConversationRouter) -> None:
    global _router
    _router = router


def reset_conversation_router_for_tests() -> None:
    global _router
    _router = None
