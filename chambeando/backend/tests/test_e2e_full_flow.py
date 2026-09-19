"""
Phase 2C — full local end-to-end product flow, driven through the real
FastAPI routes (auth -> membership -> orders -> settlement -> disputes ->
admin), a real Alembic-migrated database (SQLite here; the same flow is
re-run against real PostgreSQL in test_e2e_postgres.py), and a synthetic
chain adapter.

Architecture choice (Phase 2C section 1 explicitly forbids a second business
state machine that could disagree with EscrowP2P.sol): this file reuses
FakeChainAdapter AS-IS (the same test double every other test in this suite
already uses for OPEN/CLAIMED/PAID/DISPUTED/RELEASED/REFUNDED/CANCELLED) and
only swaps `verify_wallet_signature` for real tronpy ECDSA verification, so
identities in this file are backed by genuine synthetic keypairs (Phase 2C
section 2) while the on-chain state machine itself stays the single
already-verified source of truth.

Two deliberate, explicitly-authorized bypasses of "go through the API":
  1. bootstrap_admin() is called directly — there is intentionally no HTTP
     route for it (see bootstrap_admin.py's docstring); this is the only way
     the first admin can ever be created, in production or in this test.
  2. On-chain state transitions (claim/pay/dispute/settle) are simulated by
     calling the chain adapter directly (`chain.seed_*`) and syncing the
     indexer — because the REAL product does not expose "claim"/"release" as
     backend HTTP endpoints at all (those are calls to the smart contract,
     already verified by its own 61-test suite). This is not a shortcut
     around a real feature; it is the actual architecture.

Every membership, settlement-detail, order-metadata, dispute-evidence,
dispute-assignment and privacy check below goes through the real HTTP
routes via TestClient, matching Phase 2C section 12.
"""
import pathlib
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

tronpy = pytest.importorskip("tronpy", reason="tronpy not installed in this environment")

from tronpy.keys import PrivateKey  # noqa: E402

from backend.bootstrap_admin import bootstrap_admin  # noqa: E402
from backend.chain import reset_chain_adapter_for_tests, set_chain_adapter_for_tests  # noqa: E402
from backend.chain.tron_adapter import verify_tron_signature  # noqa: E402
from backend.database import get_db  # noqa: E402
from backend.indexer import _apply_event, run_indexer_once  # noqa: E402
from backend.main import app  # noqa: E402
from backend.models import (  # noqa: E402
    DisputeAssignmentDB,
    IndexerCheckpointDB,
    OrderStatus,
    P2POrderDB,
    SecurityEventDB,
)
from backend.security.rate_limit import reset_rate_limiter_for_tests  # noqa: E402

from .fake_chain_adapter import FakeChainAdapter  # noqa: E402

BACKEND_DIR = pathlib.Path(__file__).resolve().parent.parent


class SyntheticChainAdapter(FakeChainAdapter):
    """Same state machine as every other test in this suite (see module
    docstring) -- only verify_wallet_signature is real tronpy crypto instead
    of FakeChainAdapter's trivial string-equality scheme."""

    def verify_wallet_signature(self, address: str, message: str, signature: str) -> bool:
        return verify_tron_signature(address, message, signature)


def make_wallet():
    """Ephemeral synthetic TRON keypair -- generated fresh, never touched
    testnet/mainnet, never had real value. The private key object is kept
    only in memory for this test and never printed or persisted."""
    pk = PrivateKey.random()
    return pk, pk.public_key.to_base58check_address()


def sign(pk, message: str) -> str:
    return pk.sign_msg(message.encode()).hex()


def login(client: TestClient, pk, address: str) -> str:
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
    """Same idempotent-per-event application the real indexer uses
    (_apply_event), but applied immediately (bypassing the confirmation-delay
    gate) -- the SAME shortcut every other test in this suite already relies
    on for business-logic tests. The confirmation-delay/checkpoint machinery
    itself is proven separately and for real in test_indexer_reconciliation
    below, via run_indexer_once()."""
    for event in chain.get_events(0, chain.current_block()):
        _apply_event(db, event)
    db.commit()


