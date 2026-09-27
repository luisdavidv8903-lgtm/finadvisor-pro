"""
CLI local para administrar el acceso al piloto VIP de WhatsApp. NO existe (ni
existira) una ruta HTTP equivalente a proposito -- ver requirement 1 del
piloto: un mensaje entrante al webhook NUNCA prueba pertenencia al grupo, asi
que la unica forma de otorgar acceso es que el operador (con acceso directo a
este servidor/DB) lo autorice explicitamente desde aca.

Subcomandos:
    authorize   --whatsapp-id <id> [--note TEXT]
                Autoriza directamente un WhatsApp id ya conocido.
    invite      --max-uses N --ttl-days N [--note TEXT]
                Genera un codigo de invitacion de un solo uso (por defecto),
                para compartir fuera de banda con alguien cuyo WhatsApp id
                todavia no se conoce. Imprime el codigo EN TEXTO PLANO UNA
                SOLA VEZ -- no se puede recuperar despues (solo su hash vive
                en la DB, mismo criterio que InviteDB).
    revoke      --membership-id N --reason TEXT
                Suspende el acceso VIP de un miembro ya autorizado.
    list        Lista miembros VIP actuales (status, via, fecha) -- nunca
                imprime tokens/codigos.

Requiere que la base de datos ya este migrada (`alembic upgrade head`).

Uso:
    python -m backend.vip_admin authorize --whatsapp-id 15551234567 --confirm
    python -m backend.vip_admin invite --max-uses 1 --ttl-days 14 --confirm
    python -m backend.vip_admin list
"""

import argparse
import sys

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from .database import SessionLocal
from .messaging.whatsapp_vip_identity import authorize_whatsapp_id, create_vip_invite, revoke_membership
from .models import ChannelIdentityDB, WhatsAppVipMembershipDB


class VipAdminError(Exception):
    pass


def _check_database_migrated(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    required = {"alembic_version", "channel_identities", "whatsapp_vip_memberships", "whatsapp_vip_invites"}
    missing = required - tables
    if missing:
        raise VipAdminError(f"Faltan tablas requeridas ({', '.join(sorted(missing))}) -- corre `alembic upgrade head` primero.")


def _cmd_authorize(args, session_factory) -> int:
    if not args.confirm:
        print("Falta --confirm. Este comando no procede sin confirmacion explicita.", file=sys.stderr)
        return 2
    db = session_factory()
    try:
        _check_database_migrated(db.get_bind())
        membership = authorize_whatsapp_id(db, args.whatsapp_id, note=args.note)
    finally:
        db.close()
    print(f"Autorizado: whatsapp_id={args.whatsapp_id} membership_id={membership.id} status={membership.status.value}")
    return 0


def _cmd_invite(args, session_factory) -> int:
    if not args.confirm:
        print("Falta --confirm. Este comando no procede sin confirmacion explicita.", file=sys.stderr)
        return 2
    db = session_factory()
    try:
        _check_database_migrated(db.get_bind())
        code = create_vip_invite(db, max_uses=args.max_uses, ttl_days=args.ttl_days, note=args.note)
    finally:
        db.close()
    # UNICA vez que el codigo en texto plano existe fuera de la memoria de
    # quien lo comparta -- nunca se vuelve a poder recuperar de la DB.
    print(f"Codigo de invitacion (compartir fuera de banda, no se puede volver a ver): {code}")
    print(f"max_uses={args.max_uses} ttl_days={args.ttl_days}")
    return 0


def _cmd_revoke(args, session_factory) -> int:
    if not args.confirm:
        print("Falta --confirm. Este comando no procede sin confirmacion explicita.", file=sys.stderr)
        return 2
    db = session_factory()
    try:
        _check_database_migrated(db.get_bind())
        membership = db.query(WhatsAppVipMembershipDB).filter(WhatsAppVipMembershipDB.id == args.membership_id).first()
        if membership is None:
            print(f"No existe membership_id={args.membership_id}", file=sys.stderr)
            return 1
        revoke_membership(db, membership, reason=args.reason)
    finally:
        db.close()
    print(f"Revocado: membership_id={args.membership_id}")
    return 0


def _cmd_list(args, session_factory) -> int:
    db = session_factory()
    try:
        _check_database_migrated(db.get_bind())
        rows = db.query(WhatsAppVipMembershipDB).order_by(WhatsAppVipMembershipDB.id).all()
        for m in rows:
            identity = db.query(ChannelIdentityDB).filter(ChannelIdentityDB.id == m.channel_identity_id).first()
            # whatsapp_id se imprime porque el OPERADOR ya lo conoce/eligio
            # (es quien corre este script) -- distinto de publicarlo en un
            # anuncio de oferta, que nunca ocurre en ningun otro lugar de
            # este pilot.
            print(f"id={m.id} whatsapp_id={identity.channel_user_id if identity else '?'} status={m.status.value} via={m.authorized_via.value}")
    finally:
        db.close()
    return 0


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.vip_admin", description="Administra el acceso al piloto VIP de WhatsApp.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("authorize", help="Autoriza directamente un WhatsApp id.")
    p_auth.add_argument("--whatsapp-id", required=True)
    p_auth.add_argument("--note", default=None)
    p_auth.add_argument("--confirm", action="store_true")

    p_invite = sub.add_parser("invite", help="Genera un codigo de invitacion de un solo uso.")
    p_invite.add_argument("--max-uses", type=int, default=1)
    p_invite.add_argument("--ttl-days", type=int, default=14)
    p_invite.add_argument("--note", default=None)
    p_invite.add_argument("--confirm", action="store_true")

    p_revoke = sub.add_parser("revoke", help="Suspende el acceso VIP de un miembro.")
    p_revoke.add_argument("--membership-id", type=int, required=True)
    p_revoke.add_argument("--reason", required=True)
    p_revoke.add_argument("--confirm", action="store_true")

    sub.add_parser("list", help="Lista miembros VIP actuales.")

    args = parser.parse_args(argv)
    factory = session_factory or SessionLocal

    try:
        if args.command == "authorize":
            return _cmd_authorize(args, factory)
        if args.command == "invite":
            return _cmd_invite(args, factory)
        if args.command == "revoke":
            return _cmd_revoke(args, factory)
        if args.command == "list":
            return _cmd_list(args, factory)
    except VipAdminError as exc:
        print(f"Rechazado: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
