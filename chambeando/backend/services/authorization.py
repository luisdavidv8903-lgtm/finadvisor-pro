"""
Predicados de autorizacion explicitos y pequenos, reutilizados por los routers.
A proposito NO hay un "resolver de visibilidad" generico/magico — cada endpoint
llama estos predicados y decide explicitamente que campos devolver (ver
DATA_VISIBILITY_MATRIX.md). Fail closed: si algo no calza con ninguna regla, no
hay acceso.
"""

from sqlalchemy.orm import Session

from .. import timeutils
from ..models import DisputeAssignmentDB, MemberRole, MembershipDB, OrderStatus, P2POrderDB, UserDB


def mask_wallet(address: str) -> str:
    """Nunca se expone la wallet completa en listados de marketplace (seccion 7).
    Muestra solo prefijo/sufijo — suficiente para que un humano reconozca "es la
    misma wallet que ya vi" sin exponer el valor completo."""
    if len(address) <= 10:
        return address
    return f"{address[:6]}…{address[-4:]}"


def is_order_party(order: P2POrderDB, user: UserDB) -> bool:
    """True si `user` es el comprador o vendedor (matched counterparty) de esta orden."""
    wallet = user.wallet_address
    return wallet == order.seller_wallet or wallet == order.buyer_wallet


def is_authorized_arbitrator(order: P2POrderDB, user: UserDB) -> bool:
    """True SOLO si la orden esta DISPUTED on-chain (segun el espejo del indexer) Y
    la wallet del caller es el arbiterSnapshot on-chain de ESA orden especifica —
    nunca un rol backend generico. Este es el vinculo explicito pedido: "Any field
    already authoritative on-chain should reference, not contradict, the contract."
    """
    if order.onchain_status != OrderStatus.DISPUTED:
        return False
    if not order.arbiter_snapshot_wallet:
        return False
    return user.wallet_address == order.arbiter_snapshot_wallet


def is_moderator_or_admin(membership: MembershipDB) -> bool:
    return membership.role in (MemberRole.MODERATOR, MemberRole.ADMIN)


def is_admin(membership: MembershipDB) -> bool:
    return membership.role == MemberRole.ADMIN


def can_view_settlement_detail(order: P2POrderDB, user: UserDB, membership: MembershipDB) -> bool:
    """MATCHED_COUNTERPARTY: solo de SU propio trade. DISPUTE_ARBITRATOR: solo si la
    orden esta DISPUTED y el caller es el arbiter on-chain de esa orden. MODERATOR
    puede ver evidencia (no settlement) de disputas activas — ver
    can_view_dispute_evidence; settlement es mas sensible, MODERATOR no lo obtiene
    automaticamente aqui (seccion 3: "Access to decrypted data must be enforced
    server-side" + seccion 11: MODERATOR "must NOT ... view arbitrary settlement
    details outside an authorized matched/disputed trade" — y un moderador no es,
    por defecto, el arbitro de la disputa)."""
    if is_order_party(order, user):
        return True
    if is_authorized_arbitrator(order, user):
        return True
    return False


def has_active_dispute_assignment(db: Session, order: P2POrderDB, user: UserDB) -> bool:
    """Unica via de acceso a evidencia para staff que NO es parte del trade ni
    arbitro on-chain — ver DisputeAssignmentDB. Tener rol MODERATOR/ADMIN por si
    solo NO basta (corregido en Phase 2B.1 — Phase 2B daba acceso amplio a
    cualquier MODERATOR/ADMIN mientras la orden estuviera DISPUTED, lo cual era
    demasiado amplio)."""
    now = timeutils.utcnow()
    assignment = (
        db.query(DisputeAssignmentDB)
        .filter(
            DisputeAssignmentDB.order_id == order.id,
            DisputeAssignmentDB.assigned_user_id == user.id,
            DisputeAssignmentDB.revoked_at.is_(None),
            DisputeAssignmentDB.expires_at >= now,
        )
        .first()
    )
    return assignment is not None


def can_view_dispute_evidence(db: Session, order: P2POrderDB, user: UserDB) -> bool:
    """Parte del trade siempre puede ver su propia evidencia. El arbitro on-chain
    autorizado puede verla mientras este DISPUTED. MODERATOR/ADMIN NUNCA obtienen
    acceso solo por su rol — necesitan una DisputeAssignmentDB activa y
    especifica para ESTA orden (ver has_active_dispute_assignment)."""
    if is_order_party(order, user):
        return True
    if is_authorized_arbitrator(order, user):
        return True
    if has_active_dispute_assignment(db, order, user):
        return True
    return False