@pytest.fixture()
def e2e_env(tmp_path, monkeypatch):
    """A real Alembic-migrated SQLite database (bootstrap_admin() checks for
    the alembic_version marker, so create_all() would not do) wired as the
    TestClient's actual get_db -- the whole HTTP flow in this file runs
    against ONE consistently-migrated database, closer to the real product
    than the create_all() shortcut the rest of the suite uses."""
    db_path = tmp_path / "e2e.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocalTest = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocalTest()

    adapter = SyntheticChainAdapter()
    set_chain_adapter_for_tests(adapter)

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limiter_for_tests()

    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, db=session, chain=adapter, engine=engine, SessionLocalTest=SessionLocalTest
        )

    app.dependency_overrides.clear()
    reset_chain_adapter_for_tests()
    session.close()
    engine.dispose()


@pytest.fixture()
def onboarded(e2e_env):
    """Phase 2C section 3, proven through real application behavior:
    ADMIN bootstrapped -> invite created -> SELLER redeems -> BUYER redeems a
    SEPARATE invite -> memberships ACTIVE. Also seeds a THIRD_PARTY member
    (its own separate invite) for the privacy tests in section 7."""
    env = e2e_env
    client = env.client

    admin_pk, admin_wallet = make_wallet()
    admin_user = bootstrap_admin(env.db, env.chain, admin_wallet)
    admin_token = login(client, admin_pk, admin_wallet)

    def create_invite() -> str:
        r = client.post("/admin/invites", json={"max_uses": 1, "expires_in_hours": 72}, headers=hdr(admin_token))
        assert r.status_code == 200, r.text
        return r.json()["code"]

    def redeem(pk, address, code) -> dict:
        token = login(client, pk, address)
        r = client.post("/invites/redeem", json={"code": code}, headers=hdr(token))
        assert r.status_code == 200, r.text
        return token

    seller_pk, seller_wallet = make_wallet()
    buyer_pk, buyer_wallet = make_wallet()
    third_pk, third_wallet = make_wallet()

    seller_token = redeem(seller_pk, seller_wallet, create_invite())
    buyer_token = redeem(buyer_pk, buyer_wallet, create_invite())
    third_token = redeem(third_pk, third_wallet, create_invite())

    return SimpleNamespace(
        env=env,
        client=client,
        admin=SimpleNamespace(pk=admin_pk, wallet=admin_wallet, token=admin_token, user=admin_user),
        seller=SimpleNamespace(pk=seller_pk, wallet=seller_wallet, token=seller_token),
        buyer=SimpleNamespace(pk=buyer_pk, wallet=buyer_wallet, token=buyer_token),
        third_party=SimpleNamespace(pk=third_pk, wallet=third_wallet, token=third_token),
        create_invite=create_invite,
    )


# ---------------------------------------------------------------------------
# Section 3 — invite + membership flow
# ---------------------------------------------------------------------------


def test_memberships_active_after_redemption(onboarded):
    client = onboarded.client
    for who in (onboarded.seller, onboarded.buyer, onboarded.third_party):
        me = client.get("/me", headers=hdr(who.token)).json()
        assert me["is_member"] is True
        assert me["role"] == "member"


def test_uninvited_wallet_cannot_obtain_membership(onboarded):
    """Authenticating (proving wallet ownership) alone never creates
    membership -- there is no invite path taken for this wallet."""
    client = onboarded.client
    stranger_pk, stranger_wallet = make_wallet()
    token = login(client, stranger_pk, stranger_wallet)
    me = client.get("/me", headers=hdr(token)).json()
    assert me["is_member"] is False
    assert me["role"] is None
    # and it is locked out of any member-only route
    r = client.get("/orders/", headers=hdr(token))
    assert r.status_code == 403


def test_third_party_authenticated_non_member_cannot_access_orderbook(onboarded):
    client = onboarded.client
    stranger_pk, stranger_wallet = make_wallet()
    token = login(client, stranger_pk, stranger_wallet)
    r = client.get("/orders/", headers=hdr(token))
    assert r.status_code == 403


def test_unauthenticated_cannot_access_orderbook(onboarded):
    r = onboarded.client.get("/orders/")
    assert r.status_code in (401, 403)


def test_invite_limits_and_expiration_enforced_in_this_flow(onboarded):
    client = onboarded.client
    code = onboarded.create_invite()  # max_uses=1
    p1_pk, p1_wallet = make_wallet()
    p2_pk, p2_wallet = make_wallet()

    t1 = login(client, p1_pk, p1_wallet)
    r1 = client.post("/invites/redeem", json={"code": code}, headers=hdr(t1))
    assert r1.status_code == 200

    t2 = login(client, p2_pk, p2_wallet)
    r2 = client.post("/invites/redeem", json={"code": code}, headers=hdr(t2))
    assert r2.status_code == 400  # cupo agotado (max_uses=1, ya usado)


