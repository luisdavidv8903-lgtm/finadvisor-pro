"""Reportes de abuso — cualquier MEMBER puede reportar; MODERATOR/ADMIN revisan."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import timeutils
from ..database import get_db
from ..deps import get_current_membership, get_current_user, require_role
from ..models import MemberRole, MembershipDB, ReportDB, SecurityEventType, UserDB
from ..schemas import ReportCreateRequest, ReportOut, ReportReviewRequest
from ..security.audit import log_security_event

router = APIRouter(tags=["reports"])


@router.post("/reports", response_model=ReportOut, status_code=201)
def file_report(
    payload: ReportCreateRequest,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),
    current_user: UserDB = Depends(get_current_user),
):
    report = ReportDB(
        reporter_user_id=current_user.id,
        reported_user_id=payload.reported_user_id,
        order_id=payload.order_id,
        reason=payload.reason,
    )
    db.add(report)
    db.flush()  # puebla report.id para el evento de auditoria, sin comprometer la transaccion
    log_security_event(db, action=SecurityEventType.REPORT_FILED, actor_user_id=current_user.id, target_type="report", target_id=report.id)
    db.commit()  # UNA sola transaccion: reporte + evento de auditoria juntos
    db.refresh(report)
    return report


@router.get("/moderation/reports", response_model=list[ReportOut])
def list_reports(
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.MODERATOR, MemberRole.ADMIN)),
):
    return db.query(ReportDB).order_by(ReportDB.id.desc()).all()


@router.post("/moderation/reports/{report_id}/review", response_model=ReportOut)
def review_report(
    report_id: int,
    payload: ReportReviewRequest,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(require_role(MemberRole.MODERATOR, MemberRole.ADMIN)),
):
    report = db.query(ReportDB).filter(ReportDB.id == report_id).first()
    if report is None:
        raise HTTPException(status_code=404, detail="Reporte no encontrado")

    old_status = report.status
    report.status = payload.status
    report.reviewed_by_user_id = membership.user_id
    report.reviewed_at = timeutils.utcnow()
    # accion privilegiada (MODERATOR/ADMIN-only, muta estado persistente) —
    # ver seccion 8 del informe de Phase 2B.3: primaria + auditoria, una sola transaccion
    log_security_event(
        db,
        action=SecurityEventType.REPORT_REVIEWED,
        actor_user_id=membership.user_id,
        target_type="report",
        target_id=report_id,
        reason=f"{old_status} -> {payload.status}",
    )
    db.commit()
    db.refresh(report)
    return report
