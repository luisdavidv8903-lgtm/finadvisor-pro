"""
Order book del marketplace — MEMBER-only (ninguna ruta de este router es
publica). Ver DATA_VISIBILITY_MATRIX.md: OrderMemberOut nunca incluye wallet
completa ni datos de liquidacion.

`onchain_status` y demas campos (B) nunca se escriben desde aqui — solo se
leen. `attach_order_metadata` adjunta metadata (A) DESPUES de que el indexer ya
proceso el evento OrderCreated, y verifica que el caller sea realmente el
vendedor on-chain de esa orden.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_membership, get_current_user
from ..models import MembershipDB, P2POrderDB, SettlementDetailDB, UserDB
from ..schemas import OrderMemberOut, OrderMetadataCreate
from ..services.authorization import mask_wallet

router = APIRouter(prefix="/orders", tags=["orders"])


def _to_member_out(order: P2POrderDB) -> OrderMemberOut:
    return OrderMemberOut(
        id=order.id,
        onchain_status=order.onchain_status,
        seller_wallet_masked=mask_wallet(order.seller_wallet),
        buyer_wallet_masked=mask_wallet(order.buyer_wallet) if order.buyer_wallet else None,
        crypto_amount=order.crypto_amount,
        fiat_amount=order.fiat_amount,
        fiat_currency=order.fiat_currency,
        payment_method=order.payment_method,
        quoted_rate=order.quoted_rate,
        created_at=order.created_at,
    )


@router.get("/", response_model=list[OrderMemberOut])
def list_orders(
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),
):
    orders = db.query(P2POrderDB).order_by(P2POrderDB.created_at.desc()).all()
    return [_to_member_out(o) for o in orders]


@router.get("/{order_id}", response_model=OrderMemberOut)
def get_order(
    order_id: int,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),
):
    order = db.query(P2POrderDB).filter(P2POrderDB.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    return _to_member_out(order)


@router.post("/metadata", response_model=OrderMemberOut, status_code=status.HTTP_201_CREATED)
def attach_order_metadata(
    payload: OrderMetadataCreate,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),
    current_user: UserDB = Depends(get_current_user),
):
    order = db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == payload.onchain_order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada on-chain todavia (esperá a que el indexer la procese)")
    if order.seller_wallet != current_user.wallet_address:
        raise HTTPException(status_code=403, detail="Solo el vendedor de esta orden puede adjuntar metadata")
    if order.fiat_amount is not None:
        raise HTTPException(status_code=400, detail="Esta orden ya tiene metadata adjunta")

    if payload.settlement_detail_id is not None:
        detail = db.query(SettlementDetailDB).filter(SettlementDetailDB.id == payload.settlement_detail_id).first()
        if detail is None or detail.owner_user_id != current_user.id or not detail.active:
            raise HTTPException(status_code=400, detail="settlement_detail_id invalido o no pertenece al caller")
        order.settlement_detail_id = detail.id
        # snapshot congelado, tomado AHORA, en la MISMA transaccion que el resto
        # de esta metadata — ver SETTLEMENT_LIFECYCLE.md. Como esta orden nunca
        # puede volver a pasar por aqui (guard de "ya tiene metadata adjunta"
        # arriba), este snapshot es efectivamente write-once: nada despues de
        # esto puede alterar lo que la contraparte de ESTE trade especifico ve,
        # ni siquiera revocar o reemplazar el registro maestro.
        order.settlement_snapshot_payload = detail.encrypted_payload
        order.settlement_snapshot_payment_method = detail.payment_method

    order.fiat_amount = payload.fiat_amount
    order.fiat_currency = payload.fiat_currency
    order.payment_method = payload.payment_method
    order.created_by_user_id = current_user.id
    if order.crypto_amount:
        order.quoted_rate = Decimal(payload.fiat_amount) / Decimal(order.crypto_amount)

    db.commit()
    db.refresh(order)
    return _to_member_out(order)