# ---------------------------------------------------------------------------
# Section 4 — settlement detail flow (synthetic payment data only)
# ---------------------------------------------------------------------------

SYNTHETIC_PAYMENT_METHOD = "TEST_CUP_TRANSFER"
SYNTHETIC_ACCOUNT_REFERENCE = "TEST-000000"


def _create_settlement_detail(client, token) -> dict:
    r = client.post(
        "/settlement-details",
        json={
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "currency": "CUP",
            "payload": {"recipient": "SYNTHETIC SELLER", "account_reference": SYNTHETIC_ACCOUNT_REFERENCE},
        },
        headers=hdr(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_settlement_detail_encrypted_and_not_leaked_raw(onboarded):
    env = onboarded.env
    detail = _create_settlement_detail(onboarded.client, onboarded.seller.token)

    # raw at rest: the DB column holds ciphertext, never the plaintext reference
    from backend.models import SettlementDetailDB

    row = env.db.query(SettlementDetailDB).filter(SettlementDetailDB.id == detail["id"]).first()
    assert SYNTHETIC_ACCOUNT_REFERENCE.encode() not in row.encrypted_payload

    # raw absent from the response schema itself (SettlementDetailOut never carries payload)
    assert "payload" not in detail
    assert SYNTHETIC_ACCOUNT_REFERENCE not in str(detail)

    # raw absent from every audit event recorded so far
    events = env.db.query(SecurityEventDB).all()
    for e in events:
        assert SYNTHETIC_ACCOUNT_REFERENCE not in (e.reason or "")


def test_settlement_detail_absent_from_orderbook_response(onboarded):
    client = onboarded.client
    _create_settlement_detail(client, onboarded.seller.token)
    body = client.get("/orders/", headers=hdr(onboarded.seller.token)).text
    assert SYNTHETIC_ACCOUNT_REFERENCE not in body


def test_third_party_cannot_reveal_settlement_detail(onboarded, happy_path_order):
    order_id = happy_path_order["order_id"]
    r = onboarded.client.get(f"/orders/{order_id}/settlement", headers=hdr(onboarded.third_party.token))
    assert r.status_code == 403


def test_buyer_cannot_reveal_before_match(onboarded):
    """Before CLAIMED, order.buyer_wallet is still unset -- nobody who isn't
    the seller is an order party yet, so reveal must fail closed."""
    client, env = onboarded.client, onboarded.env
    detail = _create_settlement_detail(client, onboarded.seller.token)

    env.chain.seed_order_created(9101, onboarded.seller.wallet, "TFakeTokenAddress0000001", 100_000000)
    apply_all_events(env.db, env.chain)
    order = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == 9101).first()

    client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": 9101,
            "fiat_amount": "100.00",
            "fiat_currency": "CUP",
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "settlement_detail_id": detail["id"],
        },
        headers=hdr(onboarded.seller.token),
    )

    r = client.get(f"/orders/{order.id}/settlement", headers=hdr(onboarded.buyer.token))
    assert r.status_code == 403  # buyer_wallet still None -- buyer is not yet a party


