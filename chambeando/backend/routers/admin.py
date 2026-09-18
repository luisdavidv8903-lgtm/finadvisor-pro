"""
Acciones privilegiadas de MODERATOR/ADMIN. Ver seccion 11 y ROLE_PRIVILEGE_MATRIX
en el informe: ninguna funcion de aqui toca P2POrderDB.onchain_status, mueve
fondos, cambia comprador/vendedor, ni edita contadores de reputacion — esos
campos no tienen NINGUN endpoint de escritura en todo el backend (son
exclusivos del indexer o del contrato). No existe un endpoint "god mode".
"""

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import timeutils
from ..database import get_db
from ..deps import require_role
from ..models import (
    DisputeAssignmentDB,
    InviteDB,
    MemberRole,
    MembershipDB,
    MembershipStatus,
    OrderStatus,
    P2POrderDB,
    SecurityEventDB,
    SecurityEventType,
)
from ..schemas import (
    DisputeAssignmentCreateRequest,
    DisputeAssignmentOut,
    InviteCreateRequest,
    InviteCreateResponse,
    MembershipAdminOut,
    RoleAssignRequest,
    SecurityEventOut,
    SuspendMembershipRequest,
)
from ..security.audit import log_security_event
from ..services.invites import hash_invite_code

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/invites", response_model=InviteCreateResponse)
def create_invite(
    payload: InviteCreateRequest,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.ADMIN)),
):
    code = secrets.token_urlsafe(16)  # devuelto UNA sola vez — nunca se vuelve a poder leer
    invite = InviteDB(
        code_hash=hash_invite_code(code),
        created_by_user_id=membership.user_id,
        max_uses=payload.max_uses,
        expires_at=timeutils.utcnow() + timedelta(hours=payload.expires_in_hours),
    )
    db.add(invite)
    db.flush()  # puebla invite.id para el evento de auditoria, sin comprometer la transaccion
    log_security_event(
        db, action=SecurityEventType.INVITE_CREATED, actor_user_id=membership.user_id, target_type="invite", target_id=invite.id
    )
    db.commit()  # UNA sola transaccion: invite + evento de auditoria juntos
    db.refresh(invite)
    return InviteCreateResponse(id=invite.id, code=code, max_uses=invite.max_uses, expires_at=invite.expires_at)


@router.post("/invites/{invite_id}/revoke", status_code=204)
def revoke_invite(
    invite_id: int,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.ADMIN)),
):
    invite = db.query(InviteDB).filter(InviteDB.id == invite_id).first()
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite no encontrado")
    invite.revoked_at = timeutils.utcnow()
    log_security_event(
        db, action=SecurityEventType.INVITE_REVOKED, actor_user_id=membership.user_id, target_type="invite", target_id=invite.id
    )
    db.commit()  # UNA sola transaccion: revocacion + evento de auditoria juntos


@router.post("/memberships/{user_id}/suspend", response_model=MembershipAdminOut)
def suspend_membership(
    user_id: int,
    payload: SuspendMembershipRequest,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.MODERATOR, MemberRole.ADMIN)),
):
    target = db.query(MembershipDB).filter(MembershipDB.user_id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="Membership no encontrada")
    target.status = MembershipStatus.SUSPENDED
    target.suspended_at = timeutils.utcnow()
    target.suspended_reason = payload.reason
    log_security_event(
        db,
        action=SecurityEventType.MEMBERSHIP_SUSPENDED,
        actor_user_id=membership.user_id,
        target_type="user",
        target_id=user_id,
        reason=payload.reason,
    )
    db.commit()  # UNA sola transaccion: suspension + evento de auditoria juntos
    db.refresh(target)
    return target


@router.post("/memberships/{user_id}/reactivate", response_model=MembershipAdminOut)
def reactivate_membership(
    user_id: int,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.ADMIN)),
):
    target = db.query(MembershipDB).filter(MembershipDB.user_id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="Membership no encontrada")
    target.status = MembershipStatus.ACTIVE
    target.suspended_at = None
    target.suspended_reason = None
    log_security_event(
        db, action=SecurityEventType.MEMBERSHIP_REACTIVATED, actor_user_id=membership.user_id, target_type="user", target_id=user_id
    )
    db.commit()  # UNA sola transaccion: reactivacion + evento de auditoria juntos
    db.refresh(target)
    return target


