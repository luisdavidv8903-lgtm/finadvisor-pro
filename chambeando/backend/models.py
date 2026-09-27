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
    TELEGRAM_IDENTITY_REGISTERED = "telegram_identity_registered"
    TELEGRAM_MEMBERSHIP_SUSPENDED = "telegram_membership_suspended"
    TELEGRAM_MEMBERSHIP_REACTIVATED = "telegram_membership_reactivated"
    TELEGRAM_OFFER_CREATED = "telegram_offer_created"
    TELEGRAM_OFFER_MATCHED = "telegram_offer_matched"
    TELEGRAM_OFFER_STATUS_CHANGED = "telegram_offer_status_changed"
    VIP_WHATSAPP_ID_AUTHORIZED = "vip_whatsapp_id_authorized"
    VIP_INVITE_CREATED = "vip_invite_created"
    VIP_INVITE_REDEEMED = "vip_invite_redeemed"
    VIP_MEMBERSHIP_REVOKED = "vip_membership_revoked"
    VIP_OFFER_CREATED = "vip_offer_created"
    VIP_OFFER_CLOSED = "vip_offer_closed"
    VIP_OFFER_INTEREST_REGISTERED = "vip_offer_interest_registered"
    VIP_OUTBOUND_DELIVERY_UNCERTAIN = "vip_outbound_delivery_uncertain"
    VIP_OUTBOUND_DELIVERY_FAILED_TERMINAL = "vip_outbound_delivery_failed_terminal"
    TELEGRAM_VIP_OUTBOUND_DELIVERY_UNCERTAIN = "telegram_vip_outbound_delivery_uncertain"
    TELEGRAM_VIP_OUTBOUND_DELIVERY_FAILED_TERMINAL = "telegram_vip_outbound_delivery_failed_terminal"
    TELEGRAM_VIP_OUTBOUND_DELIVERY_FAILED_PERMANENT = "telegram_vip_outbound_delivery_failed_permanent"


class UserDB(Base):
    """Identidad minima de una wallet que demostro poseer su clave privada (ver auth.py).
    Que exista un UserDB NO implica acceso al marketplace — eso requiere ademas un
    MembershipDB con status=ACTIVE. Registrarse (probar firma) y ser miembro son cosas
    distintas a proposito: ver seccion 1 del informe de Phase 2B.

    `network_id` (revision 2026-09-25, autenticacion multi-red): la identidad
    real de una wallet es el PAR (network_id, wallet_address), nunca
    wallet_address solo -- distintas familias de cadena usan formatos de
    direccion incompatibles (TRON base58 vs EVM hex), pero mezclarlas bajo
    una unica clave global igual seria una decision implicita y fragil ante
    cualquier familia futura que pudiera coincidir en formato. La unicidad
    ahora es el UniqueConstraint compuesto de abajo, no wallet_address por
    si sola -- ver auth.py (create_access_token/get_current_user) para como
    se propaga esto al JWT."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("network_id", "wallet_address", name="uq_user_network_wallet"),)

    id = Column(Integer, primary_key=True, index=True)
    network_id = Column(String, nullable=False, index=True, default="tron")
    wallet_address = Column(String, index=True, nullable=False)  # identidad real, unica junto con network_id
    alias = Column(String, unique=True, nullable=True)  # nombre mostrado en el marketplace — nunca la wallet completa
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class AuthNonceDB(Base):
    """Nonce de un solo uso para el login por firma de wallet (challenge-response).

    `network_id` (revision 2026-09-25): el nonce queda atado a la red para la
    que se emitio -- /auth/verify busca el nonce pendiente filtrando TAMBIEN
    por network_id, nunca solo por wallet_address, para que no sea posible
    pedir un nonce para una red y "canjearlo" bajo otra (ver
    chain/auth_networks.py para la whitelist de redes admitidas para auth)."""

    __tablename__ = "auth_nonces"
    id = Column(Integer, primary_key=True, index=True)
    network_id = Column(String, nullable=False, default="tron")
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
    signing page -- same hash-not-plaintext discipline as InviteDB.code_hash.

    link_token_network_id (multichain refactor): the network_id the token was
    ISSUED for, set once alongside link_token_hash and cleared alongside it on
    consumption. Server-side binding, never trusted from a client-supplied
    value alone -- consume_link_token() rejects a network_id that does not
    match this column, so a query-string `&network=` on the deep link can
    never be swapped by whoever opens the link (see messaging/identity.py)."""

    __tablename__ = "whatsapp_links"
    id = Column(Integer, primary_key=True, index=True)
    whatsapp_id = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    link_token_hash = Column(String, nullable=True, index=True)
    link_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    link_token_network_id = Column(String, nullable=True)
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