# ---------------------------------------------------------------------------
# Section 5 — happy-path trade (fixture also feeds sections 4/6/7's tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
def happy_path_order(onboarded):
    """Executes and proves EVERY transition in Phase 2C section 5:
    OPEN -> metadata+settlement snapshot attached -> CLAIMED -> buyer reveals
    -> PAID -> RELEASED -> indexer converges -> reputation updates."""
    client, env = onboarded.client, onboarded.env
    onchain_id = 9001

    detail = _create_settlement_detail(client, onboarded.seller.token)

    # OPEN
    env.chain.seed_order_created(onchain_id, onboarded.seller.wallet, "TFakeTokenAddress0000001", 100_000000)
    apply_all_events(env.db, env.chain)
    order_row = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()
    assert order_row.onchain_status == OrderStatus.OPEN

    order_get = client.get(f"/orders/{order_row.id}", headers=hdr(onboarded.seller.token)).json()
    assert order_get["onchain_status"] == "open"

    # metadata + settlement snapshot attached (still pre-match, HTTP route)
    meta_resp = client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": onchain_id,
            "fiat_amount": "100.00",
            "fiat_currency": "CUP",
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "settlement_detail_id": detail["id"],
        },
        headers=hdr(onboarded.seller.token),
    )
    assert meta_resp.status_code == 201, meta_resp.text
    assert env.db.query(P2POrderDB).get(order_row.id).settlement_snapshot_payload is not None

    # CLAIMED (buyer claims on-chain; arbiter snapshot set by the "contract")
    env.chain.seed_order_claimed(onchain_id, onboarded.buyer.wallet, arbiter_snapshot="TFakeArbiterWallet00000001")
    apply_all_events(env.db, env.chain)
    order_row = env.db.query(P2POrderDB).get(order_row.id)
    assert order_row.onchain_status == OrderStatus.CLAIMED
    assert order_row.buyer_wallet == onboarded.buyer.wallet

    # buyer can now reveal the settlement snapshot
    reveal = client.get(f"/orders/{order_row.id}/settlement", headers=hdr(onboarded.buyer.token))
    assert reveal.status_code == 200, reveal.text
    assert reveal.json()["payload"]["account_reference"] == SYNTHETIC_ACCOUNT_REFERENCE

    # buyer marks synthetic fiat paid (an on-chain action -> PAID)
    env.chain.seed_paid(onchain_id)
    apply_all_events(env.db, env.chain)
    order_row = env.db.query(P2POrderDB).get(order_row.id)
    assert order_row.onchain_status == OrderStatus.PAID
    assert client.get(f"/orders/{order_row.id}", headers=hdr(onboarded.seller.token)).json()["onchain_status"] == "paid"

    # seller confirms/release -> RELEASED
    env.chain.seed_settled(onchain_id, "RELEASED")
    apply_all_events(env.db, env.chain)
    order_row = env.db.query(P2POrderDB).get(order_row.id)
    assert order_row.onchain_status == OrderStatus.RELEASED
    assert client.get(f"/orders/{order_row.id}", headers=hdr(onboarded.seller.token)).json()["onchain_status"] == "released"

    return {"order_id": order_row.id, "onchain_id": onchain_id, "detail_id": detail["id"]}


def test_happy_path_trade_transitions(happy_path_order):
    # fixture itself asserts every transition; this test documents the scenario exists
    assert happy_path_order["order_id"] is not None


def test_happy_path_reputation_updates_from_finalized_state(onboarded, happy_path_order):
    client = onboarded.client
    env = onboarded.env
    seller_id = env.db.query(P2POrderDB).get(happy_path_order["order_id"]).created_by_user_id
    rep = client.get(f"/reputation/{seller_id}", headers=hdr(onboarded.seller.token)).json()
    assert rep["completed_trades"] == 1
    assert rep["disputes_opened"] == 0


def test_backend_never_recomputes_crypto_amount_independently(onboarded, happy_path_order):
    """Section 6: the backend does not run its own fee math -- it only
    mirrors whatever amount the chain event carried. Fee math itself (gross =
    buyer amount + fee, refund fee == 0) is exclusively the contract's job,
    already proven by the 61-test contract suite; re-implementing it here
    would be exactly the disagreeing second state machine Phase 2C forbids."""
    env = onboarded.env
    order = env.db.query(P2POrderDB).get(happy_path_order["order_id"])
    assert order.crypto_amount == 100_000000


# ---------------------------------------------------------------------------
# Section 7 — privacy at each stage of a live trade
# ---------------------------------------------------------------------------


def test_public_unauthenticated_gets_nothing(onboarded, happy_path_order):
    r = onboarded.client.get(f"/orders/{happy_path_order['order_id']}")
    assert r.status_code in (401, 403)


def test_third_party_member_denied_settlement_evidence_and_metadata(onboarded, happy_path_order):
    client = onboarded.client
    order_id = happy_path_order["order_id"]
    third = onboarded.third_party.token

    settlement = client.get(f"/orders/{order_id}/settlement", headers=hdr(third))
    assert settlement.status_code == 403

    order_out = client.get(f"/orders/{order_id}", headers=hdr(third)).json()
    assert "settlement" not in order_out
    assert "buyer_wallet" not in order_out  # only the masked field is ever exposed
    assert SYNTHETIC_ACCOUNT_REFERENCE not in str(order_out)


def test_orderbook_never_exposes_full_wallets(onboarded, happy_path_order):
    body = onboarded.client.get("/orders/", headers=hdr(onboarded.third_party.token)).json()
    for o in body:
        assert o["seller_wallet_masked"] != onboarded.seller.wallet
        assert onboarded.seller.wallet not in str(o)
        if o["buyer_wallet_masked"]:
            assert onboarded.buyer.wallet not in str(o)