@router.post("/memberships/{user_id}/role", response_model=MembershipAdminOut)
def assign_role(
    user_id: int,
    payload: RoleAssignRequest,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.ADMIN)),
):
    target = db.query(MembershipDB).filter(MembershipDB.user_id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="Membership no encontrada")
    old_role = target.role
    target.role = payload.role
    log_security_event(
        db,
        action=SecurityEventType.ROLE_CHANGED,
        actor_user_id=membership.user_id,
        target_type="user",
        target_id=user_id,
        reason=f"{old_role.value} -> {payload.role.value}",
    )
    db.commit()  # UNA sola transaccion: cambio de rol + evento de auditoria juntos
    db.refresh(target)
    return target


@router.get("/memberships", response_model=list[MembershipAdminOut])
def list_memberships(
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.ADMIN)),
):
    return db.query(MembershipDB).order_by(MembershipDB.id).all()


@router.get("/security-events", response_model=list[SecurityEventOut])
def list_security_events(
    limit: int = 100,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.ADMIN)),
):
    # least-privilege: solo lectura, nunca contiene valores sensibles (ver models.py) —
    # aun asi acotado por `limit` para no volverse una exportacion masiva sin proposito.
    limit = min(limit, 500)
    return db.query(SecurityEventDB).order_by(SecurityEventDB.id.desc()).limit(limit).all()


# ---------------------------------------------------------------------------
# Dispute assignments — SOLO ADMIN puede crear/revocar. Esto es la UNICA via
# por la que staff (MODERATOR/ADMIN) obtiene acceso a evidencia de una disputa
# de la que no es parte ni arbitro on-chain (ver services/authorization.py).
# Estas funciones SOLO LEEN P2POrderDB para validar que la orden existe y esta
# DISPUTED — nunca escriben onchain_status ni ningun campo de fondos/estado
# on-chain (eso sigue siendo exclusivo del indexer/contrato).
# ---------------------------------------------------------------------------


@router.post("/disputes/{order_id}/assignments", response_model=DisputeAssignmentOut, status_code=201)
def create_dispute_assignment(
    order_id: int,
    payload: DisputeAssignmentCreateRequest,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.ADMIN)),
):
    order = db.query(P2POrderDB).filter(P2POrderDB.id == order_id).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    if order.onchain_status != OrderStatus.DISPUTED:
        raise HTTPException(status_code=400, detail="Solo se puede asignar revision de ordenes DISPUTED")

    target_membership = db.query(MembershipDB).filter(MembershipDB.user_id == payload.assigned_user_id).first()
    if target_membership is None or target_membership.role not in (MemberRole.MODERATOR, MemberRole.ADMIN):
        raise HTTPException(status_code=400, detail="assigned_user_id debe ser un MODERATOR o ADMIN activo")

    assignment = DisputeAssignmentDB(
        order_id=order_id,
        assigned_user_id=payload.assigned_user_id,
        assigned_by_user_id=membership.user_id,
        reason=payload.reason,
        expires_at=timeutils.utcnow() + timedelta(hours=payload.expires_in_hours),
    )
    db.add(assignment)
    db.flush()  # puebla assignment.id para el evento de auditoria, sin comprometer la transaccion

    log_security_event(
        db,
        action=SecurityEventType.DISPUTE_ASSIGNMENT_CREATED,
        actor_user_id=membership.user_id,
        target_type="dispute_assignment",
        target_id=assignment.id,
        reason=f"order_id={order_id} assigned_user_id={payload.assigned_user_id}",
    )
    db.commit()  # UNA sola transaccion: assignment + evento de auditoria juntos
    db.refresh(assignment)
    return assignment


@router.post("/disputes/assignments/{assignment_id}/revoke", response_model=DisputeAssignmentOut)
def revoke_dispute_assignment(
    assignment_id: int,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.ADMIN)),
):
    assignment = db.query(DisputeAssignmentDB).filter(DisputeAssignmentDB.id == assignment_id).first()
    if assignment is None:
        raise HTTPException(status_code=404, detail="Asignacion no encontrada")
    assignment.revoked_at = timeutils.utcnow()
    log_security_event(
        db,
        action=SecurityEventType.DISPUTE_ASSIGNMENT_REVOKED,
        actor_user_id=membership.user_id,
        target_type="dispute_assignment",
        target_id=assignment.id,
    )
    db.commit()  # UNA sola transaccion: revocacion + evento de auditoria juntos
    db.refresh(assignment)
    return assignment


@router.get("/disputes/{order_id}/assignments", response_model=list[DisputeAssignmentOut])
def list_dispute_assignments(
    order_id: int,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.ADMIN)),
):
    return db.query(DisputeAssignmentDB).filter(DisputeAssignmentDB.order_id == order_id).order_by(DisputeAssignmentDB.id.desc()).all()
