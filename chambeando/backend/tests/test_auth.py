"""
Auth: firma de wallet, ciclo de vida del nonce, replay, rate limiting.
Usa FakeChainAdapter.verify_wallet_signature (firma "valida" = string exacto
`valid-sig:{address}:{message}") — determinista, sin criptografia real, para
poder probar cada caso de falla sin depender de una libreria de firmas."""

from datetime import datetime, timedelta, timezone

from backend import auth as auth_module
from backend.models import AuthNonceDB

from .conftest import login
from .fake_chain_adapter import make_valid_signature

WALLET = "TFakeSeller00000000000000000001"
OTHER_WALLET = "TFakeOther000000000000000000002"


def test_valid_signature_succeeds(client):
    token = login(client, WALLET)
    assert token


def test_wrong_wallet_rejected(client):
    nonce = client.post("/auth/nonce", json={"wallet_address": WALLET}).json()
    message = nonce["message"]
    # firmado como si fuera OTRA wallet
    signature = make_valid_signature(OTHER_WALLET, message)
    r = client.post("/auth/verify", json={"wallet_address": WALLET, "signature": signature})
    assert r.status_code == 401


def test_altered_message_rejected(client):
    nonce = client.post("/auth/nonce", json={"wallet_address": WALLET}).json()
    tampered_message = nonce["message"] + " extra"
    signature = make_valid_signature(WALLET, tampered_message)
    r = client.post("/auth/verify", json={"wallet_address": WALLET, "signature": signature})
    assert r.status_code == 401


def test_altered_nonce_rejected(client):
    client.post("/auth/nonce", json={"wallet_address": WALLET})
    fabricated_message = auth_module.build_sign_message("0" * 32)  # nonce que el server nunca emitio
    signature = make_valid_signature(WALLET, fabricated_message)
    r = client.post("/auth/verify", json={"wallet_address": WALLET, "signature": signature})
    assert r.status_code == 401


def test_malformed_signature_rejected(client):
    nonce = client.post("/auth/nonce", json={"wallet_address": WALLET}).json()
    r = client.post("/auth/verify", json={"wallet_address": WALLET, "signature": "not-a-real-signature-at-all"})
    assert r.status_code == 401


def test_expired_nonce_rejected(client, db_session):
    nonce = client.post("/auth/nonce", json={"wallet_address": WALLET}).json()
    entry = db_session.query(AuthNonceDB).filter(AuthNonceDB.wallet_address == WALLET).order_by(AuthNonceDB.id.desc()).first()
    entry.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    signature = make_valid_signature(WALLET, nonce["message"])
    r = client.post("/auth/verify", json={"wallet_address": WALLET, "signature": signature})
    assert r.status_code == 401


def test_reused_nonce_rejected_at_unit_level(db_session):
    """Prueba directa de auth.verify_and_consume_nonce: el mismo nonce nunca se
    puede consumir dos veces, sin pasar por HTTP."""
    from backend.config import settings

    nonce = auth_module.generate_nonce(db_session, WALLET)
    first = auth_module.verify_and_consume_nonce(db_session, WALLET, nonce)
    second = auth_module.verify_and_consume_nonce(db_session, WALLET, nonce)
    assert first is True
    assert second is False


def test_replay_attempt_denied_over_http(client):
    nonce = client.post("/auth/nonce", json={"wallet_address": WALLET}).json()
    signature = make_valid_signature(WALLET, nonce["message"])

    first = client.post("/auth/verify", json={"wallet_address": WALLET, "signature": signature})
    assert first.status_code == 200

    replay = client.post("/auth/verify", json={"wallet_address": WALLET, "signature": signature})
    assert replay.status_code in (400, 401)
    assert "access_token" not in replay.json()


def test_nonce_invalidated_atomically_after_successful_auth(client, db_session):
    nonce = client.post("/auth/nonce", json={"wallet_address": WALLET}).json()
    signature = make_valid_signature(WALLET, nonce["message"])
    r = client.post("/auth/verify", json={"wallet_address": WALLET, "signature": signature})
    assert r.status_code == 200

    entry = db_session.query(AuthNonceDB).filter(AuthNonceDB.wallet_address == WALLET).order_by(AuthNonceDB.id.desc()).first()
    assert entry.used == 1


def test_rate_limiting_on_nonce_endpoint(client):
    from backend.config import settings

    for _ in range(settings.RATE_LIMIT_NONCE_PER_MINUTE):
        r = client.post("/auth/nonce", json={"wallet_address": WALLET})
        assert r.status_code == 200
    over_limit = client.post("/auth/nonce", json={"wallet_address": WALLET})
    assert over_limit.status_code == 429


def test_rate_limiting_on_verify_endpoint(client):
    from backend.config import settings

    for _ in range(settings.RATE_LIMIT_VERIFY_PER_MINUTE):
        r = client.post("/auth/verify", json={"wallet_address": WALLET, "signature": "bogus"})
        assert r.status_code in (400, 401)
    over_limit = client.post("/auth/verify", json={"wallet_address": WALLET, "signature": "bogus"})
    assert over_limit.status_code == 429


def test_verify_without_prior_nonce_request_denied(client):
    r = client.post("/auth/verify", json={"wallet_address": "TNeverRequestedNonce0000000001", "signature": "bogus"})
    assert r.status_code == 400
