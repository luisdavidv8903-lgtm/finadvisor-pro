"""
Phase 2B.2 #2 — first-admin bootstrap CLI. Uses a REAL Alembic-migrated temp
database (same pattern as test_migrations.py), not the fast create_all()
fixture, because bootstrap_admin() itself checks for a real migration marker
(`alembic_version`) as its first precondition.
"""

import io
import contextlib
import pathlib

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.bootstrap_admin import BootstrapError, bootstrap_admin, main
from backend.chain import reset_chain_adapter_for_tests, set_chain_adapter_for_tests
from backend.models import MemberRole, MembershipDB, UserDB

from .fake_chain_adapter import FakeChainAdapter


@pytest.fixture(autouse=True)
def _fake_adapter_for_cli_tests():
    """main() llama get_chain_adapter() internamente — inyectamos el fake ANTES
    para que nunca intente construir un TronEscrowAdapter real (que requiere
    red/contrato configurado, no disponible ni deseado en tests unitarios)."""
    set_chain_adapter_for_tests(FakeChainAdapter())
    yield
    reset_chain_adapter_for_tests()

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bootstrap_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def unmigrated_db(tmp_path):
    """Una DB sqlite que existe pero jamas paso por Alembic — sin tabla
    alembic_version. Simula "corriste bootstrap antes de migrar"."""
    from backend.database import Base

    db_path = tmp_path / "unmigrated.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)  # crea tablas pero NO alembic_version
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def test_database_not_migrated_is_rejected(unmigrated_db):
    adapter = FakeChainAdapter()
    with pytest.raises(BootstrapError, match="alembic_version"):
        bootstrap_admin(unmigrated_db, adapter, "TFakeBootstrapAdmin000000000001")


def test_first_bootstrap_succeeds(migrated_db):
    adapter = FakeChainAdapter()
    user = bootstrap_admin(migrated_db, adapter, "TFakeBootstrapAdmin000000000001")
    assert user.wallet_address == "TFakeBootstrapAdmin000000000001"

    membership = migrated_db.query(MembershipDB).filter(MembershipDB.user_id == user.id).first()
    assert membership.role == MemberRole.ADMIN
    assert membership.status.value == "active"


def test_second_bootstrap_attempt_denied(migrated_db):
    adapter = FakeChainAdapter()
    bootstrap_admin(migrated_db, adapter, "TFakeBootstrapAdmin000000000001")

    with pytest.raises(BootstrapError, match="Ya existe al menos un ADMIN"):
        bootstrap_admin(migrated_db, adapter, "TFakeBootstrapAdminTwo00000000001")

    # confirmar que NO se creo un segundo admin ni un segundo usuario
    assert migrated_db.query(MembershipDB).filter(MembershipDB.role == MemberRole.ADMIN).count() == 1
    assert migrated_db.query(UserDB).filter(UserDB.wallet_address == "TFakeBootstrapAdminTwo00000000001").first() is None


def test_existing_member_not_silently_elevated(migrated_db):
    """Una wallet que ya tiene User (y opcionalmente Membership) NO se
    "actualiza" a ADMIN silenciosamente — se rechaza el bootstrap entero."""
    adapter = FakeChainAdapter()
    existing = UserDB(wallet_address="TFakeExistingMember0000000000001")
    migrated_db.add(existing)
    migrated_db.commit()

    with pytest.raises(BootstrapError, match="Ya existe un usuario"):
        bootstrap_admin(migrated_db, adapter, "TFakeExistingMember0000000000001")

    # nunca se le creo membership de ADMIN
    assert migrated_db.query(MembershipDB).filter(MembershipDB.user_id == existing.id).first() is None


def test_malformed_wallet_denied(migrated_db):
    adapter = FakeChainAdapter()
    for bad in ["not-a-wallet", "", "T123", "0xAbCdEf0000000000000000000000000000", "T" + "!" * 33]:
        with pytest.raises(BootstrapError, match="Wallet invalida"):
            bootstrap_admin(migrated_db, adapter, bad)

    assert migrated_db.query(UserDB).count() == 0


def test_no_secrets_printed_by_cli(monkeypatch, tmp_path):
    """Corre main() de verdad (linea de comandos completa) y captura stdout —
    debe confirmar la creacion sin imprimir tokens/claves. bootstrap_admin no
    genera JWT en absoluto, asi que esto tambien confirma esa propiedad
    estructuralmente (no hay 'access_token' en la salida). Se inyecta
    session_factory apuntando a la DB temporal migrada de este test — el
    SessionLocal global del proceso esta fijo al placeholder de conftest.py y
    no se puede redirigir mutando DATABASE_URL a esta altura."""
    db_path = tmp_path / "cli_test.db"
    monkeypatch.setenv("SECRET_KEY", "super-secret-jwt-signing-key-should-never-print")
    monkeypatch.setenv("SETTLEMENT_ENCRYPTION_KEY", "super-secret-fernet-key-should-never-print")

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = main(["--wallet", "TFakeCliBootstrap0000000000001", "--confirm"], session_factory=session_factory)

    assert exit_code == 0
    output = stdout.getvalue() + stderr.getvalue()
    assert "super-secret-jwt-signing-key-should-never-print" not in output
    assert "super-secret-fernet-key-should-never-print" not in output
    assert "access_token" not in output
    assert "TFakeCliBootstrap0000000000001" in output  # la wallet SI es esperable que aparezca (no es secreta)


def test_cli_refuses_without_confirm_flag(tmp_path, monkeypatch):
    db_path = tmp_path / "no_confirm_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("SECRET_KEY", "x")
    monkeypatch.setenv("SETTLEMENT_ENCRYPTION_KEY", "x")

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        exit_code = main(["--wallet", "TFakeNoConfirm00000000000000001"])

    assert exit_code == 2
    assert "confirm" in stderr.getvalue().lower()
    assert not db_path.exists()  # ni siquiera se toco la DB


def test_bootstrap_admin_has_no_cli_flags_for_private_key_or_seed_phrase():
    """Prueba estructural: no existe NINGUN argumento --private-key/--seed/etc.
    en el parser — es imposible pasarle una clave privada o seed phrase por
    diseno, no por validacion."""
    import backend.bootstrap_admin as mod
    import inspect

    source = inspect.getsource(mod)
    for forbidden in ("private-key", "private_key", "seed-phrase", "seed_phrase", "mnemonic"):
        assert forbidden not in source.lower()
