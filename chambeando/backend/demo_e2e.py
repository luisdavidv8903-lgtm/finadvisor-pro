"""
Phase 2C — local reusable synthetic demo. Runs one happy-path trade and one
dispute/refund scenario through the REAL FastAPI routes (same architecture as
test_e2e_full_flow.py), against a throwaway local SQLite database created
just for this run, and prints ONLY safe state transitions.

Uses exclusively: synthetic ephemeral TRON keypairs, synthetic fake payment
data, and the local FakeChainAdapter-based synthetic chain adapter. Never
touches testnet/mainnet, never prints a private key, password, raw settlement
detail, or DB credential.

Usage:
    python -m backend.demo_e2e
"""
import atexit
import os
import pathlib
import tempfile

_DB_DIR = tempfile.mkdtemp(prefix="chambeando-demo-")
_DB_PATH = os.path.join(_DB_DIR, "demo.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_PATH}")
os.environ.setdefault("SECRET_KEY", "demo-only-secret-never-use-in-production")
os.environ.setdefault("SETTLEMENT_ENCRYPTION_KEY", __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode())

from tronpy.keys import PrivateKey  # noqa: E402

from backend.bootstrap_admin import bootstrap_admin  # noqa: E402
from backend.chain import set_chain_adapter_for_tests  # noqa: E402
from backend.chain.tron_adapter import verify_tron_signature  # noqa: E402
from backend.indexer import _apply_event  # noqa: E402

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
SYNTHETIC_PAYMENT_METHOD = "TEST_CUP_TRANSFER"
SYNTHETIC_ACCOUNT_REFERENCE = "TEST-000000"


def _make_demo_chain_adapter():
    """Same on-chain state machine every backend test uses (FakeChainAdapter),
    imported lazily so this script's env vars are set before backend.database
    ever gets imported (SessionLocal is built at import time). Only
    verify_wallet_signature is swapped for real tronpy ECDSA verification."""
    from backend.tests.fake_chain_adapter import FakeChainAdapter

    class _Adapter(FakeChainAdapter):
        def verify_wallet_signature(self, address, message, signature):
            return verify_tron_signature(address, message, signature)

    return _Adapter()


def make_wallet():
    pk = PrivateKey.random()
    return pk, pk.public_key.to_base58check_address()


def sign(pk, message: str) -> str:
    return pk.sign_msg(message.encode()).hex()


def login(client, pk, address: str) -> str:
    nonce = client.post("/auth/nonce", json={"wallet_address": address}).json()["message"]
    signature = sign(pk, nonce)
    resp = client.post("/auth/verify", json={"wallet_address": address, "signature": signature})
    return resp.json()["access_token"]


def hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def apply_events(db, chain) -> None:
    for event in chain.get_events(0, chain.current_block()):
        _apply_event(db, event)
    db.commit()