class Channel(str, Enum):
    """WHATSAPP added for the VIP WhatsApp offer-board pilot (separate from
    WhatsAppLinkDB, which stays untouched and unrelated -- that table is the
    wallet-linking identity for the escrow/marketplace flow; ChannelIdentityDB
    rows with channel=WHATSAPP are a completely different, wallet-free
    identity used only by whatsapp_vip_router.py)."""

    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"


class OfferSide(str, Enum):
    COMPRO = "compro"
    VENDO = "vendo"


class OfferStatus(str, Enum):
    """Off-chain coordination state for a Telegram offer -- NOT OrderStatus.
    P2POfferDB never implies custody, escrow, or an on-chain order; this enum
    exists so that distinction can never be blurred by reusing OrderStatus."""

    OPEN = "open"
    MATCHED = "matched"
    PAYMENT_PENDING = "payment_pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"


class TelegramConversationState(str, Enum):
    """Telegram-specific conversation states -- deliberately separate from
    ConversationState (WhatsApp's), which is full of wallet/dApp-handoff
    states that do not apply here (no wallet, no escrow in V1)."""

    IDLE = "idle"
    MENU = "menu"
    OFFER_AMOUNT = "offer_amount"
    OFFER_CURRENCY = "offer_currency"
    OFFER_RATE = "offer_rate"
    OFFER_SETTLEMENT_METHOD = "offer_settlement_method"
    OFFER_SETTLEMENT_LOCATION = "offer_settlement_location"
    OFFER_CONFIRM = "offer_confirm"
    MATCH_SELECT = "match_select"
    MY_OFFERS_SELECT = "my_offers_select"
    OFFER_ACTION_SELECT = "offer_action_select"
    CANCEL_CONFIRM = "cancel_confirm"


class ChannelIdentityDB(Base):
    """Channel-agnostic identity anchor. `user_id` is nullable and unused by
    V1 (Telegram membership never requires wallet proof) -- it exists so a
    future unification ("this Telegram identity IS this WhatsApp/wallet
    user") is a single UPDATE here, never a data migration or a second
    identity table. WhatsAppLinkDB is untouched; it keeps mapping WhatsApp
    ids on its own for as long as the WhatsApp integration exists."""

    __tablename__ = "channel_identities"
    __table_args__ = (UniqueConstraint("channel", "channel_user_id", name="uq_channel_identity"),)

    id = Column(Integer, primary_key=True, index=True)
    channel = Column(SQLEnum(Channel, create_constraint=True, validate_strings=True), nullable=False)
    channel_user_id = Column(String, nullable=False, index=True)  # opaque per-channel id (Telegram numeric user id, as string)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class TelegramMembershipDB(Base):
    """Same role/status shape as MembershipDB (MemberRole/MembershipStatus
    reused as-is), but keyed off ChannelIdentityDB instead of a wallet-proven
    UserDB -- access to the private Telegram group/bot never requires wallet
    proof in V1. Moderation V1 is direct suspend/reactivate here; there is no
    report/appeal/queue model (explicitly out of scope)."""

    __tablename__ = "telegram_memberships"
    __table_args__ = (UniqueConstraint("channel_identity_id", name="uq_telegram_membership_identity"),)

    id = Column(Integer, primary_key=True, index=True)
    channel_identity_id = Column(Integer, ForeignKey("channel_identities.id"), nullable=False, index=True)
    role = Column(SQLEnum(MemberRole, create_constraint=True, validate_strings=True), nullable=False, default=MemberRole.MEMBER)
    status = Column(SQLEnum(MembershipStatus, create_constraint=True, validate_strings=True), nullable=False, default=MembershipStatus.ACTIVE)
    joined_at = Column(DateTime(timezone=True), default=timeutils.utcnow)
    suspended_by_channel_identity_id = Column(Integer, ForeignKey("channel_identities.id"), nullable=True)
    suspended_at = Column(DateTime(timezone=True), nullable=True)
    suspension_reason = Column(String, nullable=True)


