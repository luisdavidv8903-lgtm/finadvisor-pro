"""
Phase 2C section 14 — "At minimum the critical complete trade path must pass
on PostgreSQL." This mirrors the critical scenarios from test_e2e_full_flow.py
(onboarding, the full happy-path trade, the dispute->refund path, and one
cross-cutting privacy check) against a REAL local PostgreSQL 17.11 instance
instead of SQLite. Skips cleanly if POSTGRES_TEST_DATABASE_URL is not set —
same convention as test_postgres_validation.py.

Not a duplicate of every SQLite-only test in test_e2e_full_flow.py: the
exhaustive privacy/invalid-transition/indexer-convergence matrix is dialect-
independent application logic already proven there and in
test_postgres_validation.py's own dialect-parity tests; re-running all of it
against Postgres would test SQLAlchemy, not new backend behavior.
"""
import os
import pathlib

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

POSTGRES_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="POSTGRES_TEST_DATABASE_URL not set — Phase 2C PostgreSQL E2E skipped"
)

tronpy = pytest.importorskip("tronpy", reason="tronpy not installed in this environment")

from tronpy.keys import PrivateKey  # noqa: E402

from backend.bootstrap_admin import bootstrap_admin  # noqa: E402
from backend.chain import reset_chain_adapter_for_tests, set_chain_adapter_for_tests  # noqa: E402
from backend.chain.tron_adapter import verify_tron_signature  # noqa: E402
from backend.database import Base, get_db  # noqa: E402
from backend.indexer import _apply_event  # noqa: E402
from backend.main import app  # noqa: E402
from backend.models import OrderStatus, P2POrderDB  # noqa: E402
from backend.security.rate_limit import reset_rate_limiter_for_tests  # noqa: E402

from .fake_chain_adapter import FakeChainAdapter  # noqa: E402
from .test_e2e_full_flow import SYNTHETIC_ACCOUNT_REFERENCE, SYNTHETIC_PAYMENT_METHOD  # noqa: E402

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent


class SyntheticChainAdapter(FakeChainAdapter):
    def verify_wallet_signature(self, address: str, message: str, signature: str) -> bool:
        return verify_tron_signature(address, message, signature)


def make_wallet():
    pk = PrivateKey.random()
    return pk, pk.public_key.to_base58check_address()


def sign(pk, message: str) -> str:
    return pk.sign_msg(message.encode()).hex()


def login(client, pk, address: str) -> str:
    nonce_resp = client.post("/auth/nonce", json={"wallet_address": address})
    assert nonce_resp.status_code == 200, nonce_resp.text
    message = nonce_resp.json()["message"]
    signature = sign(pk, message)
    verify_resp = client.post("/auth/verify", json={"wallet_address": address, "signature": signature})
    assert verify_resp.status_code == 200, verify_resp.text
    return verify_resp.json()["access_token"]


def hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def apply_all_events(db, chain) -> None:
    for event in chain.get_events(0, chain.current_block()):
        _apply_event(db, event)
    db.commit()


@pytest.fixture(scope="module")
def pg_engine():
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = POSTGRES_URL
    command.upgrade(cfg, "head")

    engine = create_engine(POSTGRES_URL)
    yield engine
    engine.dispose()
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous


@pytest.fixture()
def pg_e2e(pg_engine):
    """TRUNCATE-isolated real-Postgres TestClient, same wiring pattern as
    test_e2e_full_flow.py's e2e_env but against the already-migrated
    chambeando_test database instead of a throwaway SQLite file."""
    from types import SimpleNamespace

    Session = sessionmaker(bind=pg_engine, autocommit=False, autoflush=False)
    session = Session()
    table_names = [t.name for t in Base.metadata.sorted_tables]
    session.execute(text(f'TRUNCATE TABLE {", ".join(table_names)} RESTART IDENTITY CASCADE'))
    session.commit()

    adapter = SyntheticChainAdapter()
    set_chain_adapter_for_tests(adapter)

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limiter_for_tests()

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, db=session, chain=adapter)

    app.dependency_overrides.clear()
    reset_chain_adapter_for_tests()
    session.rollback()
    session.close()


def _onboard(pg_e2e):
    client, db = pg_e2e.client, pg_e2e.db
    admin_pk, admin_wallet = make_wallet()
    bootstrap_admin(db, pg_e2e.chain, admin_wallet)
    admin_token = login(client, admin_pk, admin_wallet)

    def create_invite() -> str:
        r = client.post("/admin/invites", json={"max_uses": 1, "expires_in_hours": 72}, headers=hdr(admin_token))
        assert r.status_code == 200, r.text
        return r.json()["code"]

    seller_pk, seller_wallet = make_wallet()
    buyer_pk, buyer_wallet = make_wallet()
    third_pk, third_wallet = make_wallet()

    seller_token = login(client, seller_pk, seller_wallet)
    assert client.post("/invites/redeem", json={"code": create_invite()}, headers=hdr(seller_token)).status_code == 200
    buyer_token = login(client, buyer_pk, buyer_wallet)
    assert client.post("/invites/redeem", json={"code": create_invite()}, headers=hdr(buyer_token)).status_code == 200
    third_token = login(client, third_pk, third_wallet)
    assert client.post("/invites/redeem", json={"code": create_invite()}, headers=hdr(third_token)).status_code == 200

    from types import SimpleNamespace

    return SimpleNamespace(
        admin_token=admin_token,
        seller=SimpleNamespace(wallet=seller_wallet, token=seller_token),
        buyer=SimpleNamespace(wallet=buyer_wallet, token=buyer_token),
        third_party=SimpleNamespace(token=third_token),
        create_invite=create_invite,
    )


