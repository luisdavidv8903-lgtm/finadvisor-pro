"""Endpoints de autenticacion por firma de wallet. Rate-limited a proposito
(seccion 6: fuerza bruta de nonce/firma) — ver security/rate_limit.py."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import auth
from ..chain import get_chain_adapter, verify_wallet_signature_for_auth
from ..chain.auth_networks import AuthNetworkError, get_auth_network_chain_family
from ..config import settings
from ..database import get_db
from ..models import AuthNonceDB, MembershipDB, MembershipStatus, SecurityEventType, UserDB
from ..schemas import NonceRequest, NonceResponse, TokenResponse, VerifyRequest
from ..security.audit import log_security_event
from ..security.rate_limit import RateLimiter, enforce_rate_limit, get_rate_limiter

router = APIRouter(prefix="/auth", tags=["auth"])


def _resolve_network_id(network_id: str | None) -> str:
    """None -> settings.CHAIN_ADAPTER ("tron") -- EXACTAMENTE el default
    implicito que ya existia antes de esta revision (get_chain_adapter() sin
    argumento siempre resolvia CHAIN_ADAPTER), asi que un cliente que nunca
    manda network_id seguis viendo el mismo comportamiento de siempre.
    Fallo cerrado (400) para cualquier red que no este en la whitelist de
    chain/auth_networks.py -- nunca se intenta adivinar ni caer a un default
    silencioso para una red desconocida."""
    resolved = network_id or settings.CHAIN_ADAPTER
    try:
        get_auth_network_chain_family(resolved)
    except AuthNetworkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return resolved


def _verify_signature(network_id: str, wallet_address: str, message: str, signature: str) -> bool:
    """Ruta TRON heredada preservada EXACTAMENTE como estaba (revision
    2026-09-25): sigue pasando por get_chain_adapter() sin argumento -- la
    misma ruta legado de siempre, que en tests es donde FakeChainAdapter se
    inyecta (ver tests/conftest.py:fake_chain). Cambiar esto para TRON
    tambien habria roto decenas de tests existentes que dependen de esa
    inyeccion, ademas de no ser necesario: el pedido especifico era una via
    NUEVA para redes auth-only (EVM), sin tocar la que ya funciona.
    Cualquier otra red admitida (ver chain/auth_networks.py) usa la funcion
    PURA nueva -- jamas construye un EvmEscrowAdapter, jamas abre un RPC,
    jamas lee un contrato."""
    if network_id == settings.CHAIN_ADAPTER:
        return get_chain_adapter().verify_wallet_signature(wallet_address, message, signature)
    chain_family = get_auth_network_chain_family(network_id)
    return verify_wallet_signature_for_auth(chain_family, wallet_address, message, signature)


@router.post("/nonce", response_model=NonceResponse)
def request_nonce(
    payload: NonceRequest,
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
):
    enforce_rate_limit(limiter, f"nonce:{payload.wallet_address}", settings.RATE_LIMIT_NONCE_PER_MINUTE)
    network_id = _resolve_network_id(payload.network_id)
    nonce = auth.generate_nonce(db, payload.wallet_address, network_id)
    return NonceResponse(nonce=nonce, message=auth.build_sign_message(nonce))


@router.post("/verify", response_model=TokenResponse)
def verify_signature(
    payload: VerifyRequest,
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
):
    enforce_rate_limit(limiter, f"verify:{payload.wallet_address}", settings.RATE_LIMIT_VERIFY_PER_MINUTE)
    network_id = _resolve_network_id(payload.network_id)

    existing_user = (
        db.query(UserDB).filter(UserDB.wallet_address == payload.wallet_address, UserDB.network_id == network_id).first()
    )

    def _log_failure(reason: str) -> None:
        # sin escritura primaria que acompañar (es una denegacion, no una accion
        # exitosa) — commit propio e inmediato, ver security/audit.py
        log_security_event(
            db,
            action=SecurityEventType.AUTH_FAILURE,
            actor_user_id=existing_user.id if existing_user else None,
            target_type="wallet",
            target_id=f"{network_id}:{payload.wallet_address}",
            reason=reason,
        )
        db.commit()

    # el nonce se recupera implicitamente: buscamos el mas reciente no usado
    # para esta wallet EN ESTA RED -- filtrar tambien por network_id es lo
    # que impide pedir un nonce para una red y canjearlo bajo otra (ver
    # AuthNonceDB.network_id).
    entry = (
        db.query(AuthNonceDB)
        .filter(AuthNonceDB.wallet_address == payload.wallet_address, AuthNonceDB.network_id == network_id, AuthNonceDB.used == 0)
        .order_by(AuthNonceDB.created_at.desc())
        .first()
    )
    if entry is None:
        _log_failure("no pending nonce")
        raise HTTPException(status_code=400, detail="Solicitá un nonce primero con /auth/nonce")

    message = auth.build_sign_message(entry.nonce)
    if not _verify_signature(network_id, payload.wallet_address, message, payload.signature):
        _log_failure("invalid signature")
        raise HTTPException(status_code=401, detail="Firma inválida")

    if not auth.verify_and_consume_nonce(db, payload.wallet_address, network_id, entry.nonce):
        _log_failure("nonce expired or already used")
        raise HTTPException(status_code=401, detail="Nonce expirado o ya utilizado")

    user = existing_user
    if user is None:
        user = UserDB(wallet_address=payload.wallet_address, network_id=network_id)
        db.add(user)
        db.commit()
        db.refresh(user)

    log_security_event(
        db,
        action=SecurityEventType.AUTH_SUCCESS,
        actor_user_id=user.id,
        target_type="wallet",
        target_id=f"{network_id}:{user.wallet_address}",
    )
    db.commit()

    membership = db.query(MembershipDB).filter(MembershipDB.user_id == user.id).first()
    is_member = membership is not None and membership.status == MembershipStatus.ACTIVE

    return TokenResponse(access_token=auth.create_access_token(payload.wallet_address, network_id), is_member=is_member)
