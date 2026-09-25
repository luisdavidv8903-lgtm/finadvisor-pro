"""
Wallet link multichain (revision humana, item 5/6): el network_id de un token
de wallet-link se fija SERVER-SIDE al emitirlo (issue_link_token) y se
revalida al consumirlo (consume_link_token) -- nunca se confia en un
network_id que el cliente simplemente afirme. Backward compatible: los
callers que no pasan network_id (ver test_whatsapp_adapter.py) siguen
funcionando exactamente como antes.
"""

import pytest

from backend.messaging.identity import LinkTokenError, consume_link_token, issue_link_token
from backend.models import UserDB


@pytest.fixture()
def user(db_session) -> UserDB:
    u = UserDB(wallet_address="TFakeLinkBindingWallet0000001")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def other_user(db_session) -> UserDB:
    u = UserDB(wallet_address="TFakeLinkBindingOtherWallet01")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def test_token_bound_to_the_network_it_was_issued_for_round_trips(db_session, user):
    token = issue_link_token(db_session, "wa-bind-1", ttl_seconds=600, network_id="tron")
    link = consume_link_token(db_session, token, user, network_id="tron")
    assert link.user_id == user.id


def test_network_mismatch_during_link_is_rejected(db_session, user):
    """El caso central de la revision: un token emitido para 'tron' no puede
    consumirse afirmando 'bsc' (o cualquier otra red), aunque 'bsc' exista en
    el registry -- el valor que manda es el que se guardo al emitir, nunca el
    que el consumidor declara."""
    token = issue_link_token(db_session, "wa-bind-mismatch-1", ttl_seconds=600, network_id="tron")
    with pytest.raises(LinkTokenError, match="network mismatch"):
        consume_link_token(db_session, token, user, network_id="bsc")


def test_disabled_network_during_link_issuance_is_rejected(db_session):
    """bsc esta DISABLED en el registry hoy -- issue_link_token falla cerrado
    ANTES de emitir ningun token, nunca deja un link a medio-emitir para una
    red que no puede operar."""
    with pytest.raises(LinkTokenError, match="DISABLED"):
        issue_link_token(db_session, "wa-bind-disabled-1", ttl_seconds=600, network_id="bsc")


def test_historical_network_during_link_issuance_is_rejected(db_session):
    """Revision humana 2026-09-25: 'algorand' es HISTORICAL (Tether ya no
    emite/redime ahi) -- issue_link_token debia rechazarlo igual que
    DISABLED, pero _resolve_and_require_operational_network solo miraba
    `status == DISABLED` y dejaba pasar HISTORICAL. Ya corregido: ambas
    ramas usan la misma NEVER_OPERATIONAL_STATUSES que build_adapter_for_network."""
    with pytest.raises(LinkTokenError, match="HISTORICAL"):
        issue_link_token(db_session, "wa-bind-historical-1", ttl_seconds=600, network_id="algorand")


def test_disabled_network_during_link_consumption_is_rejected(db_session, user):
    """Simula un token ya emitido cuyo network_id quedo DISABLED entre la
    emision y el consumo (p.ej. un operador desactivo la red) -- el
    re-chequeo de consume_link_token debe rechazarlo, no solo el de
    issue_link_token. Se fuerza el escenario escribiendo directamente
    link_token_network_id (issue_link_token en si mismo ya rechaza emitir
    para 'bsc', ver test de arriba -- este test aisla especificamente el
    gate del lado de CONSUMO)."""
    from backend.models import WhatsAppLinkDB

    token = issue_link_token(db_session, "wa-bind-disabled-consume-1", ttl_seconds=600, network_id="tron")
    link = db_session.query(WhatsAppLinkDB).filter(WhatsAppLinkDB.whatsapp_id == "wa-bind-disabled-consume-1").first()
    link.link_token_network_id = "bsc"
    db_session.commit()

    with pytest.raises(LinkTokenError, match="DISABLED"):
        consume_link_token(db_session, token, user, network_id="bsc")


def test_historical_network_during_link_consumption_is_rejected(db_session, user):
    """Mismo escenario que el test anterior pero para HISTORICAL -- este es
    el hallazgo #1 de la revision humana en su forma mas directa: antes de
    la correccion, consume_link_token aceptaba un network_id HISTORICAL
    porque el chequeo de re-validacion (_resolve_and_require_operational_network)
    solo miraba DISABLED."""
    from backend.models import WhatsAppLinkDB

    token = issue_link_token(db_session, "wa-bind-historical-consume-1", ttl_seconds=600, network_id="tron")
    link = db_session.query(WhatsAppLinkDB).filter(WhatsAppLinkDB.whatsapp_id == "wa-bind-historical-consume-1").first()
    link.link_token_network_id = "algorand"
    db_session.commit()

    with pytest.raises(LinkTokenError, match="HISTORICAL"):
        consume_link_token(db_session, token, user, network_id="algorand")