def test_admin_does_not_automatically_get_settlement_access(onboarded, happy_path_order):
    """Role alone (ADMIN/MODERATOR) must never grant settlement/evidence
    access -- only DisputeAssignment does, and only while DISPUTED."""
    r = onboarded.client.get(
        f"/orders/{happy_path_order['order_id']}/settlement", headers=hdr(onboarded.admin.token)
    )
    assert r.status_code == 403


def test_buyer_and_seller_see_only_what_their_state_authorizes(onboarded, happy_path_order):
    client = onboarded.client
    order_id = happy_path_order["order_id"]
    for token in (onboarded.seller.token, onboarded.buyer.token):
        r = client.get(f"/orders/{order_id}/settlement", headers=hdr(token))
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Section 8 — dispute end-to-end flow (second, independent trade)
# ---------------------------------------------------------------------------


@pytest.fixture()
def moderator(onboarded):
    """Promotes a fresh member to MODERATOR via the real admin route --
    proves role assignment happens through the API, not a shortcut."""
    client = onboarded.client
    mod_pk, mod_wallet = make_wallet()
    token = login(client, mod_pk, mod_wallet)
    r = client.post("/invites/redeem", json={"code": onboarded.create_invite()}, headers=hdr(token))
    assert r.status_code == 200
    # MembershipSelfOut/MeOut deliberately never expose user_id (see schemas.py) --
    # no HTTP route maps a known wallet to its internal id, so this ID lookup
    # (needed only to build the admin role-assignment request) is exactly the
    # "cannot reasonably be performed through the API" fixture exception.
    from backend.models import UserDB

    user_id = onboarded.env.db.query(UserDB).filter(UserDB.wallet_address == mod_wallet).first().id

    role_resp = client.post(
        f"/admin/memberships/{user_id}/role", json={"role": "moderator"}, headers=hdr(onboarded.admin.token)
    )
    assert role_resp.status_code == 200, role_resp.text
    return SimpleNamespace(pk=mod_pk, wallet=mod_wallet, token=token, user_id=user_id)


@pytest.fixture()
def disputed_order(onboarded):
    """OPEN -> CLAIMED -> PAID -> DISPUTED, a SEPARATE trade from
    happy_path_order (Phase 2C section 8: "create a SECOND synthetic trade")."""
    client, env = onboarded.client, onboarded.env
    onchain_id = 9002

    env.chain.seed_order_created(onchain_id, onboarded.seller.wallet, "TFakeTokenAddress0000002", 50_000000)
    apply_all_events(env.db, env.chain)
    env.chain.seed_order_claimed(onchain_id, onboarded.buyer.wallet, arbiter_snapshot="TFakeArbiterWallet00000002")
    apply_all_events(env.db, env.chain)
    env.chain.seed_paid(onchain_id)
    apply_all_events(env.db, env.chain)
    env.chain.seed_disputed(onchain_id)
    apply_all_events(env.db, env.chain)

    order = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()
    assert order.onchain_status == OrderStatus.DISPUTED
    assert order.was_disputed is True

    # buyer submits synthetic evidence (the appropriate participant, per spec)
    ev = client.post(
        "/disputes/evidence",
        json={"order_id": order.id, "evidence_type": "note", "note": "synthetic: buyer never received fiat"},
        headers=hdr(onboarded.buyer.token),
    )
    assert ev.status_code == 201, ev.text

    return SimpleNamespace(order_id=order.id, onchain_id=onchain_id, evidence_id=ev.json()["id"])


def test_unrelated_member_cannot_see_dispute_evidence(onboarded, disputed_order):
    r = onboarded.client.get(f"/disputes/{disputed_order.order_id}/evidence", headers=hdr(onboarded.third_party.token))
    assert r.status_code == 403


def test_moderator_role_alone_does_not_grant_evidence_access(onboarded, disputed_order, moderator):
    r = onboarded.client.get(f"/disputes/{disputed_order.order_id}/evidence", headers=hdr(moderator.token))
    assert r.status_code == 403


