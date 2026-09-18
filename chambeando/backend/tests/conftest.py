"""
Fixtures compartidas. IMPORTANTE: las variables de entorno se setean ANTES de
cualquier import de `backend.*` porque `backend.config.Settings()` se instancia
una sola vez, al importar el modulo — si config.py ya se importo con otros
valores, cambiar os.environ despues no tiene efecto.

Todos los tests usan datos sinteticos: wallets falsas, invites falsos, montos
falsos. Ninguna clave real, ningun dato bancario real (ver instrucciones de
Phase 2B, seccion 13).
"""

import os
import tempfile
import uuid

_TMP_DB_DIR = tempfile.mkdtemp(prefix="chambeando-test-")
os.environ.setdefault("SECRET_KEY", "test-only-secret-never-use-in-production-" + uuid.uuid4().hex)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DB_DIR}/unused-module-level.db")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("SETTLEMENT_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.chain import reset_chain_adapter_for_tests, set_chain_adapter_for_tests  # noqa: E402
from backend.database import Base, get_db  # noqa: E402
from backend.main import app  # noqa: E402
from backend.models import MemberRole, MembershipDB, MembershipStatus, UserDB  # noqa: E402
from backend.security.rate_limit import reset_rate_limiter_for_tests  # noqa: E402

from .fake_chain_adapter import FakeChainAdapter, make_valid_signature  # noqa: E402


@pytest.fixture()
def db_session():
    db_path = os.path.join(_TMP_DB_DIR, f"test-{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def fake_chain():
    adapter = FakeChainAdapter()
    set_chain_adapter_for_tests(adapter)
    yield adapter
    reset_chain_adapter_for_tests()


@pytest.fixture()
def client(db_session, fake_chain):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limiter_for_tests()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client: TestClient, wallet_address: str) -> str:
    """Flujo real de auth (nonce + firma) contra el FakeChainAdapter — usado por
    todos los tests que necesitan un JWT valido, no un atajo."""
    nonce_resp = client.post("/auth/nonce", json={"wallet_address": wallet_address})
    assert nonce_resp.status_code == 200, nonce_resp.text
    message = nonce_resp.json()["message"]
    signature = make_valid_signature(wallet_address, message)
    verify_resp = client.post("/auth/verify", json={"wallet_address": wallet_address, "signature": signature})
    assert verify_resp.status_code == 200, verify_resp.text
    return verify_resp.json()["access_token"]


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def sync_indexer(db_session, fake_chain) -> None:
    """Aplica TODOS los eventos pendientes del FakeChainAdapter a `db_session`,
    usando la misma logica de indexer._apply_event que corre en produccion —
    solo que contra la sesion de test en vez del SessionLocal del modulo."""
    from backend.indexer import _apply_event

    for event in fake_chain.get_events(0, fake_chain.current_block()):
        _apply_event(db_session, event)
    db_session.commit()


def seed_membership(db_session, wallet_address: str, role: MemberRole = MemberRole.MEMBER, status: MembershipStatus = MembershipStatus.ACTIVE) -> UserDB:
    """Bootstrap directo de un member/moderator/admin para tests que no estan
    probando el flujo de invites en si. En produccion el PRIMER admin requeriria
    un seed operacional equivalente (fuera de alcance de esta fase) — no existe
    ningun endpoint de API que permita auto-otorgarse ADMIN."""
    user = db_session.query(UserDB).filter(UserDB.wallet_address == wallet_address).first()
    if user is None:
        user = UserDB(wallet_address=wallet_address)
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
    membership = MembershipDB(user_id=user.id, role=role, status=status)
    db_session.add(membership)
    db_session.commit()
    return user
