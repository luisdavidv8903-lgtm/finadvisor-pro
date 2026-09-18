"""
Separacion chain-agnostic (seccion 5). Prueba tanto en tiempo de ejecucion
(la app entera ya corre 100% de sus tests contra FakeChainAdapter, sin tronpy
instalado siquiera) como estaticamente (ningun archivo fuera de chain/tron_adapter.py
importa tronpy)."""

import ast
import pathlib

from backend.models import MemberRole, OrderStatus, P2POrderDB
from backend.chain.adapter import EscrowChainAdapter

from .conftest import auth_headers, login, seed_membership, sync_indexer

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _files_importing_tronpy() -> list[pathlib.Path]:
    offenders = []
    for path in BACKEND_ROOT.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(n.name.split(".")[0] == "tronpy" for n in node.names):
                offenders.append(path)
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "tronpy":
                offenders.append(path)
    return offenders


def test_no_tronpy_imports_outside_tron_adapter():
    """La regla es sobre logica de NEGOCIO, no sobre el propio test suite:
    tests/test_tron_signature_real.py (Phase 2B.1 #6) importa tronpy real a
    proposito para probar la criptografia real contra vectores sinteticos — eso
    es exactamente lo que ese archivo existe para hacer, no una violacion de la
    separacion chain-agnostic del backend en si."""
    offenders = _files_importing_tronpy()
    allowed = {
        BACKEND_ROOT / "chain" / "tron_adapter.py",
        BACKEND_ROOT / "tests" / "test_tron_signature_real.py",
    }
    unexpected = [p for p in offenders if p not in allowed]
    assert unexpected == [], f"tronpy imported outside the Tron adapter (or the real-crypto test): {unexpected}"


def test_tron_adapter_itself_does_import_tronpy_sanity_check():
    # confirma que el test anterior realmente estaria detectando algo si existiera
    # (evita un falso-positivo de "no hay nadie usando tronpy en absoluto")
    assert BACKEND_ROOT / "chain" / "tron_adapter.py" in _files_importing_tronpy()


def test_business_service_runs_against_mocked_adapter_without_a_real_chain(client, db_session, fake_chain):
    """Toda la suite ya prueba esto implicitamente (ningun test tiene un nodo TRON
    real), pero este test lo deja explicito: se puede levantar la app entera,
    autenticar, y correr un flujo completo de orden usando solo FakeChainAdapter."""
    fake_chain.seed_order_created(1, "TFakeSellerChain0000000000001", "FAKE_TOKEN", 50_000000)
    fake_chain.seed_order_claimed(1, "TFakeBuyerChain00000000000001", "TFakeArbiterChain0000000001")
    fake_chain.seed_paid(1)
    fake_chain.seed_settled(1, "RELEASED")
    sync_indexer(db_session, fake_chain)

    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()
    assert order.onchain_status == OrderStatus.RELEASED

    seed_membership(db_session, "TFakeViewerChain00000000000001", role=MemberRole.MEMBER)
    token = login(client, "TFakeViewerChain00000000000001")
    r = client.get(f"/orders/{order.id}", headers=auth_headers(token))
    assert r.status_code == 200
    assert r.json()["onchain_status"] == "released"


def test_indexer_is_sole_writer_no_api_endpoint_accepts_onchain_status(client, db_session, fake_chain):
    """Prueba negativa: ningun schema de request en todo el backend acepta un campo
    `onchain_status` (ni `arbiter_snapshot_wallet`, ni `was_disputed`) — ver
    schemas.py, todos son solo-lectura (response) o simplemente no existen como
    campo de entrada."""
    import inspect

    from backend import schemas

    for name, obj in vars(schemas).items():
        if not inspect.isclass(obj) or not hasattr(obj, "model_fields"):
            continue
        if "Create" in name or "Request" in name:
            assert "onchain_status" not in obj.model_fields, f"{name} debe ser puramente controlado por el indexer"
            assert "arbiter_snapshot_wallet" not in obj.model_fields
            assert "was_disputed" not in obj.model_fields


def test_api_cannot_submit_arbitrary_onchain_status_via_metadata_endpoint(client, db_session, fake_chain):
    fake_chain.seed_order_created(1, "TFakeSeller3000000000000000001", "FAKE_TOKEN", 10_000000)
    sync_indexer(db_session, fake_chain)
    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()
    assert order.onchain_status == OrderStatus.OPEN

    seed_membership(db_session, "TFakeSeller3000000000000000001", role=MemberRole.MEMBER)
    token = login(client, "TFakeSeller3000000000000000001")

    # el body de /orders/metadata ni siquiera TIENE un campo onchain_status —
    # intentar colarlo en el JSON simplemente se ignora (Pydantic descarta
    # campos no declarados por defecto).
    r = client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": 1,
            "fiat_amount": "10.00",
            "fiat_currency": "CUP",
            "payment_method": "Transferencia CUP",
            "onchain_status": "released",  # intento de inyeccion — debe ser ignorado
        },
        headers=auth_headers(token),
    )
    assert r.status_code == 201
    db_session.refresh(order)
    assert order.onchain_status == OrderStatus.OPEN  # sigue OPEN — el indexer nunca corrio un release real


def test_fake_adapter_reproduces_full_order_lifecycle_without_tron_specific_logic(db_session, fake_chain):
    """El FakeChainAdapter reproduce el flujo completo (create/claim/paid/dispute/
    settle) usando SOLO la interfaz EscrowChainAdapter — cero conocimiento de TRON."""
    assert isinstance(fake_chain, EscrowChainAdapter)
    fake_chain.seed_order_created(1, "TSeller", "TOKEN", 1000)
    fake_chain.seed_order_claimed(1, "TBuyer", "TArbiter")
    fake_chain.seed_paid(1)
    fake_chain.seed_disputed(1)
    fake_chain.seed_settled(1, "REFUNDED")
    sync_indexer(db_session, fake_chain)

    order = db_session.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 1).first()
    assert order.onchain_status == OrderStatus.REFUNDED
    assert order.was_disputed is True
    assert order.arbiter_snapshot_wallet == "TArbiter"