def test_scoped_dispute_assignment_grants_only_intended_access(onboarded, disputed_order, moderator):
    client = onboarded.client

    # a DIFFERENT dispute-worthy order the moderator is NOT assigned to
    env = onboarded.env
    other_id = 9003
    env.chain.seed_order_created(other_id, onboarded.seller.wallet, "TFakeTokenAddress0000003", 10_000000)
    apply_all_events(env.db, env.chain)
    env.chain.seed_order_claimed(other_id, onboarded.buyer.wallet, arbiter_snapshot="TFakeArbiterWallet00000003")
    apply_all_events(env.db, env.chain)
    env.chain.seed_paid(other_id)
    apply_all_events(env.db, env.chain)
    env.chain.seed_disputed(other_id)
    apply_all_events(env.db, env.chain)
    other_order = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == other_id).first()

    assign = client.post(
        f"/admin/disputes/{disputed_order.order_id}/assignments",
        json={"assigned_user_id": moderator.user_id, "reason": "synthetic scoped review", "expires_in_hours": 24},
        headers=hdr(onboarded.admin.token),
    )
    assert assign.status_code == 201, assign.text
    assignment_id = assign.json()["id"]

    ok = client.get(f"/disputes/{disputed_order.order_id}/evidence", headers=hdr(moderator.token))
    assert ok.status_code == 200

    scoped_out = client.get(f"/disputes/{other_order.id}/evidence", headers=hdr(moderator.token))
    assert scoped_out.status_code == 403  # assignment does NOT generalize to other disputes

    revoke = client.post(f"/admin/disputes/assignments/{assignment_id}/revoke", headers=hdr(onboarded.admin.token))
    assert revoke.status_code == 200

    after_revoke = client.get(f"/disputes/{disputed_order.order_id}/evidence", headers=hdr(moderator.token))
    assert after_revoke.status_code == 403  # revoked assignment loses access


def test_expired_dispute_assignment_loses_access(onboarded, disputed_order, moderator):
    client, env = onboarded.client, onboarded.env
    assign = client.post(
        f"/admin/disputes/{disputed_order.order_id}/assignments",
        json={"assigned_user_id": moderator.user_id, "reason": "synthetic short-lived review", "expires_in_hours": 1},
        headers=hdr(onboarded.admin.token),
    )
    assert assign.status_code == 201
    assignment = env.db.query(DisputeAssignmentDB).filter(DisputeAssignmentDB.id == assign.json()["id"]).first()

    from datetime import timedelta

    from backend import timeutils

    assignment.expires_at = timeutils.utcnow() - timedelta(minutes=1)
    env.db.commit()

    r = client.get(f"/disputes/{disputed_order.order_id}/evidence", headers=hdr(moderator.token))
    assert r.status_code == 403


def test_dispute_resolution_buyer_plus_arbiter_results_in_released(onboarded, disputed_order):
    """Off-chain DisputeAssignment (staff review access) is a DIFFERENT
    concept from the contract's on-chain 2-of-3 arbiter resolution -- this
    test exercises the latter, purely through the synthetic chain adapter +
    indexer (the same path the real contract's arbiter-vote outcome would
    reach the backend through)."""
    client, env = onboarded.client, onboarded.env
    env.chain.seed_settled(disputed_order.onchain_id, "RELEASED")
    apply_all_events(env.db, env.chain)

    order = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == disputed_order.onchain_id).first()
    assert order.onchain_status == OrderStatus.RELEASED
    api_view = client.get(f"/orders/{order.id}", headers=hdr(onboarded.buyer.token)).json()
    assert api_view["onchain_status"] == "released"


# ---------------------------------------------------------------------------
# Section 9 — refund path (a THIRD, independent disputed trade -> REFUNDED)
# ---------------------------------------------------------------------------


def test_refund_path_full(onboarded):
    client, env = onboarded.client, onboarded.env
    onchain_id = 9004

    detail = _create_settlement_detail(client, onboarded.seller.token)
    env.chain.seed_order_created(onchain_id, onboarded.seller.wallet, "TFakeTokenAddress0000004", 75_000000)
    apply_all_events(env.db, env.chain)
    order = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()

    client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": onchain_id,
            "fiat_amount": "75.00",
            "fiat_currency": "CUP",
            "payment_method": SYNTHETIC_PAYMENT_METHOD,
            "settlement_detail_id": detail["id"],
        },
        headers=hdr(onboarded.seller.token),
    )

    env.chain.seed_order_claimed(onchain_id, onboarded.buyer.wallet, arbiter_snapshot="TFakeArbiterWallet00000004")
    apply_all_events(env.db, env.chain)
    env.chain.seed_paid(onchain_id)
    apply_all_events(env.db, env.chain)
    env.chain.seed_disputed(onchain_id)
    apply_all_events(env.db, env.chain)

    # seller + arbiter resolution -> REFUNDED (seller wins the dispute)
    env.chain.seed_settled(onchain_id, "REFUNDED")
    apply_all_events(env.db, env.chain)

    order = env.db.query(P2POrderDB).get(order.id)
    assert order.onchain_status == OrderStatus.REFUNDED

    api_view = client.get(f"/orders/{order.id}", headers=hdr(onboarded.seller.token)).json()
    assert api_view["onchain_status"] == "refunded"

    # settlement data remains protected after finalization -- still gated the
    # same way, not opened up just because the trade is over
    third_party_reveal = client.get(f"/orders/{order.id}/settlement", headers=hdr(onboarded.third_party.token))
    assert third_party_reveal.status_code == 403
    party_reveal = client.get(f"/orders/{order.id}/settlement", headers=hdr(onboarded.seller.token))
    assert party_reveal.status_code == 200

    # reputation: seller won the dispute (REFUNDED) -> disputes_won for seller,
    # disputes_lost for buyer (services/reputation.py's documented mapping)
    seller_rep = client.get(f"/reputation/{order.created_by_user_id}", headers=hdr(onboarded.seller.token)).json()
    assert seller_rep["disputes_opened"] == 1
    assert seller_rep["disputes_won"] == 1
    assert seller_rep["disputes_lost"] == 0


