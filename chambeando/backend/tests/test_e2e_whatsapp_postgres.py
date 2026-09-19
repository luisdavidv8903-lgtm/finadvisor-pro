"""
Phase 2C section 14 (applied to Phase 2C's WhatsApp addendum) — the critical
WhatsApp path (link -> join -> SELL/BUY -> full trade -> RELEASED) against
real PostgreSQL 17.11, not just SQLite. Same TRUNCATE-isolated pattern as
test_postgres_validation.py / test_e2e_postgres.py.
"""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

POSTGRES_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="POSTGRES_TEST_DATABASE_URL not set — WhatsApp PostgreSQL E2E skipped")

from backend.database import Base, get_db  # noqa: E402
from backend.indexer import _apply_event  # noqa: E402
from backend.main import app  # noqa: E402
from backend.messaging import ConversationRouter, WhatsAppAdapter, reset_conversation_router_for_tests, set_conversation_router_for_tests  # noqa: E402
from backend.messaging.whatsapp_adapter import SandboxMetaClient  # noqa: E402
from backend.models import MemberRole, OrderStatus, P2POrderDB  # noqa: E402
from backend.security.rate_limit import reset_rate_limiter_for_tests  # noqa: E402

from .conftest import auth_headers, login, seed_membership  # noqa: E402
from .test_whatsapp_conversation_flow import SYNTHETIC_ACCOUNT_REFERENCE, SYNTHETIC_PAYMENT_METHOD, _w, link_wallet  # noqa: E402

BACKEND_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def pg_engine():
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
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
def pg_wa(pg_engine):
    from types import SimpleNamespace

    Session = sessionmaker(bind=pg_engine, autocommit=False, autoflush=False)
    session = Session()
    table_names = [t.name for t in Base.metadata.sorted_tables]
    session.execute(text(f'TRUNCATE TABLE {", ".join(table_names)} RESTART IDENTITY CASCADE'))
    session.commit()

    from backend.chain import reset_chain_adapter_for_tests, set_chain_adapter_for_tests

    from .fake_chain_adapter import FakeChainAdapter

    chain = FakeChainAdapter()
    set_chain_adapter_for_tests(chain)

    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock))
    set_conversation_router_for_tests(router)

    app.dependency_overrides[get_db] = lambda: (yield session)
    reset_rate_limiter_for_tests()

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, db=session, chain=chain, router=router, mock=mock)

    app.dependency_overrides.clear()
    reset_chain_adapter_for_tests()
    reset_conversation_router_for_tests()
    session.rollback()
    session.close()


def _apply_all(env) -> None:
    for event in env.chain.get_events(0, env.chain.current_block()):
        _apply_event(env.db, event)
    env.db.commit()


def test_pg_whatsapp_link_join_and_full_trade(pg_wa):
    """POSTGRES_VERIFIED: WhatsAppLinkDB linking, invite redemption via the
    conversation router, SELL/BUY flows, and the full OPEN->RELEASED cycle,
    all against real PostgreSQL 17.11."""
    env = pg_wa
    client = env.client

    admin_wallet = _w("0xE2EPgWaAdmin1")
    seed_membership(env.db, admin_wallet, role=MemberRole.ADMIN)
    admin_token = login(client, admin_wallet)

    def new_invite() -> str:
        return client.post("/admin/invites", json={"max_uses": 1, "expires_in_hours": 24}, headers=auth_headers(admin_token)).json()["code"]

    seller_wallet, buyer_wallet = _w("0xE2EPgWaSeller1"), _w("0xE2EPgWaBuyer1")
    seller_token = link_wallet(client, env.db, env.router, env.mock, "wa-pg-seller-1", seller_wallet)
    env.router.handle_inbound(env.db, "wa-pg-seller-1", new_invite())
    buyer_token = link_wallet(client, env.db, env.router, env.mock, "wa-pg-buyer-1", buyer_wallet)
    env.router.handle_inbound(env.db, "wa-pg-buyer-1", new_invite())

    onchain_id = 6001
    env.chain.seed_order_created(onchain_id, seller_wallet, "TFakePgWaToken001", 60_000000)
    _apply_all(env)
    order = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()

    detail = client.post(
        "/settlement-details",
        json={"payment_method": SYNTHETIC_PAYMENT_METHOD, "currency": "CUP", "payload": {"account_reference": SYNTHETIC_ACCOUNT_REFERENCE}},
        headers=auth_headers(seller_token),
    ).json()
    client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": onchain_id,
            "fiat_amount": "60.00",
            "fiat_currency": "CUP",
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "settlement_detail_id": detail["id"],
        },
        headers=auth_headers(seller_token),
    )

    env.router.handle_inbound(env.db, "wa-pg-buyer-1", "BUY")
    assert "cripto" in env.mock.sent[-1][1].lower()

    env.chain.seed_order_claimed(onchain_id, buyer_wallet, arbiter_snapshot="TFakePgWaArbiter001")
    _apply_all(env)
    reveal = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(buyer_token))
    assert reveal.status_code == 200

    env.chain.seed_paid(onchain_id)
    _apply_all(env)
    env.chain.seed_settled(onchain_id, "RELEASED")
    _apply_all(env)

    final = env.db.query(P2POrderDB).filter(P2POrderDB.id == order.id).first()
    assert final.onchain_status == OrderStatus.RELEASED
