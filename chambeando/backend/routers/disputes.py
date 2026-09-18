"""
Evidencia de disputas — nunca URLs publicas, nunca archivos en el repo/DB en
claro (ver services/evidence_storage.py). Politica de autorizacion (ver
services/authorization.can_view_dispute_evidence):
  - la parte que la envio siempre puede verla;
  - la contraparte matcheada tambien (ambos son "parte" de la orden);
  - el arbitro on-chain autorizado, solo mientras la orden este DISPUTED;
  - staff (MODERATOR/ADMIN) SOLO con una DisputeAssignmentDB activa y especifica
    para esa orden — el rol por si solo NO otorga acceso (Phase 2B.1; corrige
    el acceso amplio de Phase 2B). Ver routers/admin.py para la gestion de
    asignaciones.
"""

import base64
import hashlib

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_membership, get_current_user
from ..models import DisputeEvidenceDB, MembershipDB, OrderStatus, P2POrderDB, SecurityEventType, UserDB
from ..schemas import DisputeEvidenceContent, DisputeEvidenceCreate, DisputeEvidenceOut
from ..security.audit import log_security_event
from ..services.authorization import can_view_dispute_evidence, is_order_party
from ..services.evidence_storage import get_evidence_storage

router = APIRouter(prefix="/disputes", tags=["disputes"])


@router.post("/evidence", response_model=DisputeEvidenceOut, status_code=201)
def submit_evidence(
    payload: DisputeEvidenceCreate,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),
    current_user: UserDB = Depends(get_current_user),
):
    order = db.query(P2POrderDB).filter(P2POrderDB.id == payload.order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    if order.onchain_status != OrderStatus.DISPUTED:
        raise HTTPException(status_code=400, detail="La orden no esta en disputa on-chain")
    if not is_order_party(order, current_user):
        raise HTTPException(status_code=403, detail="No sos parte de esta orden")

    storage_ref = None
    content_hash = None
    if payload.evidence_type == "file":
        if not payload.content_base64:
            raise HTTPException(status_code=400, detail="content_base64 requerido para evidence_type=file")
        raw = base64.b64decode(payload.content_base64)
        content_hash = hashlib.sha256(raw).hexdigest()
        storage_ref = get_evidence_storage().store(
            order_id=order.id, content=raw, content_type=payload.content_type or "application/octet-stream"
        )

    evidence = DisputeEvidenceDB(
        order_id=order.id,
        submitted_by_user_id=current_user.id,
        evidence_type=payload.evidence_type,
        storage_ref=storage_ref,
        content_hash=content_hash,
        note=payload.note,
    )
    db.add(evidence)
    db.flush()  # puebla evidence.id para el evento de auditoria, sin comprometer la transaccion

    log_security_event(
        db,
        action=SecurityEventType.DISPUTE_EVIDENCE_SUBMITTED,
        actor_user_id=current_user.id,
        target_type="order",
        target_id=order.id,
        # NUNCA el contenido de la nota/archivo — solo que algo se sometio
    )
    db.commit()  # UNA sola transaccion: evidencia + evento de auditoria juntos
    db.refresh(evidence)
    return evidence


@router.get("/{order_id}/evidence", response_model=list[DisputeEvidenceOut])
def list_evidence(
    order_id: int,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),
    current_user: UserDB = Depends(get_current_user),
):
    order = db.query(P2POrderDB).filter(P2POrderDB.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    if not can_view_dispute_evidence(db, order, current_user):
        raise HTTPException(status_code=403, detail="No autorizado para ver evidencia de esta orden")

    items = db.query(DisputeEvidenceDB).filter(DisputeEvidenceDB.order_id == order_id).order_by(DisputeEvidenceDB.id).all()

    # solo-lectura: no hay otra escritura con la que ser atomico, pero el evento
    # SI necesita su propio commit ahora que log_security_event ya no comitea
    log_security_event(
        db, action=SecurityEventType.DISPUTE_EVIDENCE_VIEWED, actor_user_id=current_user.id, target_type="order", target_id=order_id
    )
    db.commit()
    return items


@router.get("/evidence/{evidence_id}/content", response_model=DisputeEvidenceContent)
def get_evidence_content(
    evidence_id: int,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),
    current_user: UserDB = Depends(get_current_user),
):
    evidence = db.query(DisputeEvidenceDB).filter(DisputeEvidenceDB.id == evidence_id).first()
    if evidence is None or evidence.storage_ref is None:
        raise HTTPException(status_code=404, detail="Evidencia no encontrada")
    order = db.query(P2POrderDB).filter(P2POrderDB.id == evidence.order_id).first()
    if order is None or not can_view_dispute_evidence(db, order, current_user):
        raise HTTPException(status_code=403, detail="No autorizado para ver esta evidencia")

    try:
        raw = get_evidence_storage().retrieve(evidence.storage_ref)
    except KeyError:
        raise HTTPException(status_code=404, detail="Contenido no disponible")

    log_security_event(
        db,
        action=SecurityEventType.DISPUTE_EVIDENCE_VIEWED,
        actor_user_id=current_user.id,
        target_type="dispute_evidence",
        target_id=evidence.id,
    )
    db.commit()
    return DisputeEvidenceContent(content_type=None, content_base64=base64.b64encode(raw).decode())
