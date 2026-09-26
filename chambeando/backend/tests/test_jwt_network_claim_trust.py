"""
Revision de seguridad pedida explicitamente (2026-09-25): "Verifica que la
comprobacion del JWT usa exclusivamente claims validados por el servidor y
que los tokens antiguos tienen un tratamiento explicito y seguro."

Llama las dependencies REALES (auth.get_current_user / auth.get_authenticated_network,
via el endpoint HTTP -- no una reimplementacion de su logica) con JWTs
construidos a mano para probar el limite de confianza exacto: un JWT solo se
confia si esta firmado con settings.SECRET_KEY (jose.jwt.decode lo verifica
siempre, nunca en modo sin-firma); nada de lo que haya DENTRO del payload
importa si la firma no valida.
"""

from datetime import timedelta

from jose import jwt as jose_jwt

from backend import timeutils
from backend.auth import LEGACY_NETWORK_ID_FALLBACK
from backend.config import settings

from .conftest import auth_headers, login


def _make_jwt(payload: dict, secret: str = settings.SECRET_KEY) -> str:
    return jose_jwt.encode(payload, secret, algorithm=settings.ALGORITHM)


def test_jwt_signed_with_wrong_secret_is_rejected(client):
    """Ningun claim dentro del payload importa si la firma no verifica contra
    settings.SECRET_KEY -- este es el LIMITE DE CONFIANZA real, no la forma
    del payload."""
    forged = _make_jwt(
        {"sub": "TForgedWalletNeverAuthenticated01", "network_id": "tron", "exp": timeutils.utcnow() + timedelta(hours=1)},
        secret="a-completely-different-secret-the-server-never-used",
    )
    resp = client.get("/me", headers=auth_headers(forged))
    assert resp.status_code == 401


def test_jwt_with_tampered_payload_but_original_signature_is_rejected(client):
    """Simula el ataque clasico de JWT: tomar un token real, valido, y
    cambiarle un caracter del payload sin re-firmar -- la firma HMAC deja de
    validar contra el payload modificado."""
    token = login(client, "TTamperPayloadWallet0000000001")
    header_b64, payload_b64, sig_b64 = token.split(".")
    tampered_payload_b64 = payload_b64[:-4] + ("A" if payload_b64[-4] != "A" else "B") + payload_b64[-3:]
    tampered_token = f"{header_b64}.{tampered_payload_b64}.{sig_b64}"

    resp = client.get("/me", headers=auth_headers(tampered_token))
    assert resp.status_code == 401


def test_jwt_missing_network_id_claim_falls_back_to_tron_via_the_real_dependency(client, db_session):
    """Token 'antiguo' (forma pre-revision: solo 'sub', sin 'network_id'),
    pero firmado LEGITIMAMENTE con settings.SECRET_KEY -- exactamente lo que
    un JWT emitido antes de esta revision, todavia dentro de su ventana de
    expiracion, se ve. Debe seguir funcionando (no invalidar sesiones
    existentes) resolviendo a 'tron' -- probado contra el endpoint HTTP
    real, no reimplementando la logica de auth.py."""
    from backend.models import UserDB

    user = UserDB(wallet_address="TLegacyShapedJwtWallet00000001", network_id="tron")
    db_session.add(user)
    db_session.commit()

    legacy_shaped_token = _make_jwt({"sub": "TLegacyShapedJwtWallet00000001", "exp": timeutils.utcnow() + timedelta(hours=1)})

    resp = client.get("/me", headers=auth_headers(legacy_shaped_token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["wallet_address"] == "TLegacyShapedJwtWallet00000001"

    # y NO crea un usuario duplicado -- resuelve al mismo via el fallback documentado
    matches = db_session.query(UserDB).filter(UserDB.wallet_address == "TLegacyShapedJwtWallet00000001").all()
    assert len(matches) == 1
    assert matches[0].network_id == LEGACY_NETWORK_ID_FALLBACK == "tron"


def test_jwt_without_sub_claim_at_all_is_rejected(client):
    token = _make_jwt({"network_id": "tron", "exp": timeutils.utcnow() + timedelta(hours=1)})
    resp = client.get("/me", headers=auth_headers(token))
    assert resp.status_code == 401


def test_no_bearer_token_at_all_is_rejected(client):
    resp = client.get("/me")
    assert resp.status_code == 401


def test_network_id_in_a_real_jwt_can_only_ever_be_a_value_that_already_passed_the_auth_whitelist(client):
    """No es un chequeo redundante que haga get_current_user por su cuenta --
    es una propiedad de construccion: create_access_token() solo se llama
    desde /auth/verify DESPUES de que _resolve_network_id() ya valido el
    network_id contra chain/auth_networks.py. Un JWT real jamas puede tener
    un network_id fuera de esa whitelist, porque nunca se firma uno para una
    red rechazada. Este test lo confirma extremo a extremo: pedir /auth/nonce
    para una red no admitida nunca llega a producir un JWT."""
    resp = client.post("/auth/nonce", json={"wallet_address": "TWalletForUnauthorizedNetwork01", "network_id": "solana"})
    assert resp.status_code == 400
    # nunca se emitio nonce, por lo tanto /auth/verify tampoco puede emitir un JWT para esa red
    verify_resp = client.post(
        "/auth/verify",
        json={"wallet_address": "TWalletForUnauthorizedNetwork01", "signature": "irrelevant", "network_id": "solana"},
    )
    assert verify_resp.status_code == 400
    assert "access_token" not in verify_resp.json()
