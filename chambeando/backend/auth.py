"""
Autenticación por firma de wallet (challenge-response), no por contraseña.
La identidad de un usuario es su wallet_address; demostrarla es firmar un
nonce de un solo uso con la clave privada de esa wallet (nunca se envía la
clave, solo la firma).

NOTA DE IMPLEMENTACIÓN: la verificación de firma usa tronpy. La API exacta de
recuperación de dirección desde una firma puede variar entre versiones de
tronpy — validar contra la versión pineada en requirements.txt antes de
desplegar, y cubrir con un test de integración que firme con una clave de
testnet conocida y confirme que verify_tron_signature devuelve True/False
correctamente en ambos casos.
"""

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import AuthNonceDB, UserDB

bearer_scheme = HTTPBearer(auto_error=False)


def generate_nonce(db: Session, wallet_address: str) -> str:
    nonce = secrets.token_hex(16)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.NONCE_EXPIRE_SECONDS)
    db.add(AuthNonceDB(wallet_address=wallet_address, nonce=nonce, expires_at=expires_at))
    db.commit()
    return nonce


def build_sign_message(nonce: str) -> str:
    return f"Chambeando login: {nonce}"


def verify_tron_signature(address: str, message: str, signature: str) -> bool:
    """
    Verifica que `signature` corresponde a `message` firmado por `address`.
    Implementación de referencia con tronpy — revisar contra la versión real
    instalada antes de producción.
    """
    try:
        from tronpy.keys import PublicKey

        recovered = PublicKey.recover_from_msg(message.encode(), bytes.fromhex(signature.removeprefix("0x")))
        return recovered.to_base58check_address() == address
    except Exception:
        return False


def verify_and_consume_nonce(db: Session, wallet_address: str, nonce: str) -> bool:
    entry = (
        db.query(AuthNonceDB)
        .filter(AuthNonceDB.wallet_address == wallet_address, AuthNonceDB.nonce == nonce, AuthNonceDB.used == 0)
        .first()
    )
    if not entry or entry.expires_at < datetime.now(timezone.utc):
        return False
    entry.used = 1  # de un solo uso: evita replay del mismo nonce/firma
    db.commit()
    return True


def create_access_token(wallet_address: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
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
        # primer login de esta wallet: se crea el perfil automáticamente, sin contraseña
        user = UserDB(wallet_address=wallet_address)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
