"""
Logica transaccional de redencion de invites, separada del router para poder
probarla directamente con dos sesiones de DB independientes (ver
tests/test_invite_atomicity.py) — HTTP de por medio no permite controlar el
entrelazado exacto de dos transacciones concurrentes.

GARANTIA: el consumo de cupo del invite (`used_count += 1`), la creacion de la
membership, Y su evento de auditoria (INVITE_REDEEMED) son LA MISMA
transaccion de base de datos — nunca se hace commit entre una cosa y la otra
(Phase 2B.3: el evento de auditoria se sumo a esta misma transaccion, antes
vivia en el router como un commit separado). Si la creacion de la membership
falla por cualquier motivo (wallet ya es member, carrera de UniqueConstraint),
`db.rollback()` deshace TAMBIEN el incremento de `used_count` Y el evento de
auditoria — un invite nunca puede terminar con cupo consumido, o con un evento
de "redimido" persistido, sin una membership real detras.
"""

import hashlib

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import timeutils
from ..models import InviteDB, MembershipDB, SecurityEventType, UserDB
from ..security.audit import log_security_event


def hash_invite_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class InviteRedemptionError(Exception):
    def __init__(self, reason: str, status_code: int):
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)


def redeem_invite_for_user(db: Session, code: str, user: UserDB) -> MembershipDB:
    if db.query(MembershipDB).filter(MembershipDB.user_id == user.id).first() is not None:
        raise InviteRedemptionError("wallet already has a membership", 400)

    code_hash = hash_invite_code(code)
    invite = db.query(InviteDB).filter(InviteDB.code_hash == code_hash).first()
    if invite is None:
        raise InviteRedemptionError("invite code not found", 404)

    now = timeutils.utcnow()

    # incremento atomico condicionado (compare-and-swap a nivel SQL): el guard
    # (no revocado, no expirado, cupo disponible) y el incremento son la MISMA
    # sentencia UPDATE, evaluada atomicamente por el motor de la base de datos —
    # dos redenciones concurrentes cerca del limite de `max_uses` nunca pueden
    # ambas "ganar" mas cupo del que existe (ver test_invite_atomicity.py).
    result = db.execute(
        update(InviteDB)
        .where(
            InviteDB.id == invite.id,
            InviteDB.revoked_at.is_(None),
            InviteDB.expires_at >= now,
            InviteDB.used_count < InviteDB.max_uses,
        )
        .values(used_count=InviteDB.used_count + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        db.rollback()
        raise InviteRedemptionError("invite expired, revoked, or exhausted", 400)

    membership = MembershipDB(
        user_id=user.id,
        invited_by_user_id=invite.created_by_user_id,
        invite_id=invite.id,
    )
    db.add(membership)
    try:
        # log_security_event hace db.flush() internamente — eso puede ser el
        # punto donde realmente se detecta la violacion de UniqueConstraint(user_id)
        # (antes de esto solo se detectaba en el commit), por eso el try/except
        # envuelve ambas llamadas, no solo el commit.
        log_security_event(
            db,
            action=SecurityEventType.INVITE_REDEEMED,
            actor_user_id=user.id,
            target_type="invite",
            target_id=invite.id,
        )
        db.commit()
    except IntegrityError:
        # carrera con otra redencion de ESTA MISMA wallet — el UniqueConstraint(user_id)
        # en memberships es el guard final. El rollback deshace TAMBIEN el
        # incremento de used_count Y el evento de auditoria de arriba, porque los
        # tres viven en la misma transaccion sin commit intermedio.
        db.rollback()
        raise InviteRedemptionError("concurrent membership creation for the same wallet", 409)

    db.refresh(membership)
    return membership
