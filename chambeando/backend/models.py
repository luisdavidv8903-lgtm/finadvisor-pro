"""
Modelo de datos v2.1 (Phase 2B). Separa explicitamente tres capas (ver
DATA_VISIBILITY_MATRIX.md para el detalle campo-por-campo):

  A) metadata de marketplace off-chain   -> P2POrderDB (campos no-financieros)
  B) estado de escrow on-chain            -> P2POrderDB.onchain_status y afines,
                                              escritos EXCLUSIVAMENTE por indexer.py
  C) datos privados de liquidacion (fiat) -> SettlementDetailDB, encriptado en reposo

Ademas: membresia/invites (acceso NO publico), roles backend (MEMBER/MODERATOR/
ADMIN, ortogonales al `arbiter` on-chain), y un log de auditoria append-only que
NUNCA contiene el valor sensible en si, solo metadata de acceso.
"""

from enum import Enum

from . import timeutils
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy import Enum as SQLEnum

from .database import Base


class MemberRole(str, Enum):
    MEMBER = "member"
    MODERATOR = "moderator"
    ADMIN = "admin"


class MembershipStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class OrderStatus(str, Enum):
    """Espejo del enum on-chain de EscrowP2P.sol — el indexer es la UNICA fuente que
    escribe P2POrderDB.onchain_status; ningun endpoint de la API lo modifica jamas."""

    OPEN = "open"
    CLAIMED = "claimed"
    PAID = "paid"
    DISPUTED = "disputed"
    RELEASED = "released"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class ConversationState(str, Enum):
    """Server-side WhatsApp conversation state (Phase 2C section 7: WhatsApp
    message history is never authoritative product state -- this table is).
    Deliberately small and linear: this is a sandbox interaction layer, not a
    second business state machine (order/dispute state stays in P2POrderDB,
    only referenced here by id)."""

    IDLE = "idle"
    AWAITING_LINK = "awaiting_link"
    AWAITING_INVITE_CODE = "awaiting_invite_code"
    MENU = "menu"
    BUY_SELECT_OFFER = "buy_select_offer"
    SELL_AWAITING_AMOUNT = "sell_awaiting_amount"
    SELL_AWAITING_RATE = "sell_awaiting_rate"
    SELL_AWAITING_CONFIRM = "sell_awaiting_confirm"
    DISPUTE_SELECT_TRADE = "dispute_select_trade"


class SecurityEventType(str, Enum):
    AUTH_FAILURE = "auth_failure"
    AUTH_SUCCESS = "auth_success"
    INVITE_CREATED = "invite_created"
    INVITE_REDEEMED = "invite_redeemed"
    INVITE_REDEMPTION_DENIED = "invite_redemption_denied"
    INVITE_REVOKED = "invite_revoked"
    MEMBERSHIP_SUSPENDED = "membership_suspended"
    MEMBERSHIP_REACTIVATED = "membership_reactivated"
    ROLE_CHANGED = "role_changed"
    SETTLEMENT_DETAIL_VIEWED = "settlement_detail_viewed"
    SETTLEMENT_DETAIL_CREATED = "settlement_detail_created"
    SETTLEMENT_DETAIL_REVOKED = "settlement_detail_revoked"
    DISPUTE_EVIDENCE_VIEWED = "dispute_evidence_viewed"
    DISPUTE_EVIDENCE_SUBMITTED = "dispute_evidence_submitted"
    DISPUTE_ASSIGNMENT_CREATED = "dispute_assignment_created"
    DISPUTE_ASSIGNMENT_REVOKED = "dispute_assignment_revoked"
    REPORT_FILED = "report_filed"
    REPORT_REVIEWED = "report_reviewed"
    RATE_LIMIT_TRIGGERED = "rate_limit_triggered"
    ADMIN_BOOTSTRAPPED = "admin_bootstrapped"
    WHATSAPP_ACCOUNT_LINKED = "whatsapp_account_linked"


