import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# chambeando/backend/alembic/env.py -> necesitamos chambeando/ en sys.path para
# poder importar el paquete `backend` (mismo layout que pytest.ini: pythonpath = .)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from backend.config import settings  # noqa: E402
from backend.database import Base  # noqa: E402
from backend import models  # noqa: E402  (registra todas las tablas en Base.metadata)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# La URL de conexion viene SIEMPRE de una variable de entorno, nunca de un
# valor estatico en alembic.ini — mismo principio que "nunca hardcodear
# secretos/config en el repo" del resto del backend. Se lee DATABASE_URL
# directo de os.environ (no de `settings.DATABASE_URL`, que es un singleton
# cacheado al importar backend.config una sola vez) para que los tests de
# migracion puedan apuntar alembic a un archivo sqlite temporal distinto del
# de la app sin reiniciar el proceso — env.py se re-ejecuta en cada invocacion
# de un comando de alembic, asi que SI relee os.environ cada vez.
import os as _os

config.set_main_option("sqlalchemy.url", _os.environ.get("DATABASE_URL", settings.DATABASE_URL))

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
