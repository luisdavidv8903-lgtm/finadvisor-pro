"""
Autenticación por firma de wallet (challenge-response), no por contraseña. La
identidad de un usuario es su wallet_address; demostrarla es firmar un nonce de
un solo uso con la clave privada de esa wallet (nunca se envía la clave, solo
la firma).

IMPORTANTE: autenticarse (probar que controlas una wallet) NO es lo mismo que
ser miembro del marketplace. `get_current_user` solo prueba identidad — nunca
crea ni verifica membership. El unico camino para convertirse en member es
redimir un invite valido (ver routers/invites.py). Ver deps.py para los
dependency providers que SI exigen membership activo/rol.

La verificacion de firma es especifica de cada cadena (TRON hoy) y por eso vive
detras de EscrowChainAdapter.verify_wallet_signature — este modulo no importa
tronpy ni ninguna libreria especifica de cadena (ver seccion 5 del informe).
"""

import secrets
from datetime import timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import update
from sqlalchemy.orm import Session

from . import timeutils
from .config import settings
from .database import get_db
from .models import AuthNonceDB, UserDB

bearer_scheme = HTTPBearer(auto_error=False)


def generate_nonce(db: Session, wallet_address: str) -> str:
    nonce = secrets.token_hex(16)
    expires_at = timeutils.utcnow() + timedelta(seconds=settings.NONCE_EXPIRE_SECONDS)
    db.add(AuthNonceDB(wallet_address=wallet_address, nonce=nonce, expires_at=expires_at))
    db.commit()
    return nonce


def build_sign_message(nonce: str) -> str:
    return f"Chambeando login: {nonce}"


def verify_and_consume_nonce(db: Session, wallet_address: str, nonce: str) -> bool:
    """UPDATE atomico condicionado (compare-and-swap a nivel SQL) — el nonce se
    invalida en la MISMA operacion que lo verifica, para que dos requests
    concurrentes con la misma firma nunca puedan pasar ambas (replay bajo
    concurrencia). `rowcount == 1` es la unica forma de exito; cualquier otra
    cosa (no existe, ya usado, expirado) es `False` sin distincion — no filtramos
    cual de las tres fue, para no dar pistas a un atacante sobre el estado interno."""
    now = timeutils.utcnow()
    result = db.execute(
        update(AuthNonceDB)
        .where(
            AuthNonceDB.wallet_address == wallet_address,
            AuthNonceDB.nonce == nonce,
            AuthNonceDB.used == 0,
            AuthNonceDB.expires_at >= now,
        )
        .values(used=1)
        # synchronize_session=False: no necesitamos que SQLAlchemy mantenga
        # sincronizados objetos ya cargados en la sesion (solo nos importa
        # rowcount) — evita ademas que su evaluador Python compare el datetime
        # aware `now` contra el datetime naive que SQLite devuelve para filas ya
        # cargadas en la sesion (TypeError: offset-naive vs offset-aware).
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def create_access_token(wallet_address: str) -> str:
    expire = timeutils.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": wallet_address, "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> UserDB:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudo validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise credentials_exception
    try:
        payload = jwt.decode(credentials.credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        wallet_address: str | None = payload.get("sub")
        if wallet_address is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(UserDB).filter(UserDB.wallet_address == wallet_address).first()
    if user is None:
        # la wallet ya demostro poseer la clave (JWT valido) pero no tiene fila propia
        # (p.ej. DB de dev reseteada) — se crea la identidad minima. Esto NUNCA crea
        # membership: ver docstring del modulo.
        user = UserDB(wallet_address=wallet_address)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
