from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy import Enum as SQLEnum

from .database import Base


class UserRole(str, Enum):
    USER = "user"
    MERCHANT = "merchant"
    ADMIN = "admin"


class OrderStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"


class UserDB(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(SQLEnum(UserRole), default=UserRole.USER, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class WalletDB(Base):
    __tablename__ = "wallets"
    __table_args__ = (UniqueConstraint("user_id", "currency", name="uq_wallet_user_currency"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    currency = Column(String, nullable=False)
    # Numeric(18, 8): 8 decimales, suficiente para BTC/USDT. Nunca Float para dinero.
    balance = Column(Numeric(18, 8), default=0, nullable=False)
    locked_balance = Column(Numeric(18, 8), default=0, nullable=False)


class P2POrderDB(Base):
    __tablename__ = "p2p_orders"
    id = Column(Integer, primary_key=True, index=True)
    seller_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    buyer_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    crypto_amount = Column(Numeric(18, 8), nullable=False)
    fiat_amount = Column(Numeric(12, 2), nullable=False)
    fiat_currency = Column(String, nullable=False)
    payment_method = Column(String, nullable=False)
    status = Column(SQLEnum(OrderStatus), default=OrderStatus.PENDING, nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