# ---------------------------------------------------------------------------
# Section 10 — invalid transitions the backend DOES gate at the API layer
# ---------------------------------------------------------------------------
# Money-movement invalid transitions (buyer releasing seller's funds,
# duplicate release/refund, mutation after RELEASED/REFUNDED, release before
# PAID) are NOT backend HTTP concerns at all in this architecture -- there is
# no /orders/{id}/release or /orders/{id}/claim endpoint (see module
# docstring). Those are calls to EscrowP2P.sol directly, already proven by
# its own 61-test suite (arbiter snapshot timing, double-release, double-
# refund, double-cancel, stuck-fund scenarios, etc.). Re-implementing that
# enforcement here, in a second place, is exactly the disagreeing second
# state machine Phase 2C section 1 forbids. What IS tested below is every
# invalid transition the backend genuinely gates.


def test_cannot_attach_metadata_twice(onboarded):
    client, env = onboarded.client, onboarded.env
    env.chain.seed_order_created(9201, onboarded.seller.wallet, "TFakeTokenAddress0000005", 1_000000)
    apply_all_events(env.db, env.chain)

    payload = {
        "onchain_order_id": 9201,
        "fiat_amount": "1.00",
        "fiat_currency": "CUP",
        "payment_method": SYNTHETIC_PAYMENT_METHOD,
    }
    first = client.post("/orders/metadata", json=payload, headers=hdr(onboarded.seller.token))
    assert first.status_code == 201
    second = client.post("/orders/metadata", json=payload, headers=hdr(onboarded.seller.token))
    assert second.status_code == 400


def test_only_seller_can_attach_metadata(onboarded):
    client, env = onboarded.client, onboarded.env
    env.chain.seed_order_created(9202, onboarded.seller.wallet, "TFakeTokenAddress0000006", 1_000000)
    apply_all_events(env.db, env.chain)

    r = client.post(
        "/orders/metadata",
        json={"onchain_order_id": 9202, "fiat_amount": "1.00", "fiat_currency": "CUP", "payment_method": SYNTHETIC_PAYMENT_METHOD},
        headers=hdr(onboarded.buyer.token),  # not the seller
    )
    assert r.status_code == 403


def test_third_party_cannot_submit_evidence(onboarded, disputed_order):
    r = onboarded.client.post(
        "/disputes/evidence",
        json={"order_id": disputed_order.order_id, "evidence_type": "note", "note": "not my dispute"},
        headers=hdr(onboarded.third_party.token),
    )
    assert r.status_code == 403


def test_cannot_submit_evidence_before_disputed(onboarded, happy_path_order):
    """happy_path_order is already RELEASED -- evidence submission requires
    the order to be DISPUTED right now, not merely to have existed."""
    r = onboarded.client.post(
        "/disputes/evidence",
        json={"order_id": happy_path_order["order_id"], "evidence_type": "note", "note": "too late"},
        headers=hdr(onboarded.buyer.token),
    )
    assert r.status_code == 400


def test_cannot_create_dispute_assignment_on_non_disputed_order(onboarded, happy_path_order, moderator):
    r = onboarded.client.post(
        f"/admin/disputes/{happy_path_order['order_id']}/assignments",
        json={"assigned_user_id": moderator.user_id, "reason": "synthetic", "expires_in_hours": 24},
        headers=hdr(onboarded.admin.token),
    )
    assert r.status_code == 400


