"""
Schemas Pydantic explicitos. REGLA DURA: ningun endpoint devuelve un objeto ORM
crudo — todo pasa por uno de estos `response_model`, cada uno construido a
proposito para UNA audiencia (ver DATA_VISIBILITY_MATRIX.md). Un campo nuevo en
un modelo de SQLAlchemy NUNCA se filtra a una respuesta a menos que se agregue
aqui explicitamente — whitelist, no blacklist.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, condecimal

from .models import MemberRole, MembershipStatus, OrderStatus

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class NonceRequest(BaseModel):
    wallet_address: str = Field(min_length=25, max_length=64)


class NonceResponse(BaseModel):
    nonce: str
    message: str


class VerifyRequest(BaseModel):
    wallet_address: str = Field(min_length=25, max_length=64)
    signature: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    is_member: bool  # el cliente sabe de una si debe mostrar "unirse con invite" o el marketplace


class MeOut(BaseModel):
    """Respuesta de GET /me — whitelist explicita, nunca el dict crudo de antes.
    Nota: `wallet_address` completa es intencional aqui — es la propia wallet del
    caller, no la de un tercero (la regla de enmascarado es sobre wallets AJENAS,
    ver services/authorization.mask_wallet)."""

    wallet_address: str
    alias: str | None
    is_member: bool
    role: MemberRole | None


# ---------------------------------------------------------------------------
# Invites / membership
# ---------------------------------------------------------------------------


class InviteCreateRequest(BaseModel):
    max_uses: int = Field(default=1, ge=1, le=50)
    expires_in_hours: int = Field(default=72, ge=1, le=24 * 30)


class InviteCreateResponse(BaseModel):
    """`code` en texto plano se devuelve UNA sola vez — el backend nunca lo vuelve
    a mostrar despues de este response (solo se persiste su hash)."""

    id: int
    code: str
    max_uses: int
    expires_at: datetime


class InviteRedeemRequest(BaseModel):
    code: str = Field(min_length=8, max_length=128)


class MembershipSelfOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: MemberRole
    status: MembershipStatus
    joined_at: datetime


class MembershipAdminOut(BaseModel):
    """Vista ADMIN de una membresia — incluye el user_id pero NUNCA la wallet
    completa (usar el endpoint de usuario si hace falta, que tambien la enmascara
    salvo excepciones explicitas)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    role: MemberRole
    status: MembershipStatus
    joined_at: datetime
    suspended_at: datetime | None
    suspended_reason: str | None


class SuspendMembershipRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class RoleAssignRequest(BaseModel):
    role: MemberRole


# ---------------------------------------------------------------------------
# Ordenes / marketplace — audiencia MEMBER (ver DATA_VISIBILITY_MATRIX.md)
# ---------------------------------------------------------------------------


class OrderMemberOut(BaseModel):
    """Lo que cualquier MEMBER activo puede ver de una orden. Nunca wallet
    completa, nunca datos de liquidacion, nunca identidad legal."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    onchain_status: OrderStatus
    seller_wallet_masked: str
    buyer_wallet_masked: str | None
    crypto_amount: Decimal
    fiat_amount: Decimal | None
    fiat_currency: str | None
    payment_method: str | None  # generico — nunca datos de cuenta
    quoted_rate: Decimal | None
    created_at: datetime


class OrderMetadataCreate(BaseModel):
    onchain_order_id: int
    fiat_amount: condecimal(gt=Decimal("0"), decimal_places=2)
    fiat_currency: str = Field(min_length=3, max_length=8)
    payment_method: str = Field(min_length=2, max_length=64)
    settlement_detail_id: int | None = None  # debe pertenecer al caller — verificado en el router


# ---------------------------------------------------------------------------
# Settlement details (cifrados en reposo)
# ---------------------------------------------------------------------------


class SettlementDetailCreateRequest(BaseModel):
    payment_method: str = Field(min_length=2, max_length=64)
    currency: str | None = Field(default=None, max_length=8)
    payload: dict[str, Any]  # p.ej. {"account_holder": "...", "account_number": "...", "bank": "..."}


class SettlementDetailOut(BaseModel):
    """Metadata NO sensible de un settlement detail — jamas incluye el payload."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    payment_method: str
    currency: str | None
    active: bool
    created_at: datetime
    revoked_at: datetime | None


class SettlementDetailRevealed(BaseModel):
    """Solo la devuelve el endpoint de reveal, tras autorizacion + auditoria."""

    id: int
    payment_method: str
    currency: str | None
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Dispute evidence
# ---------------------------------------------------------------------------


class DisputeEvidenceCreate(BaseModel):
    order_id: int
    evidence_type: str = Field(pattern="^(note|file)$", default="note")
    note: str | None = Field(default=None, max_length=2000)
    content_base64: str | None = None
    content_type: str | None = Field(default=None, max_length=100)


class DisputeEvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    evidence_type: str
    content_hash: str | None
    note: str | None
    created_at: datetime


class DisputeEvidenceContent(BaseModel):
    content_type: str | None
    content_base64: str


# ---------------------------------------------------------------------------
# Dispute assignments (staff-only evidence review — explicit, scoped, audited)
# ---------------------------------------------------------------------------


class DisputeAssignmentCreateRequest(BaseModel):
    assigned_user_id: int
    reason: str = Field(min_length=3, max_length=500)
    expires_in_hours: int = Field(default=72, ge=1, le=24 * 30)


class DisputeAssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    assigned_user_id: int
    assigned_by_user_id: int
    reason: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


# ---------------------------------------------------------------------------
# Reputation
# ---------------------------------------------------------------------------


class ReputationOut(BaseModel):
    completed_trades: int
    cancelled_trades: int
    disputes_opened: int
    disputes_won: int
    disputes_lost: int
    account_age_days: int
    successful_volume: str


# ---------------------------------------------------------------------------
# Reports / moderation
# ---------------------------------------------------------------------------


class ReportCreateRequest(BaseModel):
    reported_user_id: int | None = None
    order_id: int | None = None
    reason: str = Field(min_length=3, max_length=1000)


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reported_user_id: int | None
    order_id: int | None
    reason: str
    status: str
    created_at: datetime


class ReportReviewRequest(BaseModel):
    status: str = Field(pattern="^(reviewed|dismissed)$")


# ---------------------------------------------------------------------------
# Security / audit events (ADMIN)
# ---------------------------------------------------------------------------


class SecurityEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_user_id: int | None
    action: str
    target_type: str | None
    target_id: str | None
    reason: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# WhatsApp sandbox (Phase 2C) — see WHATSAPP_ARCHITECTURE.md. The inbound
# webhook envelope below is a deliberately SIMPLIFIED synthetic shape, not
# Meta's real deeply-nested Cloud API payload — parsing the real envelope is
# listed as a remaining blocker before a genuine Meta sandbox integration.
# ---------------------------------------------------------------------------


class WhatsAppInboundMessage(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)
    from_whatsapp_id: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=4096)


class WhatsAppWebhookPayload(BaseModel):
    messages: list[WhatsAppInboundMessage] = Field(default_factory=list)


class WhatsAppLinkRequest(BaseModel):
    link_token: str = Field(min_length=1, max_length=128)


class WhatsAppLinkResponse(BaseModel):
    whatsapp_id: str
    linked: bool
