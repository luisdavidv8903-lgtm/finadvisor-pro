"""
Datos de liquidacion (fiat) — cifrados en reposo, nunca en columnas planas, logs,
URLs ni on-chain (ver security/crypto.py). Acceso a texto plano SOLO para:
  - el propio owner (self-service);
  - la contraparte matcheada de un trade especifico donde su settlement_detail
    esta enlazado (P2POrderDB.settlement_detail_id);
  - el arbitro on-chain autorizado, solo mientras esa orden este DISPUTED.
Cada revelacion se audita (sin el valor) — ver security/audit.py.

IMPORTANTE (Phase 2B.2, ver SETTLEMENT_LIFECYCLE.md): `reveal_order_settlement`
lee de `P2POrderDB.settlement_snapshot_*` (una copia congelada tomada UNA VEZ
en attach_order_metadata), NUNCA del registro SettlementDetailDB "maestro" en
vivo. Revocar el maestro despues de que una orden ya tomo su snapshot no
afecta a esa orden — es exactamente la propiedad de seguridad pedida ("una vez
MATCHED, las instrucciones de esa orden no cambian silenciosamente").
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import timeutils
from ..database import get_db
from ..deps import get_current_membership, get_current_user
from ..models import MembershipDB, P2POrderDB, SecurityEventType, SettlementDetailDB, UserDB
from ..schemas import SettlementDetailCreateRequest, SettlementDetailOut, SettlementDetailRevealed
from ..security.audit import log_security_event
from ..security.crypto import decrypt_settlement_payload, encrypt_settlement_payload
from ..services.authorization import can_view_settlement_detail

router = APIRouter(tags=["settlement"])


@router.post("/settlement-details", response_model=SettlementDetailOut, status_code=201)
def create_settlement_detail(
    payload: SettlementDetailCreateRequest,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),
    current_user: UserDB = Depends(get_current_user),
):
    detail = SettlementDetailDB(
        owner_user_id=current_user.id,
        payment_method=payload.payment_method,
        currency=payload.currency,
        encrypted_payload=encrypt_settlement_payload(payload.payload),
    )
    db.add(detail)
    db.flush()  # puebla detail.id para el evento de auditoria, sin comprometer la transaccion
    log_security_event(
        db, action=SecurityEventType.SETTLEMENT_DETAIL_CREATED, actor_user_id=current_user.id, target_type="settlement_detail", target_id=detail.id
    )
    db.commit()  # UNA sola transaccion: settlement detail (sensible) + evento de auditoria juntos
    db.refresh(detail)
    return detail


@router.get("/settlement-details", response_model=list[SettlementDetailOut])
def list_own_settlement_details(
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),
    current_user: UserDB = Depends(get_current_user),
):
    return (
        db.query(SettlementDetailDB)
        .filter(SettlementDetailDB.owner_user_id == current_user.id)
        .order_by(SettlementDetailDB.id.desc())
        .all()
    )


@router.post("/settlement-details/{detail_id}/revoke", response_model=SettlementDetailOut)
def revoke_settlement_detail(
    detail_id: int,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),
    current_user: UserDB = Depends(get_current_user),
):
    detail = db.query(SettlementDetailDB).filter(SettlementDetailDB.id == detail_id).first()
    if detail is None or detail.owner_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Settlement detail no encontrado")
    detail.active = False
    detail.revoked_at = timeutils.utcnow()
    log_security_event(
        db, action=SecurityEventType.SETTLEMENT_DETAIL_REVOKED, actor_user_id=current_user.id, target_type="settlement_detail", target_id=detail.id
    )
    db.commit()  # UNA sola transaccion: revocacion + evento de auditoria juntos
    db.refresh(detail)
    return detail


@router.get("/orders/{order_id}/settlement", response_model=SettlementDetailRevealed)
def reveal_order_settlement(
    order_id: int,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),
    current_user: UserDB = Depends(get_current_user),
):
    order = db.query(P2POrderDB).filter(P2POrderDB.id == order_id).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    # fail closed: sin autorizacion explicita, 403 — no se revela ni siquiera si el
    # settlement_detail existe o no (evita filtrar informacion por diferencia de error).
    if not can_view_settlement_detail(order, current_user, membership):
        raise HTTPException(status_code=403, detail="No autorizado para ver datos de liquidacion de esta orden")

    if order.settlement_snapshot_payload is None:
        raise HTTPException(status_code=404, detail="Esta orden no tiene datos de liquidacion adjuntos")

    # SIEMPRE el snapshot congelado de la orden — nunca una relectura del
    # registro maestro, que pudo haber sido revocado despues sin afectar a
    # este trade especifico (ver docstring del modulo).
    payload = decrypt_settlement_payload(order.settlement_snapshot_payload)

    # solo-lectura: no hay otra escritura con la que ser atomico, pero el evento
    # SI necesita su propio commit ahora que log_security_event ya no comitea
    log_security_event(
        db,
        action=SecurityEventType.SETTLEMENT_DETAIL_VIEWED,
        actor_user_id=current_user.id,
        target_type="order",
        target_id=order_id,
        # NUNCA `payload` ni ningun fragmento de el en `reason` — ver security/audit.py
    )
    db.commit()

    return SettlementDetailRevealed(
        id=order.settlement_detail_id,
        payment_method=order.settlement_snapshot_payment_method,
        currency=order.fiat_currency,
        payload=payload,
    )