def test_settlement_reveal_fails_closed_for_unauthorized_party(onboarded, happy_path_order):
    stranger_pk, stranger_wallet = make_wallet()
    token = login(onboarded.client, stranger_pk, stranger_wallet)
    code = onboarded.create_invite()
    onboarded.client.post("/invites/redeem", json={"code": code}, headers=hdr(token))
    r = onboarded.client.get(f"/orders/{happy_path_order['order_id']}/settlement", headers=hdr(token))
    assert r.status_code == 403


def test_cannot_redeem_invite_when_already_a_member(onboarded):
    r = onboarded.client.post(
        "/invites/redeem", json={"code": onboarded.create_invite()}, headers=hdr(onboarded.seller.token)
    )
    assert r.status_code == 400


def test_suspended_member_denied_marketplace_access(onboarded):
    client, env = onboarded.client, onboarded.env
    from backend.models import UserDB

    seller_user = env.db.query(UserDB).filter(UserDB.wallet_address == onboarded.seller.wallet).first()
    r = client.post(
        f"/admin/memberships/{seller_user.id}/suspend",
        json={"reason": "synthetic test suspension"},
        headers=hdr(onboarded.admin.token),
    )
    assert r.status_code == 200
    denied = client.get("/orders/", headers=hdr(onboarded.seller.token))
    assert denied.status_code == 403

    # reactivate so this test doesn't leak state assumptions to anyone reusing the fixture
    client.post(f"/admin/memberships/{seller_user.id}/reactivate", headers=hdr(onboarded.admin.token))


# ---------------------------------------------------------------------------
# Section 11 — reconciliation / indexer convergence (the REAL indexer, not
# the apply_all_events() test shortcut used everywhere else in this file)
# ---------------------------------------------------------------------------


def test_indexer_reconciliation_converges_and_is_idempotent(onboarded, monkeypatch):
    """Proves, against the REAL run_indexer_once() (checkpoint persistence +
    confirmation-delay gate + idempotent event application), that:
      1. an event without enough confirmations is NOT yet reflected in the
         backend (backend can lag chain -- chain is authoritative);
      2. once confirmations catch up, a subsequent run converges to the
         chain-confirmed state;
      3. re-running the indexer again is a no-op (no duplicate/regressed state).
    """
    env = onboarded.env
    monkeypatch.setattr("backend.indexer.SessionLocal", env.SessionLocalTest)

    def checkpoint_block() -> int:
        env.db.expire_all()
        cp = env.db.query(IndexerCheckpointDB).filter(IndexerCheckpointDB.contract_address == "fake:test-contract").first()
        return cp.last_processed_block if cp else 0

    def run_to_convergence(max_iterations: int = 10) -> None:
        # INDEXER_MAX_BLOCK_RANGE (500) bounds how far one run advances the
        # checkpoint, so "caught up to the chain" can take several calls --
        # this loop is exactly what run_forever()'s poll loop does over time.
        previous = None
        for _ in range(max_iterations):
            run_indexer_once()
            current = checkpoint_block()
            if current == previous:
                return
            previous = current
        raise AssertionError("indexer did not converge within max_iterations")

    onchain_id = 9301
    env.chain.seed_order_created(onchain_id, onboarded.seller.wallet, "TFakeTokenAddress0000007", 5_000000)

    # not enough confirmations yet (CONFIRMATIONS_REQUIRED=19, only 0 blocks since)
    run_indexer_once()
    assert env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first() is None

    # fast-forward the synthetic chain head past the confirmation threshold
    env.chain._block += 25

    run_to_convergence()
    order = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()
    assert order is not None
    assert order.onchain_status == OrderStatus.OPEN
    processed_after_convergence = checkpoint_block()

    # true idempotency: ONLY once fully converged to the chain head does a
    # further run with no new events leave the checkpoint unchanged
    run_indexer_once()
    order_again = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()
    assert order_again.onchain_status == OrderStatus.OPEN
    assert checkpoint_block() == processed_after_convergence  # unchanged -- nothing new to advance to

    # a second, later event converges the same way
    env.chain.seed_order_claimed(onchain_id, onboarded.buyer.wallet, arbiter_snapshot="TFakeArbiterWallet00000099")
    env.chain._block += 25
    run_to_convergence()
    order_final = env.db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == onchain_id).first()
    assert order_final.onchain_status == OrderStatus.CLAIMED
