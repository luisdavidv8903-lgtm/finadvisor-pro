"""Endpoints de autenticacion por firma de wallet. Rate-limited a proposito
(seccion 6: fuerza bruta de nonce/firma) — ver security/rate_limit.py."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import auth
from ..chain import get_chain_adapter
from ..config import settings
from ..database import get_db
from ..models import AuthNonceDB, MembershipDB, MembershipStatus, SecurityEventType, UserDB
from ..schemas import NonceRequest, NonceResponse, TokenResponse, VerifyRequest
from ..security.audit import log_security_event
from ..security.rate_limit import RateLimiter, enforce_rate_limit, get_rate_limiter

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/nonce", response_model=NonceResponse)
def request_nonce(
    payload: NonceRequest,
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
):
    enforce_rate_limit(limiter, f"nonce:{payload.wallet_address}", settings.RATE_LIMIT_NONCE_PER_MINUTE)
    nonce = auth.generate_nonce(db, payload.wallet_address)
    return NonceResponse(nonce=nonce, message=auth.build_sign_message(nonce))


@router.post("/verify", response_model=TokenResponse)
def verify_signature(
    payload: VerifyRequest,
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
):
    enforce_rate_limit(limiter, f"verify:{payload.wallet_address}", settings.RATE_LIMIT_VERIFY_PER_MINUTE)

    existing_user = db.query(UserDB).filter(UserDB.wallet_address == payload.wallet_address).first()

    def _log_failure(reason: str) -> None:
        # sin escritura primaria que acompañar (es una denegacion, no una accion
        # exitosa) — commit propio e inmediato, ver security/audit.py
        log_security_event(
            db,
            action=SecurityEventType.AUTH_FAILURE,
            actor_user_id=existing_user.id if existing_user else None,
            target_type="wallet",
            target_id=payload.wallet_address,
            reason=reason,
        )
        db.commit()

    # el nonce se recupera implicitamente: buscamos el mas reciente no usado para esta wallet
    entry = (
        db.query(AuthNonceDB)
        .filter(AuthNonceDB.wallet_address == payload.wallet_address, AuthNonceDB.used == 0)
        .order_by(AuthNonceDB.created_at.desc())
        .first()
    )
    if entry is None:
        _log_failure("no pending nonce")
        raise HTTPException(status_code=400, detail="Solicitá un nonce primero con /auth/nonce")

    message = auth.build_sign_message(entry.nonce)
    if not get_chain_adapter().verify_wallet_signature(payload.wallet_address, message, payload.signature):
        _log_failure("invalid signature")
        raise HTTPException(status_code=401, detail="Firma inválida")

    if not auth.verify_and_consume_nonce(db, payload.wallet_address, entry.nonce):
        _log_failure("nonce expired or already used")
        raise HTTPException(status_code=401, detail="Nonce expirado o ya utilizado")

    user = existing_user
    if user is None:
        user = UserDB(wallet_address=payload.wallet_address)
        db.add(user)
        db.commit()
        db.refresh(user)

    log_security_event(db, action=SecurityEventType.AUTH_SUCCESS, actor_user_id=user.id, target_type="wallet", target_id=user.wallet_address)
    db.commit()

    membership = db.query(MembershipDB).filter(MembershipDB.user_id == user.id).first()
    is_member = membership is not None and membership.status == MembershipStatus.ACTIVE

    return TokenResponse(access_token=auth.create_access_token(payload.wallet_address), is_member=is_member)