class P2POfferDB(Base):
    """Off-chain COMPRO/VENDO coordination offer. Never custody, never an
    escrow reference, never a wallet -- `settlement_method` (e.g. "cash",
    "zelle") and `settlement_location` (e.g. "Havana") are both plain
    descriptive text, never an account/payment credential (that belongs, for
    the on-chain flow only, in SettlementDetailDB's encrypted payload -- this
    table must never grow a similar field)."""

    __tablename__ = "p2p_offers"

    id = Column(Integer, primary_key=True, index=True)
    channel_identity_id = Column(Integer, ForeignKey("channel_identities.id"), nullable=False, index=True)
    side = Column(SQLEnum(OfferSide, create_constraint=True, validate_strings=True), nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)
    currency = Column(String, nullable=False)
    rate = Column(Numeric(18, 4), nullable=False)
    settlement_method = Column(String, nullable=False)
    settlement_location = Column(String, nullable=False)
    status = Column(SQLEnum(OfferStatus, create_constraint=True, validate_strings=True), nullable=False, default=OfferStatus.OPEN, index=True)
    matched_offer_id = Column(Integer, ForeignKey("p2p_offers.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)
    updated_at = Column(DateTime(timezone=True), default=timeutils.utcnow, onupdate=timeutils.utcnow)


class TelegramSessionDB(Base):
    """Server-side conversation state per Telegram identity -- same role as
    ConversationSessionDB, kept separate because the state enum and domain
    (offers, not wallet-signed orders) are entirely different."""

    __tablename__ = "telegram_sessions"
    id = Column(Integer, primary_key=True, index=True)
    channel_identity_id = Column(Integer, ForeignKey("channel_identities.id"), unique=True, nullable=False, index=True)
    state = Column(SQLEnum(TelegramConversationState, create_constraint=True, validate_strings=True), nullable=False, default=TelegramConversationState.IDLE)
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


# ---------------------------------------------------------------------------
# WhatsApp VIP offer-board pilot -- completely separate from the wallet/
# escrow flow above (WhatsAppLinkDB, ConversationSessionDB, P2POrderDB).
# No wallet, no escrow, no P2POrderDB reference anywhere below. Access is
# NEVER auto-granted by talking to the bot -- see WhatsAppVipMembershipDB's
# docstring and services/whatsapp_vip_identity.py.
# ---------------------------------------------------------------------------


class VipAuthorizationMethod(str, Enum):
    DIRECT = "direct"  # operator authorized this exact WhatsApp id out of band
    INVITE = "invite"  # redeemed a single-use WhatsAppVipInviteDB code


class WhatsAppVipInviteDB(Base):
    """Single-use (by default) invite code for the VIP pilot -- same
    hash-only discipline as InviteDB (the plaintext code is returned once, at
    creation, by vip_admin.py, and never persisted). Deliberately NOT the
    same table as InviteDB: that one is wallet/UserDB-keyed and redeeming it
    goes through services/invites.py's wallet-authenticated flow, which this
    pilot explicitly must not touch."""

    __tablename__ = "whatsapp_vip_invites"

    id = Column(Integer, primary_key=True, index=True)
    code_hash = Column(String, unique=True, index=True, nullable=False)
    max_uses = Column(Integer, nullable=False, default=1)
    used_count = Column(Integer, nullable=False, default=0)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    note = Column(String, nullable=True)  # operator's own label, e.g. "for Maria - group VIP"
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class WhatsAppVipMembershipDB(Base):
    """The ONLY gate for every VIP command (OFERTA/OFERTAS/MIS OFERTAS/ME
    INTERESA/CERRAR). Created EXCLUSIVELY by vip_admin.py (authorize_whatsapp_id,
    a local operator-run script -- never reachable from chat) or by redeeming
    a WhatsAppVipInviteDB code via the VIP <code> chat command. Receiving an
    inbound webhook message from a whatsapp_id proves nothing about group
    membership by itself -- this row is the only thing that does."""

    __tablename__ = "whatsapp_vip_memberships"
    __table_args__ = (UniqueConstraint("channel_identity_id", name="uq_vip_membership_identity"),)

    id = Column(Integer, primary_key=True, index=True)
    channel_identity_id = Column(Integer, ForeignKey("channel_identities.id"), nullable=False, index=True)
    status = Column(SQLEnum(MembershipStatus, create_constraint=True, validate_strings=True), nullable=False, default=MembershipStatus.ACTIVE)
    authorized_via = Column(SQLEnum(VipAuthorizationMethod, create_constraint=True, validate_strings=True), nullable=False)
    invite_id = Column(Integer, ForeignKey("whatsapp_vip_invites.id"), nullable=True)
    authorized_at = Column(DateTime(timezone=True), default=timeutils.utcnow)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoke_reason = Column(String, nullable=True)


class VipOfferIntent(str, Enum):
    COMPRO = "compro"
    VENDO = "vendo"
    CAMBIO = "cambio"


class VipCurrency(str, Enum):
    """Closed catalog observed in the VIP chat evidence (CHAMBEANDO_VIP_OFERTAS_V1.md).
    Deliberately does NOT include 'CLM'/'C' or USDT -- unconfirmed/out-of-scope
    per that document; adding a currency here is a deliberate product decision,
    never a side effect of a user typing something new."""

    USD = "USD"
    CUP = "CUP"
    EUR = "EUR"
    MLC = "MLC"
    MXN = "MXN"


class VipSettlementMethod(str, Enum):
    CASH = "cash"
    TRANSFER = "transfer"
    ZELLE = "zelle"
    MLC_BALANCE = "mlc_balance"
    OTHER = "other"  # requires *_method_detail to be set (short free text)


class VipOfferStatus(str, Enum):
    """BORRADOR/ACTIVA/CERRADA/CADUCADA from the source document, in the
    English-enum-value convention this codebase already uses elsewhere
    (OfferStatus, OrderStatus)."""

    DRAFT = "draft"
    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"


class VipWizardState(str, Enum):
    """Every step of the guided CREAR OFERTA flow -- one question per state,
    nothing inferred, nothing skipped. OFERTAS/MIS OFERTAS/ME INTERESA <id>/
    CERRAR <id> are all single-shot stateless commands (no state needed) --
    only offer CREATION is multi-step."""

    IDLE = "idle"
    WIZ_INTENT = "wiz_intent"
    WIZ_GIVE_CURRENCY = "wiz_give_currency"
    WIZ_RECEIVE_CURRENCY = "wiz_receive_currency"
    WIZ_AMOUNT = "wiz_amount"
    WIZ_RATE_UNIT = "wiz_rate_unit"
    WIZ_RATE_VALUE = "wiz_rate_value"
    WIZ_GIVE_METHOD = "wiz_give_method"
    WIZ_GIVE_METHOD_OTHER = "wiz_give_method_other"
    WIZ_GIVE_LOCATION = "wiz_give_location"
    WIZ_RECEIVE_METHOD = "wiz_receive_method"
    WIZ_RECEIVE_METHOD_OTHER = "wiz_receive_method_other"
    WIZ_RECEIVE_LOCATION = "wiz_receive_location"
    WIZ_DURATION = "wiz_duration"
    WIZ_CONFIRM = "wiz_confirm"


class WhatsAppVipSessionDB(Base):
    """Server-side wizard state per VIP channel identity -- same role as
    TelegramSessionDB/ConversationSessionDB, kept separate because this
    domain (offer-board, no wallet) and its state enum are entirely
    different from both."""

    __tablename__ = "whatsapp_vip_sessions"

    id = Column(Integer, primary_key=True, index=True)
    channel_identity_id = Column(Integer, ForeignKey("channel_identities.id"), unique=True, nullable=False, index=True)
    state = Column(SQLEnum(VipWizardState, create_constraint=True, validate_strings=True), nullable=False, default=VipWizardState.IDLE)
    context = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=timeutils.utcnow, onupdate=timeutils.utcnow)


class WhatsAppVipOfferDB(Base):
    """Off-chain VIP offer-board announcement. NEVER a P2POrderDB, NEVER
    custody, NEVER escrow -- give_/receive_ method+location are plain
    descriptive text/enum, exactly like P2POfferDB.settlement_method (never
    an account/card number -- that would belong in SettlementDetailDB's
    encrypted payload, which this table must never grow a field resembling).

    `source_message_id` is the inbound WhatsApp message id (wamid) whose
    CONFIRMAR triggered this row's creation -- a unique constraint on it is
    what makes offer creation itself idempotent under webhook redelivery
    (see routers/whatsapp.py's transaction-ordering fix and
    services/whatsapp_vip_offers.create_offer)."""

    __tablename__ = "whatsapp_vip_offers"
    __table_args__ = (UniqueConstraint("source_message_id", name="uq_vip_offer_source_message"),)

    id = Column(Integer, primary_key=True, index=True)
    channel_identity_id = Column(Integer, ForeignKey("channel_identities.id"), nullable=False, index=True)

    intent = Column(SQLEnum(VipOfferIntent, create_constraint=True, validate_strings=True), nullable=False)
    give_currency = Column(SQLEnum(VipCurrency, create_constraint=True, validate_strings=True), nullable=False)
    receive_currency = Column(SQLEnum(VipCurrency, create_constraint=True, validate_strings=True), nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)
    min_amount = Column(Numeric(18, 2), nullable=True)

    # Fixed rate only in V1 (percentage explicitly deferred -- see
    # services/whatsapp_vip_offers.py). rate_currency is ALWAYS one of
    # give_currency/receive_currency, confirmed explicitly by the user during
    # the wizard (WIZ_RATE_UNIT) -- never inferred/assumed by the bot.
    rate_value = Column(Numeric(18, 4), nullable=False)
    rate_currency = Column(SQLEnum(VipCurrency, create_constraint=True, validate_strings=True), nullable=False)

    give_method = Column(SQLEnum(VipSettlementMethod, create_constraint=True, validate_strings=True), nullable=False)
    give_method_detail = Column(String, nullable=True)
    give_location = Column(String, nullable=False)
    receive_method = Column(SQLEnum(VipSettlementMethod, create_constraint=True, validate_strings=True), nullable=False)
    receive_method_detail = Column(String, nullable=True)
    receive_location = Column(String, nullable=False)

    duration_hours = Column(Integer, nullable=False)  # 24 (default) or 72 -- see WIZ_DURATION
    status = Column(SQLEnum(VipOfferStatus, create_constraint=True, validate_strings=True), nullable=False, default=VipOfferStatus.ACTIVE, index=True)
    source_message_id = Column(String, nullable=False)

    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)
    updated_at = Column(DateTime(timezone=True), default=timeutils.utcnow, onupdate=timeutils.utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)


