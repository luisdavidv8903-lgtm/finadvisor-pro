"""
Modelo de datos v2. El estado financiero (onchain_status, montos) vive en el
contrato EscrowP2P — Postgres es un espejo de solo-lectura mantenido por el
indexer (indexer.py), nunca escrito directamente desde un endpoint a partir
de un request de usuario. WalletDB ya no existe: el contrato es la custodia.
"""

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy import Enum as SQLEnum

from .database import Base


class UserRole(str, Enum):
    USER = "user"
    MERCHANT = "merchant"
    ADMIN = "admin"


class OrderStatus(str, Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    PAID = "paid"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"
    REFUNDED = "refunded"


class UserDB(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    wallet_address = Column(String, unique=True, index=True, nullable=False)  # identidad real
    username = Column(String, unique=True, nullable=True)
    email = Column(String, unique=True, nullable=True)
    role = Column(SQLEnum(UserRole), default=UserRole.USER, nullable=False)
    reputation_score = Column(Numeric(5, 2), default=0, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AuthNonceDB(Base):
    """Nonce de un solo uso para el login por firma de wallet (challenge-response)."""

    __tablename__ = "auth_nonces"
    id = Column(Integer, primary_key=True, index=True)
    wallet_address = Column(String, index=True, nullable=False)
    nonce = Column(String, nullable=False, unique=True)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Integer, default=0, nullable=False)  # 0/1 — evita reutilizar el mismo nonce
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class P2POrderDB(Base):
    __tablename__ = "p2p_orders"
    __table_args__ = (UniqueConstraint("onchain_order_id", name="uq_order_onchain_id"),)

    id = Column(Integer, primary_key=True, index=True)
    onchain_order_id = Column(Integer, index=True, nullable=False)
    escrow_tx_hash = Column(String, index=True, nullable=True)
    token_address = Column(String, nullable=False)

    seller_wallet = Column(String, nullable=False, index=True)
    buyer_wallet = Column(String, nullable=True, index=True)

    crypto_amount = Column(Numeric(18, 8), nullable=False)
    fiat_amount = Column(Numeric(12, 2), nullable=True)   # se completa en el paso de metadata (fuera de la cadena)
    fiat_currency = Column(String, nullable=True)
    payment_method = Column(String, nullable=True)

    # Solo el indexer escribe este campo — nunca un endpoint a partir de un request de usuario.
    onchain_status = Column(SQLEnum(OrderStatus), nullable=False, default=OrderStatus.OPEN)
    confirmed_block = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class DisputeEvidenceDB(Base):
    __tablename__ = "dispute_evidence"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("p2p_orders.id"), nullable=False, index=True)
    submitted_by_wallet = Column(String, nullable=False)
    file_url = Column(String, nullable=False)
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ArbiterResolutionDB(Base):
    """Registro de auditoría: por qué el árbitro resolvió una disputa de cierta forma."""

    __tablename__ = "arbiter_resolutions"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("p2p_orders.id"), nullable=False, index=True)
    resolved_by = Column(String, nullable=False)
    recipient = Column(String, nullable=False)
    reasoning = Column(String, nullable=False)
    resolve_tx_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class IndexerCheckpointDB(Base):
    __tablename__ = "indexer_checkpoints"
    id = Column(Integer, primary_key=True, index=True)
    contract_address = Column(String, unique=True, nullable=False)
    last_processed_block = Column(Integer, nullable=False, default=0)