def test_pg_happy_path_trade_full_lifecycle(pg_e2e):
    """POSTGRES_VERIFIED: OPEN -> metadata+snapshot -> CLAIMED -> reveal ->
    PAID -> RELEASED -> reputation, against real PostgreSQL 17.11."""
    client, env = pg_e2e.client, pg_e2e
    people = _onboard(pg_e2e)

    detail = client.post(
        "/settlement-details",
        json={
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "currency": "CUP",
            "payload": {"recipient": "SYNTHETIC SELLER", "account_reference": SYNTHETIC_ACCOUNT_REFERENCE},
        },
        headers=hdr(people.seller.token),
    )
    assert detail.status_code == 201, detail.text
    detail_id = detail.json()["id"]

    onchain_id = 8001
    env.chain.seed_order_created(onchain_id, people.seller.wallet, "TFakeTokenAddress0000001", 100_000000)
    apply_all_events(env.db, env.chain)
    order = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()
    assert order.onchain_status == OrderStatus.OPEN

    meta = client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": onchain_id,
            "fiat_amount": "100.00",
            "fiat_currency": "CUP",
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "settlement_detail_id": detail_id,
        },
        headers=hdr(people.seller.token),
    )
    assert meta.status_code == 201, meta.text

    env.chain.seed_order_claimed(onchain_id, people.buyer.wallet, arbiter_snapshot="TFakeArbiterWallet00000001")
    apply_all_events(env.db, env.chain)

    reveal = client.get(f"/orders/{order.id}/settlement", headers=hdr(people.buyer.token))
    assert reveal.status_code == 200
    assert reveal.json()["payload"]["account_reference"] == SYNTHETIC_ACCOUNT_REFERENCE

    third_party_reveal = client.get(f"/orders/{order.id}/settlement", headers=hdr(people.third_party.token))
    assert third_party_reveal.status_code == 403

    env.chain.seed_paid(onchain_id)
    apply_all_events(env.db, env.chain)
    env.chain.seed_settled(onchain_id, "RELEASED")
    apply_all_events(env.db, env.chain)

    final = client.get(f"/orders/{order.id}", headers=hdr(people.seller.token)).json()
    assert final["onchain_status"] == "released"

    seller_user_id = env.db.query(P2POrderDB).filter(P2POrderDB.id == order.id).first().created_by_user_id
    rep = client.get(f"/reputation/{seller_user_id}", headers=hdr(people.seller.token)).json()
    assert rep["completed_trades"] == 1


def test_pg_dispute_to_refund_path(pg_e2e):
    """POSTGRES_VERIFIED: OPEN -> CLAIMED -> PAID -> DISPUTED -> REFUNDED,
    evidence submission/visibility, and post-finalization settlement
    protection, against real PostgreSQL 17.11."""
    client, env = pg_e2e.client, pg_e2e
    people = _onboard(pg_e2e)

    detail = client.post(
        "/settlement-details",
        json={"payment_method": SYNTHETIC_PAYMENT_METHOD, "currency": "CUP", "payload": {"account_reference": SYNTHETIC_ACCOUNT_REFERENCE}},
        headers=hdr(people.seller.token),
    ).json()

    onchain_id = 8002
    env.chain.seed_order_created(onchain_id, people.seller.wallet, "TFakeTokenAddress0000002", 50_000000)
    apply_all_events(env.db, env.chain)
    order = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()

    client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": onchain_id,
            "fiat_amount": "50.00",
            "fiat_currency": "CUP",
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "settlement_detail_id": detail["id"],
        },
        headers=hdr(people.seller.token),
    )

    env.chain.seed_order_claimed(onchain_id, people.buyer.wallet, arbiter_snapshot="TFakeArbiterWallet00000002")
    apply_all_events(env.db, env.chain)
    env.chain.seed_paid(onchain_id)
    apply_all_events(env.db, env.chain)
    env.chain.seed_disputed(onchain_id)
    apply_all_events(env.db, env.chain)

    ev = client.post(
        "/disputes/evidence",
        json={"order_id": order.id, "evidence_type": "note", "note": "synthetic: goods not delivered"},
        headers=hdr(people.buyer.token),
    )
    assert ev.status_code == 201

    unrelated = client.get(f"/disputes/{order.id}/evidence", headers=hdr(people.third_party.token))
    assert unrelated.status_code == 403

    party_view = client.get(f"/disputes/{order.id}/evidence", headers=hdr(people.seller.token))
    assert party_view.status_code == 200

    env.chain.seed_settled(onchain_id, "REFUNDED")
    apply_all_events(env.db, env.chain)

    final = env.db.query(P2POrderDB).filter(P2POrderDB.id == order.id).first()
    assert final.onchain_status == OrderStatus.REFUNDED

    # settlement stays protected after finalization
    assert client.get(f"/orders/{order.id}/settlement", headers=hdr(people.third_party.token)).status_code == 403
    assert client.get(f"/orders/{order.id}/settlement", headers=hdr(people.seller.token)).status_code == 200

    seller_rep = client.get(f"/reputation/{final.created_by_user_id}", headers=hdr(people.seller.token)).json()
    assert seller_rep["disputes_won"] == 1
