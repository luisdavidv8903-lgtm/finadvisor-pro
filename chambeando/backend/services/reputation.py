"""
Reputacion derivada — a proposito NO hay una tabla de "reputation_score" mutable
que alguien (ni siquiera ADMIN) pueda editar directamente. Todo aqui se computa
en el momento a partir de P2POrderDB (escrito exclusivamente por el indexer, ver
models.py), asi que no existe una via de API para "enviar" o "fabricar" un
contador — ver seccion 11 (ADMIN must NOT ... fabricate reputation history).
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import timeutils
from ..models import OrderStatus, P2POrderDB, UserDB


@dataclass(frozen=True)
class ReputationSummary:
    completed_trades: int
    cancelled_trades: int
    disputes_opened: int
    disputes_won: int
    disputes_lost: int
    account_age_days: int
    successful_volume: str  # Decimal serializado como string — nunca float


def get_reputation(db: Session, user: UserDB) -> ReputationSummary:
    wallet = user.wallet_address
    orders = (
        db.query(P2POrderDB)
        .filter(or_(P2POrderDB.seller_wallet == wallet, P2POrderDB.buyer_wallet == wallet))
        .all()
    )

    completed = cancelled = disputes_opened = disputes_won = disputes_lost = 0
    volume = Decimal("0")

    for o in orders:
        is_seller = o.seller_wallet == wallet
        is_buyer = o.buyer_wallet == wallet

        if o.onchain_status == OrderStatus.RELEASED:
            completed += 1
            if is_seller and o.crypto_amount is not None:
                volume += Decimal(str(o.crypto_amount))
        elif o.onchain_status == OrderStatus.CANCELLED:
            cancelled += 1

        if o.was_disputed:
            disputes_opened += 1
            # RELEASED tras disputa = el comprador gano la disputa; REFUNDED = el vendedor la gano.
            if o.onchain_status == OrderStatus.RELEASED:
                disputes_won += 1 if is_buyer else 0
                disputes_lost += 1 if is_seller else 0
            elif o.onchain_status == OrderStatus.REFUNDED:
                disputes_won += 1 if is_seller else 0
                disputes_lost += 1 if is_buyer else 0

    created = timeutils.ensure_utc(user.created_at)
    account_age_days = (timeutils.utcnow() - created).days if created else 0

    return ReputationSummary(
        completed_trades=completed,
        cancelled_trades=cancelled,
        disputes_opened=disputes_opened,
        disputes_won=disputes_won,
        disputes_lost=disputes_lost,
        account_age_days=max(account_age_days, 0),
        successful_volume=str(volume),
    )