def run_demo() -> None:
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.database import get_db
    from backend.main import app
    from backend.models import OrderStatus, P2POrderDB

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{_DB_PATH}", connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = Session()

    chain = _make_demo_chain_adapter()
    set_chain_adapter_for_tests(chain)

    app.dependency_overrides[get_db] = lambda: (yield db)
    client = TestClient(app)

    print("=== CHAMBEANDO -- SYNTHETIC LOCAL DEMO (Phase 2C) ===")
    print("(all wallets/payment data below are synthetic and local-only)\n")

    # --- onboarding ---
    admin_pk, admin_wallet = make_wallet()
    bootstrap_admin(db, chain, admin_wallet)
    admin_token = login(client, admin_pk, admin_wallet)
    print("ADMIN = BOOTSTRAPPED")

    def new_invite() -> str:
        r = client.post("/admin/invites", json={"max_uses": 1, "expires_in_hours": 24}, headers=hdr(admin_token))
        return r.json()["code"]

    seller_pk, seller_wallet = make_wallet()
    buyer_pk, buyer_wallet = make_wallet()

    seller_token = login(client, seller_pk, seller_wallet)
    client.post("/invites/redeem", json={"code": new_invite()}, headers=hdr(seller_token))
    print("SELLER MEMBER = ACTIVE")

    buyer_token = login(client, buyer_pk, buyer_wallet)
    client.post("/invites/redeem", json={"code": new_invite()}, headers=hdr(buyer_token))
    print("BUYER MEMBER = ACTIVE")

    # --- scenario 1: happy path ---
    detail = client.post(
        "/settlement-details",
        json={
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "currency": "CUP",
            "payload": {"recipient": "SYNTHETIC SELLER", "account_reference": SYNTHETIC_ACCOUNT_REFERENCE},
        },
        headers=hdr(seller_token),
    ).json()

    onchain_id = 1
    chain.seed_order_created(onchain_id, seller_wallet, "TFakeTokenAddress0000001", 100_000000)
    apply_events(db, chain)
    print("ORDER = OPEN")

    order = db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()
    client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": onchain_id,
            "fiat_amount": "100.00",
            "fiat_currency": "CUP",
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "settlement_detail_id": detail["id"],
        },
        headers=hdr(seller_token),
    )

    chain.seed_order_claimed(onchain_id, buyer_wallet, arbiter_snapshot="TFakeArbiterWallet00000001")
    apply_events(db, chain)
    print("ORDER = CLAIMED")

    chain.seed_paid(onchain_id)
    apply_events(db, chain)
    print("PAYMENT = MARKED_PAID")

    chain.seed_settled(onchain_id, "RELEASED")
    apply_events(db, chain)
    print("ORDER = RELEASED")

    seller_user_id = db.query(P2POrderDB).filter(P2POrderDB.id == order.id).first().created_by_user_id
    client.get(f"/reputation/{seller_user_id}", headers=hdr(seller_token))
    print("REPUTATION = UPDATED\n")

    # --- scenario 2: dispute -> refund ---
    detail2 = client.post(
        "/settlement-details",
        json={"payment_method": SYNTHETIC_PAYMENT_METHOD, "currency": "CUP", "payload": {"account_reference": SYNTHETIC_ACCOUNT_REFERENCE}},
        headers=hdr(seller_token),
    ).json()

    onchain_id_2 = 2
    chain.seed_order_created(onchain_id_2, seller_wallet, "TFakeTokenAddress0000002", 50_000000)
    apply_events(db, chain)
    print("ORDER_2 = OPEN")

    order2 = db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id_2).first()
    client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": onchain_id_2,
            "fiat_amount": "50.00",
            "fiat_currency": "CUP",
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "settlement_detail_id": detail2["id"],
        },
        headers=hdr(seller_token),
    )

    chain.seed_order_claimed(onchain_id_2, buyer_wallet, arbiter_snapshot="TFakeArbiterWallet00000002")
    apply_events(db, chain)
    print("ORDER_2 = CLAIMED")

    chain.seed_paid(onchain_id_2)
    apply_events(db, chain)
    print("ORDER_2 PAYMENT = MARKED_PAID")

    chain.seed_disputed(onchain_id_2)
    apply_events(db, chain)
    print("ORDER_2 = DISPUTED")

    client.post(
        "/disputes/evidence",
        json={"order_id": order2.id, "evidence_type": "note", "note": "synthetic: goods not delivered"},
        headers=hdr(buyer_token),
    )
    print("ORDER_2 EVIDENCE = SUBMITTED")

    chain.seed_settled(onchain_id_2, "REFUNDED")
    apply_events(db, chain)
    print("ORDER_2 = REFUNDED")

    seller_rep = client.get(f"/reputation/{order2.created_by_user_id}", headers=hdr(seller_token)).json()
    print(f"ORDER_2 REPUTATION = UPDATED (disputes_won={seller_rep['disputes_won']})")

    db.close()
    engine.dispose()
    print("\n=== DEMO COMPLETE -- all data was synthetic and local-only ===")


@atexit.register
def _cleanup() -> None:
    import shutil

    shutil.rmtree(_DB_DIR, ignore_errors=True)


def main() -> int:
    run_demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
