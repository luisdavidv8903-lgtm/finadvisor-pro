from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    """Habilita PRAGMA foreign_keys=ON en TODA conexion SQLite de este proceso —
    no solo el engine de la app (`engine` mas abajo), tambien los que crean los
    tests y Alembic internamente, porque este listener esta registrado en la
    clase Engine, no en una instancia. SQLite trae foreign_keys=OFF por defecto
    por compatibilidad historica; sin esto, las ForeignKey declaradas en
    models.py son puramente documentales en SQLite — no rechazan nada en
    tiempo de ejecucion (ver ENFORCEMENT_LEVELS.md, gap identificado en
    Phase 2B.1, cerrado aqui en Phase 2B.2). No aplica a Postgres (siempre
    enforced ahi) — el chequeo de modulo evita tocar conexiones no-sqlite3."""
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