class WhatsAppVipOfferInterestDB(Base):
    """'ME INTERESA <id>' -- registered once per (offer, interested person),
    DB-enforced (same unique-constraint-is-the-real-guarantee pattern as
    ProcessedWebhookEventDB). Never stores/publishes the interested party's
    whatsapp_id to anyone but the offer owner's own notification."""

    __tablename__ = "whatsapp_vip_offer_interests"
    __table_args__ = (UniqueConstraint("offer_id", "interested_channel_identity_id", name="uq_vip_offer_interest"),)

    id = Column(Integer, primary_key=True, index=True)
    offer_id = Column(Integer, ForeignKey("whatsapp_vip_offers.id"), nullable=False, index=True)
    interested_channel_identity_id = Column(Integer, ForeignKey("channel_identities.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)


class OutboundMessageStatus(str, Enum):
    """See services/whatsapp_outbox.py module docstring for the full state
    machine and the documented delivery-uncertainty window. Never treat
    META_ACCEPTED as proof of DELIVERED -- those are deliberately different
    states, only the latter is set from a real Meta status webhook."""

    PENDING = "pending"
    SENDING = "sending"
    META_ACCEPTED = "meta_accepted"
    DELIVERED = "delivered"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    UNCERTAIN_NEEDS_RECONCILIATION = "uncertain_needs_reconciliation"


class OutboundMessageDB(Base):
    """The outbox: every VIP-flow outbound reply is persisted here BEFORE
    being attempted, so a crash/restart can always recover 'we owed this
    person a reply' without re-running the inbound handler (see
    services/whatsapp_outbox.py). Channel-agnostic in shape (whatsapp_id is
    just an opaque recipient id) but only used by the WhatsApp VIP flow in
    V1 -- the existing wallet-flow ConversationRouter still calls
    adapter.send() directly, unchanged, out of this iteration's scope."""

    __tablename__ = "whatsapp_outbound_messages"

    id = Column(Integer, primary_key=True, index=True)
    whatsapp_id = Column(String, nullable=False, index=True)
    body_text = Column(String, nullable=False)
    status = Column(SQLEnum(OutboundMessageStatus, create_constraint=True, validate_strings=True), nullable=False, default=OutboundMessageStatus.PENDING, index=True)
    wamid = Column(String, unique=True, nullable=True, index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    last_error = Column(String, nullable=True)  # sanitized only -- exception type / Meta numeric error code, never a raw body
    source_message_id = Column(String, nullable=True, index=True)  # the inbound wamid that caused this reply, for observability only (not an idempotency key here)
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)
    updated_at = Column(DateTime(timezone=True), default=timeutils.utcnow, onupdate=timeutils.utcnow)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True, index=True)


class OutboxWorkerLeaseDB(Base):
    """Lease rows so periodic outbox sweeps stay single-worker-responsible
    even if this process is ever scaled to more than one instance (V1
    deployment is a single systemd process, where this is a no-op formality
    every tick wins -- see SERVER_DEPLOYMENT_RUNBOOK.md). Acquired via an
    atomic compare-and-swap UPDATE, the same idiom already used elsewhere in
    this codebase's audited services for atomic balance/state updates --
    never a naive read-then-write.

    id=1 is the WhatsApp VIP outbox's lease (see services/whatsapp_outbox.py).
    id=2 is the Telegram VIP outbox's lease (services/telegram_outbox.py,
    2026-09-27) -- a separate row in the SAME table rather than a second
    table, since both are the identical single-row-per-worker-domain shape;
    the two sweeps never contend with each other's row."""

    __tablename__ = "whatsapp_outbox_worker_lease"

    id = Column(Integer, primary_key=True)
    owner_token = Column(String, nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Telegram VIP outbox (2026-09-27) -- durable outbound delivery for
# telegram_vip_router.py, added after an audit proved the pilot's original
# "commit DB, then best-effort adapter.send()" design loses replies: a
# transient Telegram API failure OR a process crash between the atomic
# commit and the send loop leaves NOTHING durable to recover from (nothing
# was ever persisted -- pending_sends was a plain in-memory list). See
# services/telegram_outbox.py's module docstring for the full state machine,
# which mirrors services/whatsapp_outbox.py's (OutboundMessageDB/
# OutboundMessageStatus) with one simplification: Telegram's Bot API
# sendMessage response IS the definitive synchronous outcome (200 = sent,
# non-2xx/exception = not sent) -- there is no separate asynchronous
# delivery-status webhook to correlate later, so SENT is the terminal
# success state (no META_ACCEPTED/DELIVERED split).
# ---------------------------------------------------------------------------


class TelegramOutboundMessageStatus(str, Enum):
    """Revision 2026-09-27 (second correction, "última corrección antes de
    cerrar un commit local"): FAILED_PERMANENT added, distinct from
    FAILED_TERMINAL. FAILED_PERMANENT is reached on the FIRST attempt that
    gets a definitive, known-permanent rejection (HTTP 400/401/403 -- see
    messaging/telegram_adapter.TelegramApiPermanentError) -- no retry is
    EVER attempted, because we know from the response itself that retrying
    the identical request cannot succeed. FAILED_TERMINAL is reached only
    after a TRANSIENT failure (429/5xx, TelegramApiTransientError) has
    been retried until WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS is exhausted --
    a genuinely different history, kept as a genuinely different terminal
    state so an operator scanning the outbox can tell "never going to
    work" apart from "we gave up after N honest tries"."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    FAILED_PERMANENT = "failed_permanent"
    UNCERTAIN_NEEDS_RECONCILIATION = "uncertain_needs_reconciliation"


class TelegramOutboundMessageDB(Base):
    """The Telegram counterpart of OutboundMessageDB -- every VIP-flow
    outbound Telegram reply is persisted here BEFORE being attempted, in the
    SAME atomic commit as the wizard/session-state advance and the webhook
    idempotency marker (see routers/telegram.py's
    _handle_telegram_vip_message_atomically and
    telegram_vip_router.py's _send/handle_inbound)."""

    __tablename__ = "telegram_outbound_messages"

    id = Column(Integer, primary_key=True, index=True)
    telegram_chat_id = Column(String, nullable=False, index=True)
    body_text = Column(String, nullable=False)
    status = Column(SQLEnum(TelegramOutboundMessageStatus, create_constraint=True, validate_strings=True), nullable=False, default=TelegramOutboundMessageStatus.PENDING, index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    last_error = Column(String, nullable=True)  # sanitized only -- exception type + HTTP status_code + Telegram's own numeric error_code, never a raw response body or the bot_token
    source_message_id = Column(String, nullable=True, index=True)  # the inbound Telegram message_id that caused this reply, for observability only
    created_at = Column(DateTime(timezone=True), default=timeutils.utcnow)
    updated_at = Column(DateTime(timezone=True), default=timeutils.utcnow, onupdate=timeutils.utcnow)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True, index=True)