class UserDB(Base):
    """Identidad minima de una wallet que demostro poseer su clave privada (ver auth.py).
    Que exista un UserDB NO implica acceso al marketplace — eso requiere ademas un
    MembershipDB con status=ACTIVE. Registrarse (probar firma) y ser miembro son cosas
    distintas a proposito: ver seccion 1 del informe de Phase 2B."""

    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    wallet_address = Column(String, unique=True, index=True, nullable=False)  # identidad real
    alias = Column(String, unique=True, nullable=True)  # nombre mostrado en el marketplace — nunca la wallet completa
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class AuthNonceDB(Base):
    """Nonce de un solo uso para el login por firma de wallet (challenge-response)."""

    __tablename__ = "auth_nonces"
    id = Column(Integer, primary_key=True, index=True)
    wallet_address = Column(String, index=True, nullable=False)
    nonce = Column(String, nullable=False, unique=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Integer, default=0, nullable=False)  # 0/1 — invalidado atomicamente, ver auth.py
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class InviteDB(Base):
    """El codigo de invitacion en texto plano NUNCA se persiste — solo su hash. Se
    devuelve en texto plano UNA sola vez, en la respuesta de creacion."""

    __tablename__ = "invites"
    id = Column(Integer, primary_key=True, index=True)
    code_hash = Column(String, unique=True, index=True, nullable=False)  # sha256(code), nunca el code
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    max_uses = Column(Integer, nullable=False, default=1)
    used_count = Column(Integer, nullable=False, default=0)  # incrementado atomicamente, ver invites router
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class MembershipDB(Base):
    """La UNICA forma de obtener esto es redimiendo un invite valido (ver invites
    router) — jamas se crea automaticamente al autenticar una wallet."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", name="uq_membership_user"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    invited_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    invite_id = Column(Integer, ForeignKey("invites.id"), nullable=True)
    role = Column(SQLEnum(MemberRole, create_constraint=True, validate_strings=True), nullable=False, default=MemberRole.MEMBER)
    status = Column(SQLEnum(MembershipStatus, create_constraint=True, validate_strings=True), nullable=False, default=MembershipStatus.ACTIVE)
    joined_at = Column(DateTime(timezone=True), default=timeutils.utcnow)
    suspended_at = Column(DateTime(timezone=True), nullable=True)
    suspended_reason = Column(String, nullable=True)


class SettlementDetailDB(Base):
    """Instrucciones de liquidacion fiat (p.ej. datos de una cuenta para transferencia
    CUP) de un usuario. `encrypted_payload` es el UNICO lugar donde vive el dato
    sensible, y siempre cifrado (ver security/crypto.py) — nunca en texto plano en
    ninguna columna, log, evento on-chain ni URL. Un usuario puede tener varios
    registros (distintos metodos/monedas); cada uno se activa/revoca independientemente."""

    __tablename__ = "settlement_details"
    id = Column(Integer, primary_key=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    payment_method = Column(String, nullable=False)  # catalogo generico, p.ej. "cup_transfer"
    currency = Column(String, nullable=True)
    encrypted_payload = Column(LargeBinary, nullable=False)  # ciphertext (Fernet) — ver security/crypto.py
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class P2POrderDB(Base):
    """(A) metadata de marketplace + (B) espejo de solo-lectura del estado on-chain.
    Los campos de (B) (onchain_status, arbiter_snapshot, was_disputed, confirmed_block)
    los escribe EXCLUSIVAMENTE indexer.py — ningun endpoint de la API los toca a partir
    de un request de usuario. (C) — los datos de liquidacion — viven aparte, en
    SettlementDetailDB, solo referenciados aqui por id (nunca embebidos)."""

    __tablename__ = "p2p_orders"
    __table_args__ = (UniqueConstraint("onchain_order_id", name="uq_order_onchain_id"),)

    id = Column(Integer, primary_key=True, index=True)

    # --- (B) espejo on-chain — solo indexer.py escribe estos campos ---
    onchain_order_id = Column(Integer, index=True, nullable=False)
    escrow_tx_hash = Column(String, index=True, nullable=True)
    token_address = Column(String, nullable=False)
    seller_wallet = Column(String, nullable=False, index=True)
    buyer_wallet = Column(String, nullable=True, index=True)
    arbiter_snapshot_wallet = Column(String, nullable=True, index=True)  # espejo de Order.arbiterSnapshot
    crypto_amount = Column(Numeric(18, 8), nullable=False)
    onchain_status = Column(SQLEnum(OrderStatus, create_constraint=True, validate_strings=True), nullable=False, default=OrderStatus.OPEN)
    was_disputed = Column(Boolean, nullable=False, default=False)  # una vez True, nunca vuelve a False
    confirmed_block = Column(Integer, nullable=True)

    # --- (A) metadata de marketplace — la adjunta el vendedor via API, post-indexado ---
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    fiat_amount = Column(Numeric(12, 2), nullable=True)
    fiat_currency = Column(String, nullable=True)
    payment_method = Column(String, nullable=True)  # generico — "Transferencia CUP", nunca datos de cuenta
    quoted_rate = Column(Numeric(18, 8), nullable=True)  # fiat_amount / crypto_amount, guardado explicito

    # --- (C) referencia + snapshot — ver seccion 5 del informe de Phase 2B.2 ---
    # `settlement_detail_id` apunta al registro "maestro" que origino los datos
    # (solo para trazabilidad/auditoria) — pero lo que efectivamente se revela
    # a la contraparte/arbitro es SIEMPRE `settlement_snapshot_*`, una copia
    # congelada tomada en el MISMO momento (misma transaccion) que se adjunta
    # la metadata, en attach_order_metadata(). Esto garantiza que revocar o
    # reemplazar el registro maestro DESPUES nunca altera silenciosamente lo
    # que la contraparte de ESTE trade especifico ve — ver
    # SETTLEMENT_LIFECYCLE.md.
    settlement_detail_id = Column(Integer, ForeignKey("settlement_details.id"), nullable=True)
    settlement_snapshot_payload = Column(LargeBinary, nullable=True)  # copia del ciphertext, tomada una sola vez
    settlement_snapshot_payment_method = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class DisputeEvidenceDB(Base):
    """Metadata de evidencia — nunca el archivo en si. `storage_ref` es una referencia
    opaca hacia el backend de almacenamiento (ver services/evidence_storage.py); en
    esta fase es un backend de test local, nunca un proveedor cloud real."""

    __tablename__ = "dispute_evidence"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("p2p_orders.id"), nullable=False, index=True)
    submitted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    evidence_type = Column(String, nullable=False, default="note")  # "note" | "file"
    storage_ref = Column(String, nullable=True)  # opaco — ver EvidenceStorage
    content_hash = Column(String, nullable=True)
    note = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class DisputeAssignmentDB(Base):
    """Unica via por la que un usuario de staff (MODERATOR/ADMIN) puede leer
    evidencia de una disputa de la que NO es parte ni arbitro on-chain. Tener el
    rol MODERATOR o ADMIN por si solo NO otorga acceso — ver
    services/authorization.can_view_dispute_evidence (Phase 2B.1, corrige el
    acceso amplio de Phase 2B). Solo ADMIN puede crear/revocar asignaciones
    (ver routers/admin.py); cada creacion y revocacion se audita."""

    __tablename__ = "dispute_assignments"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("p2p_orders.id"), nullable=False, index=True)
    assigned_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    assigned_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reason = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class ArbiterResolutionDB(Base):
    """Registro de auditoria: por que el arbitro on-chain resolvio una disputa de cierta forma."""

    __tablename__ = "arbiter_resolutions"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("p2p_orders.id"), nullable=False, index=True)
    resolved_by_wallet = Column(String, nullable=False)
    recipient_wallet = Column(String, nullable=False)
    reasoning = Column(String, nullable=False)
    resolve_tx_hash = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class IndexerCheckpointDB(Base):
    __tablename__ = "indexer_checkpoints"
    id = Column(Integer, primary_key=True, index=True)
    contract_address = Column(String, unique=True, nullable=False)
    last_processed_block = Column(Integer, nullable=False, default=0)


class ReportDB(Base):
    """Reporte de abuso/usuario marcado, insumo minimo para que MODERATOR pueda
    'review flagged users / abuse reports' (seccion 11). Sin maquina de estados
    compleja a proposito — YAGNI hasta que el volumen real lo justifique."""

    __tablename__ = "reports"
    id = Column(Integer, primary_key=True, index=True)
    reporter_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reported_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    order_id = Column(Integer, ForeignKey("p2p_orders.id"), nullable=True)
    reason = Column(String, nullable=False)
    status = Column(String, nullable=False, default="open")  # "open" | "reviewed" | "dismissed"
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)
    reviewed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)


class SecurityEventDB(Base):
    """Log de auditoria APPLICATION-APPEND-ONLY (no DB-enforced todavia — ver
    ENFORCEMENT_LEVELS.md). Ningun servicio ni endpoint de este backend expone
    una via de update/delete para esta tabla (verificado por
    tests/test_audit_log_enforcement.py), pero eso es una garantia de la
    aplicacion, no de la base de datos: cualquier codigo con la misma conexion/
    credencial de DB que usa el backend (p.ej. un script administrativo corrido
    a mano, o un bug futuro) SI podria hacer UPDATE/DELETE directo sobre esta
    tabla — nada en el motor de la base de datos en si lo impide en esta fase.
    Endurecimiento de produccion (no implementado, ver ENFORCEMENT_LEVELS.md):
    un rol de DB separado sin permiso UPDATE/DELETE sobre esta tabla, un trigger
    que rechace UPDATE/DELETE, o un log externo inmutable (WORM/SIEM).
    NUNCA contiene el valor sensible: solo actor, accion, objeto afectado, timestamp
    y una razon opcional en texto libre (que a su vez nunca debe contener el dato
    sensible — responsabilidad de cada caller, ver tests de logging)."""

    __tablename__ = "security_events"
    id = Column(Integer, primary_key=True, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # null = intento pre-auth (p.ej. login fallido)
    action = Column(SQLEnum(SecurityEventType, create_constraint=True, validate_strings=True), nullable=False)
    target_type = Column(String, nullable=True)  # "order" | "settlement_detail" | "membership" | ...
    target_id = Column(String, nullable=True)
    reason = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class WhatsAppLinkDB(Base):
    """Maps an opaque WhatsApp identity (WA's own user id -- never the raw
    phone number is required to be stored, and never used as a trust
    signal) to a Chambeando UserDB -- but ONLY after that user proved wallet
    ownership through the normal nonce/signature flow (auth.py), same as any
    other login. Phase 2C section 5's rule, enforced by construction: a
    WhatsAppLinkDB row existing (whatsapp_id known) never implies user_id is
    set, and user_id being set never implies membership -- that is still a
    separate MembershipDB row, redeemed the normal way. Three independent
    facts, three independent columns/tables, exactly as documented in
    WHATSAPP_ARCHITECTURE.md.

    link_token_hash/link_token_expires_at hold a short-lived, single-use,
    hashed (never plaintext) token for the deep-link handoff to the wallet-
    signing page -- same hash-not-plaintext discipline as InviteDB.code_hash."""

    __tablename__ = "whatsapp_links"
    id = Column(Integer, primary_key=True, index=True)
    whatsapp_id = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    link_token_hash = Column(String, nullable=True, index=True)
    link_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    linked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class ConversationSessionDB(Base):
    """Server-side conversation state per WhatsApp identity (see
    ConversationState). `context` is a small opaque JSON string (e.g. which
    synthetic offer numbers map to which real order ids for THIS session) --
    never raw settlement data, never a private key, never full wallets."""

    __tablename__ = "conversation_sessions"
    id = Column(Integer, primary_key=True, index=True)
    whatsapp_id = Column(String, unique=True, index=True, nullable=False)
    state = Column(SQLEnum(ConversationState, create_constraint=True, validate_strings=True), nullable=False, default=ConversationState.IDLE)
    context = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=timeutils.utcnow, onupdate=timeutils.utcnow)


class ProcessedWebhookEventDB(Base):
    """DB-ENFORCED idempotency for inbound webhook delivery (Phase 2C section
    6/7: duplicate webhook delivery must be a no-op). The unique constraint
    is the actual guarantee -- a second INSERT for the same
    (provider, message_id) raises IntegrityError, which the webhook handler
    catches and treats as 'already processed', never a second time
    processing the same inbound event (same DB-enforced-uniqueness pattern
    as InviteDB.code_hash / UserDB.wallet_address elsewhere in this schema)."""

    __tablename__ = "processed_webhook_events"
    __table_args__ = (UniqueConstraint("provider", "message_id", name="uq_webhook_event_provider_message"),)

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, nullable=False, default="whatsapp")
    message_id = Column(String, nullable=False)
    processed_at = Column(DateTime(timezone=True), default=timeutils.utcnow)
