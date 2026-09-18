"""
Redencion de invites — el UNICO camino para que una wallet autenticada se
convierta en member. Autenticarse (JWT valido) no alcanza: ver auth.py.

La logica transaccional vive en services/invites.py (extraido en Phase 2B.1
para poder probar atomicidad/concurrencia directamente, sin pasar por HTTP).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import get_current_user
from ..models import SecurityEventType, UserDB
from ..schemas import InviteRedeemRequest, MembershipSelfOut
from ..security.audit import log_security_event
from ..security.rate_limit import RateLimiter, enforce_rate_limit, get_rate_limiter
from ..services.invites import InviteRedemptionError, redeem_invite_for_user

router = APIRouter(prefix="/invites", tags=["invites"])


@router.post("/redeem", response_model=MembershipSelfOut)
def redeem_invite(
    payload: InviteRedeemRequest,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
    limiter: RateLimiter = Depends(get_rate_limiter),
):
    enforce_rate_limit(limiter, f"invite-redeem:{current_user.wallet_address}", settings.RATE_LIMIT_INVITE_REDEEM_PER_MINUTE)

    try:
        # el evento INVITE_REDEEMED del caso exitoso ya se logea DENTRO de
        # redeem_invite_for_user, en la misma transaccion que el consumo del
        # invite y la creacion de la membership (ver services/invites.py).
        membership = redeem_invite_for_user(db, payload.code, current_user)
    except InviteRedemptionError as exc:
        # denegacion: no hay escritura primaria que acompañar (la transaccion
        # que fallo ya se revirtio) — commit propio e inmediato.
        log_security_event(
            db,
            action=SecurityEventType.INVITE_REDEMPTION_DENIED,
            actor_user_id=current_user.id,
            target_type="invite",
            target_id=None,  # nunca el codigo, ni siquiera su hash, en el log
            reason=exc.reason,
        )
        db.commit()
        raise HTTPException(status_code=exc.status_code, detail=exc.reason)

    return membership
