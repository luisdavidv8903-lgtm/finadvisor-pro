"""multi-network auth identity (network_id on users/auth_nonces)

Revision ID: 9229f715361d
Revises: 8834a2893229
Create Date: 2026-09-25 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9229f715361d'
down_revision: Union[str, Sequence[str], None] = '8834a2893229'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Vinculo de wallet solo-auth (revision 2026-09-25): una identidad real es
    el PAR (network_id, wallet_address), no wallet_address solo -- distintas
    familias de cadena usan formatos de direccion incompatibles, pero
    mezclarlas bajo una unica clave global seguia siendo una decision
    implicita. server_default='tron' rellena TODAS las filas existentes con
    la unica red que existio hasta ahora (la ruta legado), asi que ningun
    usuario/nonce existente cambia de identidad con esta migracion.
    """
    op.add_column('users', sa.Column('network_id', sa.String(), nullable=False, server_default='tron'))
    op.add_column('auth_nonces', sa.Column('network_id', sa.String(), nullable=False, server_default='tron'))

    # La unicidad global de wallet_address deja paso a la unicidad compuesta
    # (network_id, wallet_address) -- mismo patron ya usado en este repo
    # (indices unicos con nombre via op.f(), no UniqueConstraint suelto, ver
    # 9355077ef709_initial_schema.py) para mantenerse portable entre SQLite
    # (usado en tests) y Postgres sin necesitar batch_alter_table.
    op.drop_index(op.f('ix_users_wallet_address'), table_name='users')
    op.create_index(op.f('ix_users_wallet_address'), 'users', ['wallet_address'], unique=False)
    op.create_index('uq_user_network_wallet', 'users', ['network_id', 'wallet_address'], unique=True)
    op.create_index(op.f('ix_users_network_id'), 'users', ['network_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    HALLAZGO DE REVISION (2026-09-25): esta migracion permite deliberadamente
    que la MISMA wallet_address exista en redes distintas (esa es la razon
    de ser de la unicidad compuesta). El downgrade original reinstalaba sin
    mas una unicidad GLOBAL de wallet_address (unique=True, sin network_id)
    -- si dos usuarios de redes distintas llegaran a compartir
    wallet_address (hoy imposible en la practica: TRON usa base58 'T...',
    EVM usa hex '0x...', formatos que nunca coinciden -- pero no
    estructuralmente imposible si una familia de cadena futura compartiera
    formato), crear ese indice unico habria fallado con un IntegrityError
    crudo de SQLite/Postgres, o -- peor, en un dialecto menos estricto --
    podria dejar el indice a medio crear. Ninguna de las dos filas
    conflictivas se borra ni se fusiona jamas de forma automatica aqui: cual
    de las dos identidades es la "real" es una decision de negocio que este
    script no puede tomar. En cambio, el downgrade CHEQUEA explicitamente
    ANTES de tocar ninguna fila y falla con un diagnostico claro, accionable,
    listando exactamente que wallet_address esta duplicado entre que redes
    -- ver el mensaje de RuntimeError abajo para la recuperacion exacta.
    """
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, wallet_address, network_id FROM users")).fetchall()
    by_address: dict[str, list[tuple]] = {}
    for row in rows:
        by_address.setdefault(row[1], []).append((row[0], row[2]))
    conflicts = {addr: entries for addr, entries in by_address.items() if len(entries) > 1}
    if conflicts:
        detail_lines = "\n".join(
            f"  - wallet_address={addr!r}: user_id/network_id = {entries}" for addr, entries in conflicts.items()
        )
        raise RuntimeError(
            "No se puede revertir la migracion 9229f715361d de forma segura: existen usuarios de REDES "
            "DISTINTAS compartiendo el mismo wallet_address. El downgrade reinstala una unicidad GLOBAL de "
            "wallet_address (sin network_id) -- aplicarla ahora operaria sobre datos ambiguos, y esta "
            "migracion NUNCA fusiona ni borra usuarios automaticamente (es una decision de negocio, no "
            "tecnica). RECUPERACION: para cada conflicto listado abajo, un operador humano debe decidir "
            "manualmente cual de los dos usuarios es la identidad a conservar bajo esa wallet_address -- por "
            "ejemplo, reasignando (con SQL manual, fuera de esta migracion) el wallet_address del usuario "
            "secundario a un valor distinto antes de reintentar el downgrade, o eliminandolo a mano si "
            "corresponde por conocimiento de negocio (nunca automaticamente). Conflictos encontrados:\n"
            + detail_lines
        )

    op.drop_index(op.f('ix_users_network_id'), table_name='users')
    op.drop_index('uq_user_network_wallet', table_name='users')
    op.drop_index(op.f('ix_users_wallet_address'), table_name='users')
    op.create_index(op.f('ix_users_wallet_address'), 'users', ['wallet_address'], unique=True)
    op.drop_column('auth_nonces', 'network_id')
    op.drop_column('users', 'network_id')
