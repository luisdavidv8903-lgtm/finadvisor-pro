"""
Lógica de negocio del escrow P2P. Todas las mutaciones de balance/estado usan
UPDATE ... WHERE <guard> condicionado (compare-and-swap a nivel SQL) para ser
atómicas sin depender de locks explícitos, y funcionan igual en SQLite y Postgres.
En Postgres, combinar además con with_for_update() cuando un flujo toca múltiples
filas relacionadas (ver AUDIT.md sección 1.1).
"""

from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.orm import Session

from .models import OrderStatus, P2POrderDB, UserDB, WalletDB


def create_p2p_order(
    db: Session,
    seller: UserDB,
    crypto_amount: Decimal,
    fiat_amount: Decimal,
    fiat_currency: str,
    payment_method: str,
) -> P2POrderDB:
    try:
        result = db.execute(
            update(WalletDB)
            .where(
                WalletDB.user_id == seller.id,
                WalletDB.currency == "USDT",
                WalletDB.balance >= crypto_amount,
            )
            .values(
                balance=WalletDB.balance - crypto_amount,
                locked_balance=WalletDB.locked_balance + crypto_amount,
            )
        )
        if result.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Saldo insuficiente o billetera no encontrada",
            )

        new_order = P2POrderDB(
            seller_id=seller.id,
            crypto_amount=crypto_amount,
            fiat_amount=fiat_amount,
            fiat_currency=fiat_currency,
            payment_method=payment_method,
            status=OrderStatus.PENDING,
        )
        db.add(new_order)
        db.commit()
        db.refresh(new_order)
        return new_order
    except Exception:
        db.rollback()
        raise


def mark_order_as_paid(db: Session, order_id: int, buyer: UserDB) -> P2POrderDB:
    order = db.query(P2POrderDB).filter(P2POrderDB.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    if order.seller_id == buyer.id:
        raise HTTPException(status_code=400, detail="No puedes comprar tu propia orden")

    try:
        result = db.execute(
            update(P2POrderDB)
            .where(P2POrderDB.id == order_id, P2POrderDB.status == OrderStatus.PENDING)
            .values(status=OrderStatus.PAID, buyer_id=buyer.id)
        )
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="La orden ya no está en estado pendiente (posible carrera con otro comprador)",
            )
        db.commit()
        db.refresh(order)
        return order
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise


def release_escrow(db: Session, order_id: int, seller: UserDB) -> P2POrderDB:
    order = db.query(P2POrderDB).filter(P2POrderDB.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    if order.seller_id != seller.id:
        raise HTTPException(status_code=403, detail="No tienes autorización para liberar esta orden")

    try:
        # 1. Transición de estado atómica: actúa como "lock lógico" de la orden.
        #    Si dos requests concurrentes llegan aquí, solo una gana la carrera.
        result = db.execute(
            update(P2POrderDB)
            .where(P2POrderDB.id == order_id, P2POrderDB.status == OrderStatus.PAID)
            .values(status=OrderStatus.COMPLETED)
        )
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="La orden debe estar en estado 'paid' para liberar los fondos (o ya fue liberada)",
            )

        # 2. Solo la request que ganó la carrera de arriba llega aquí.
        seller_wallet_result = db.execute(
            update(WalletDB)
            .where(
                WalletDB.user_id == order.seller_id,
                WalletDB.currency == "USDT",
                WalletDB.locked_balance >= order.crypto_amount,
            )
            .values(locked_balance=WalletDB.locked_balance - order.crypto_amount)
        )
        if seller_wallet_result.rowcount == 0:
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail="Error crítico de consistencia en el balance retenido del vendedor",
            )

        # get_or_create atómico del wallet del comprador (evita duplicados por carrera, ver AUDIT.md 1.4)
        buyer_wallet = (
            db.query(WalletDB)
            .filter(WalletDB.user_id == order.buyer_id, WalletDB.currency == "USDT")
            .with_for_update()
            .first()
        )
        if not buyer_wallet:
            buyer_wallet = WalletDB(user_id=order.buyer_id, currency="USDT", balance=0)
            db.add(buyer_wallet)
            db.flush()

        db.execute(
            update(WalletDB)
            .where(WalletDB.id == buyer_wallet.id)
            .values(balance=WalletDB.balance + order.crypto_amount)
        )

        db.commit()
        db.refresh(order)
        return order
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise


def cancel_order(db: Session, order_id: int, seller: UserDB) -> P2POrderDB:
    order = db.query(P2POrderDB).filter(P2POrderDB.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    if order.seller_id != seller.id:
        raise HTTPException(status_code=403, detail="Solo el creador/vendedor puede cancelar la orden")

    try:
        result = db.execute(
            update(P2POrderDB)
            .where(P2POrderDB.id == order_id, P2POrderDB.status == OrderStatus.PENDING)
            .values(status=OrderStatus.CANCELLED)
        )
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No se puede cancelar una orden que ya está pagada, cancelada o finalizada",
            )

        wallet_result = db.execute(
            update(WalletDB)
            .where(
                WalletDB.user_id == order.seller_id,
                WalletDB.currency == "USDT",
                WalletDB.locked_balance >= order.crypto_amount,
            )
            .values(
                locked_balance=WalletDB.locked_balance - order.crypto_amount,
                balance=WalletDB.balance + order.crypto_amount,
            )
        )
        if wallet_result.rowcount == 0:
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail="Error crítico de consistencia en el balance retenido del vendedor",
            )

        db.commit()
        db.refresh(order)
        return order
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise
