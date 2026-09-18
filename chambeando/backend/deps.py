"""
Dependency providers de FastAPI. Distincion central de esta fase:

  get_current_user          -> prueba SOLO identidad (wallet firmo el nonce)
  get_current_membership    -> ademas exige una Membership ACTIVA (ver models.py)
  require_role(*roles)      -> ademas exige que el rol de la membership este en `roles`

Fail closed: cualquier ambiguedad (sin membership, membership suspendida, rol no
listado) es 403, nunca "dejar pasar por si acaso".
"""

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from .auth import get_current_user  # re-exportado para import consistente desde routers
from .database import get_db
from .models import MemberRole, MembershipDB, MembershipStatus, UserDB

__all__ = [
    "get_current_user",
    "get_current_membership",
    "require_role",
]


def get_current_membership(
    current_user: UserDB = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MembershipDB:
    membership = db.query(MembershipDB).filter(MembershipDB.user_id == current_user.id).first()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Se requiere ser member del marketplace")
    if membership.status != MembershipStatus.ACTIVE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Membership suspendida")
    return membership


def require_role(*roles: MemberRole):
    """Cada endpoint declara explicitamente que roles puede llamarlo — sin
    jerarquia implicita (ADMIN no hereda automaticamente permisos de MODERATOR;
    un endpoint que deba permitir ambos lista ambos explicitamente). Esto es a
    proposito mas verboso pero elimina sorpresas de "ADMIN puede todo"."""

    def dependency(membership: MembershipDB = Depends(get_current_membership)) -> MembershipDB:
        if membership.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Rol insuficiente para esta accion")
        return membership

    return dependency
