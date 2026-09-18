"""
Perfil propio + reputacion. La reputacion se computa siempre en el momento
desde P2POrderDB (ver services/reputation.py) — no hay endpoint de escritura
para ningun contador."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_membership, get_current_user
from ..models import MembershipDB, MembershipStatus, UserDB
from ..schemas import MeOut, ReputationOut
from ..services.reputation import get_reputation

router = APIRouter(tags=["users"])


@router.get("/me", response_model=MeOut)
def read_own_profile(
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    membership = db.query(MembershipDB).filter(MembershipDB.user_id == current_user.id).first()
    return MeOut(
        wallet_address=current_user.wallet_address,  # el propio usuario SI ve su wallet completa
        alias=current_user.alias,
        is_member=membership is not None and membership.status == MembershipStatus.ACTIVE,
        role=membership.role if membership else None,
    )


@router.get("/reputation/{user_id}", response_model=ReputationOut)
def read_reputation(
    user_id: int,
    db: Session = Depends(get_db),
    membership: MembershipDB = Depends(get_current_membership),  # requiere MEMBER activo — nunca publico
):
    target = db.query(UserDB).filter(UserDB.id == user_id).first()
    if target is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    summary = get_reputation(db, target)
    return ReputationOut(
        completed_trades=summary.completed_trades,
        cancelled_trades=summary.cancelled_trades,
        disputes_opened=summary.disputes_opened,
        disputes_won=summary.disputes_won,
        disputes_lost=summary.disputes_lost,
        account_age_days=summary.account_age_days,
        successful_volume=summary.successful_volume,
    )
