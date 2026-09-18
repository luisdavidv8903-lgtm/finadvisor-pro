"""
CLI local para crear el PRIMER usuario ADMIN de Chambeando. NO existe (ni
existira) una ruta HTTP equivalente a proposito: quien puede crear el primer
admin es una decision operacional (quien tiene acceso al servidor/DB donde
corre este script), nunca algo alcanzable desde la API publica.

Una vez que existe CUALQUIER admin, este comando se niega a crear otro — los
admins subsiguientes se gestionan via `POST /admin/memberships/{user_id}/role`,
que ya requiere estar autenticado como un ADMIN existente. Bootstrap es
estrictamente para arrancar de cero.

Uso:
    python -m backend.bootstrap_admin --wallet <address> --confirm

Requiere que la base de datos ya este migrada (`alembic upgrade head`) — este
script NUNCA crea ni migra tablas por su cuenta.

Produccion podra mas adelante reemplazar esto por un mecanismo operacionalmente
mas fuerte (p.ej. requerir una firma de la wallet, o un flujo con aprobacion
humana adicional) — la funcion `bootstrap_admin()` de abajo es el nucleo
testeable, separado del `main()` de linea de comandos, precisamente para que
ese reemplazo futuro no tenga que rehacer la logica de negocio.
"""

import argparse
import sys

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .chain import EscrowChainAdapter, get_chain_adapter
from .database import SessionLocal
from .models import MemberRole, MembershipDB, MembershipStatus, SecurityEventType, UserDB
from .security.audit import log_security_event


class BootstrapError(Exception):
    pass


def _check_database_migrated(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "alembic_version" not in tables:
        raise BootstrapError(
            "La base de datos no parece estar migrada (falta la tabla alembic_version). "
            "Corre `alembic upgrade head` antes de bootstrap."
        )
    if not {"users", "memberships"} <= tables:
        raise BootstrapError("Faltan tablas requeridas (users/memberships) — corre `alembic upgrade head` primero.")


def bootstrap_admin(db: Session, adapter: EscrowChainAdapter, wallet_address: str) -> UserDB:
    """Nucleo testeable: recibe la sesion de DB y el chain adapter como
    parametros explicitos (nunca los singletons globales directamente) para
    poder probarse contra una DB temporal real, migrada con Alembic de verdad —
    ver tests/test_bootstrap_admin.py."""
    _check_database_migrated(db.get_bind())

    try:
        normalized_wallet = adapter.normalize_address(wallet_address)
    except ValueError as exc:
        raise BootstrapError(f"Wallet invalida: {exc}") from exc

    if db.query(MembershipDB).filter(MembershipDB.role == MemberRole.ADMIN).first() is not None:
        raise BootstrapError(
            "Ya existe al menos un ADMIN en esta base de datos — bootstrap es solo para el PRIMER admin. "
            "Para agregar mas admins usa POST /admin/memberships/{user_id}/role autenticado como un ADMIN existente."
        )

    existing_user = db.query(UserDB).filter(UserDB.wallet_address == normalized_wallet).first()
    if existing_user is not None:
        raise BootstrapError(f"Ya existe un usuario para la wallet {normalized_wallet} — bootstrap nunca sobreescribe silenciosamente.")

    try:
        user = UserDB(wallet_address=normalized_wallet)
        db.add(user)
        db.flush()  # necesitamos user.id antes de crear la membership, misma transaccion

        membership = MembershipDB(user_id=user.id, role=MemberRole.ADMIN, status=MembershipStatus.ACTIVE)
        db.add(membership)

        # evento de auditoria en la MISMA transaccion — si esto falla (p.ej. un
        # CHECK constraint), el rollback deshace TAMBIEN el User y la Membership:
        # nunca puede sobrevivir un admin bootstrapeado sin su evento de auditoria.
        log_security_event(
            db,
            action=SecurityEventType.ADMIN_BOOTSTRAPPED,
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
            reason="initial admin bootstrap via local CLI",
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(user)
    return user


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    """`session_factory` es un punto de inyeccion SOLO para tests (que necesitan
    apuntar a una DB temporal migrada de verdad, distinta del SessionLocal
    global de este proceso) — el uso real de linea de comandos nunca lo pasa,
    y por default usa el SessionLocal real de database.py."""
    parser = argparse.ArgumentParser(
        prog="python -m backend.bootstrap_admin",
        description="Crea el PRIMER usuario ADMIN de Chambeando. Solo puede ejecutarse una vez por base de datos.",
    )
    parser.add_argument(
        "--wallet",
        required=True,
        help="Direccion de wallet TRON del primer admin. NUNCA una clave privada ni una seed phrase — este comando no acepta ninguna de las dos (no existe tal flag).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Obligatorio: confirma explicitamente que queres crear este admin (uso no-interactivo, sin prompt).",
    )
    args = parser.parse_args(argv)

    if not args.confirm:
        print("Falta --confirm. Este comando no procede sin confirmacion explicita.", file=sys.stderr)
        return 2

    factory = session_factory or SessionLocal
    db = factory()
    try:
        user = bootstrap_admin(db, get_chain_adapter(), args.wallet)
        # extraer los valores ANTES de cerrar la sesion: `user` queda detached
        # despues de db.close(), y acceder a sus atributos en ese punto lanza
        # DetachedInstanceError en vez de imprimir nada.
        user_id, wallet_address = user.id, user.wallet_address
    except BootstrapError as exc:
        print(f"Bootstrap rechazado: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    # NUNCA imprimir tokens/secretos/claves de cifrado — este comando no genera
    # ni un JWT ni toca SETTLEMENT_ENCRYPTION_KEY/SECRET_KEY en absoluto. Solo
    # se confirma lo que ya es publico (wallet, id interno).
    print(f"Admin creado: user_id={user_id} wallet={wallet_address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