def test_tron_testnet_link_still_works_end_to_end(db_session, user):
    """Control: la correccion de HISTORICAL/DISABLED no debe afectar el
    enlace funcional de TRON, que sigue en TESTNET (operacional, no en
    NEVER_OPERATIONAL_STATUSES)."""
    token = issue_link_token(db_session, "wa-bind-tron-control-1", ttl_seconds=600, network_id="tron")
    link = consume_link_token(db_session, token, user, network_id="tron")
    assert link.user_id == user.id


def test_unknown_network_during_link_issuance_is_rejected(db_session):
    with pytest.raises(LinkTokenError):
        issue_link_token(db_session, "wa-bind-unknown-1", ttl_seconds=600, network_id="does-not-exist")


def test_unknown_network_during_link_consumption_is_rejected(db_session, user):
    token = issue_link_token(db_session, "wa-bind-consume-unknown-1", ttl_seconds=600, network_id="tron")
    with pytest.raises(LinkTokenError):
        consume_link_token(db_session, token, user, network_id="does-not-exist")


def test_network_id_omitted_at_consumption_skips_the_check_for_backward_compatibility(db_session, user):
    """Callers que predatan el binding por red (network_id=None) siguen
    funcionando exactamente como antes -- ver test_whatsapp_adapter.py, que
    no pasa network_id en ninguna llamada."""
    token = issue_link_token(db_session, "wa-bind-legacy-1", ttl_seconds=600)  # network_id=None -> default CHAIN_ADAPTER
    link = consume_link_token(db_session, token, user)  # sin network_id -> no valida el binding
    assert link.user_id == user.id


def test_issue_link_token_defaults_network_id_to_chain_adapter_setting(db_session):
    """Sin network_id explicito, se usa settings.CHAIN_ADAPTER (hoy 'tron') --
    nunca queda sin red asociada."""
    from backend.config import settings
    from backend.models import WhatsAppLinkDB

    issue_link_token(db_session, "wa-bind-default-1", ttl_seconds=600)
    link = db_session.query(WhatsAppLinkDB).filter(WhatsAppLinkDB.whatsapp_id == "wa-bind-default-1").first()
    assert link.link_token_network_id == settings.CHAIN_ADAPTER


def test_consumed_token_clears_the_network_binding_alongside_the_hash(db_session, user):
    """Single-use tambien para el binding de red: una vez consumido, no queda
    un network_id residual que pudiera confundirse con un token vigente."""
    from backend.models import WhatsAppLinkDB

    token = issue_link_token(db_session, "wa-bind-clear-1", ttl_seconds=600, network_id="tron")
    consume_link_token(db_session, token, user, network_id="tron")
    link = db_session.query(WhatsAppLinkDB).filter(WhatsAppLinkDB.whatsapp_id == "wa-bind-clear-1").first()
    assert link.link_token_network_id is None
    assert link.link_token_hash is None


def test_whatsapp_link_http_endpoint_requires_network_id(client, db_session):
    """Fase 8/revision: WhatsAppLinkRequest exige network_id -- un request sin
    el campo es rechazado por FastAPI/Pydantic antes de llegar a la logica de
    negocio (422), no silenciosamente aceptado sin binding."""
    from .conftest import auth_headers, login

    token = login(client, "TFakeLinkBindingHttpWallet01")
    r = client.post("/whatsapp/link", json={"link_token": "whatever"}, headers=auth_headers(token))
    assert r.status_code == 422


def test_whatsapp_link_http_endpoint_rejects_network_mismatch(client, db_session, user):
    from .conftest import auth_headers, login

    link_token = issue_link_token(db_session, "wa-bind-http-mismatch-1", ttl_seconds=600, network_id="tron")
    access_token = login(client, user.wallet_address)
    r = client.post(
        "/whatsapp/link",
        json={"link_token": link_token, "network_id": "bsc"},
        headers=auth_headers(access_token),
    )
    assert r.status_code == 400
    assert "network" in r.json()["detail"].lower()
