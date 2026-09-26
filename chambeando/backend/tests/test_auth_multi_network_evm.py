"""
Autenticacion multi-red, EVM solo-para-auth (revision 2026-09-25, renombrado
desde "bsc" por un hallazgo de revision posterior).

Decision de producto: Trust Wallet movil sobre BSC/EVM, EXCLUSIVAMENTE para
autenticacion y vinculo de WhatsApp. Sin ordenes, sin escrow, sin USDT, sin
transacciones en ninguna cadena EVM.

HALLAZGO CORREGIDO AQUI: el network_id auth-only se llama "evm", NUNCA
"bsc" (ni ningun otro nombre de cadena EVM especifica). `personal_sign`
(EIP-191) firma un mensaje sin ningun campo de chainId -- la MISMA wallet,
con CUALQUIER red EVM seleccionada en su UI (BSC, Ethereum, Polygon...),
produce la MISMA firma valida sobre el mismo texto. Llamar a esto "bsc"
habria sido afirmar una verificacion de BSC que el sistema nunca hizo -- ver
backend/chain/auth_networks.py:EVM_AUTH_ONLY_LIMITATIONS para el detalle
completo (que SI se prueba, que NO se prueba, y que haria falta -- EIP-712
con chainId en el domain separator -- para probar criptograficamente una
cadena EVM especifica, con sus propios limites incluso asi).

chain/networks.py:NETWORKS["bsc"] (la ENTRADA OPERACIONAL real de BNB Smart
Chain, chain_id=56) sigue existiendo y sigue DISABLED -- eso es un concepto
COMPLETAMENTE DISTINTO del network_id auth-only "evm" de este archivo, y se
prueba por separado (ver test_evm_auth_identity_has_no_entry_in_the_operational_registry_at_all
y test_real_bsc_operational_registry_entry_independently_stays_disabled).

Usa eth_account real (ya dependencia del backend -- ver
backend/chain/evm_adapter.py, que lo importa a nivel de modulo) para firmar
mensajes EIP-191 personal_sign reales, exactamente el esquema que
verify_evm_signature exige -- no el esquema trivial de FakeChainAdapter.
Claves sinteticas generadas al vuelo (Account.create()), nunca usadas en
ninguna red real, sin valor.
"""

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from backend import auth as auth_module
from backend.chain.auth_networks import AUTH_ONLY_NETWORK_IDS
from backend.chain.networks import NetworkStatus, get_network
from backend.messaging import identity
from backend.models import UserDB

from .conftest import auth_headers


@pytest.fixture()
def user(db_session) -> UserDB:
    u = UserDB(wallet_address="TFakeEvmLinkableGateWallet01", network_id="tron")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _fresh_evm_account():
    return Account.create()


def _sign(account, message: str) -> str:
    signed = Account.sign_message(encode_defunct(text=message), private_key=account.key)
    return signed.signature.hex()


def _evm_nonce_and_message(client, address: str) -> tuple[str, str]:
    resp = client.post("/auth/nonce", json={"wallet_address": address, "network_id": "evm"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["nonce"], body["message"]


def _evm_login(client, account) -> str:
    _, message = _evm_nonce_and_message(client, account.address)
    signature = _sign(account, message)
    resp = client.post(
        "/auth/verify",
        json={"wallet_address": account.address, "signature": signature, "network_id": "evm"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Guardas de alcance: nada de esto activa capacidad operacional para ninguna
# cadena EVM, y la identidad auth-only nunca se hace pasar por una cadena
# especifica.
# ---------------------------------------------------------------------------


def test_evm_is_in_the_auth_only_whitelist_not_derived_from_network_status():
    assert "evm" in AUTH_ONLY_NETWORK_IDS
    assert "tron" in AUTH_ONLY_NETWORK_IDS
    assert "bsc" not in AUTH_ONLY_NETWORK_IDS  # nunca una cadena EVM especifica, ver docstring del modulo


def test_bsc_no_longer_accepted_as_an_auth_network_id_prevents_a_false_bsc_claim(client):
    """La prueba negativa pedida explicitamente: una wallet EVM (con
    CUALQUIER cadena seleccionada, personal_sign no distingue) NUNCA puede
    producir una afirmacion de "bsc" -- porque "bsc" ya ni siquiera es un
    network_id aceptado para autenticacion. El sistema no puede mentir
    sobre algo que ya no puede ni siquiera intentar afirmar."""
    account = _fresh_evm_account()

    nonce_resp = client.post("/auth/nonce", json={"wallet_address": account.address, "network_id": "bsc"})
    assert nonce_resp.status_code == 400

    verify_resp = client.post(
        "/auth/verify",
        json={"wallet_address": account.address, "signature": "0x" + "00" * 65, "network_id": "bsc"},
    )
    assert verify_resp.status_code == 400
    assert "access_token" not in verify_resp.json()


def test_evm_signature_verifies_identically_no_matter_what_chain_the_wallet_would_report(client):
    """`personal_sign` no incluye chainId en lo que firma -- la firma real
    de una wallet EVM es EXACTAMENTE la misma sin importar si esa wallet
    tiene seleccionada BSC, Ethereum o cualquier otra red. Lo probamos
    generando la firma de la forma EXACTA en que una wallet real la
    produciria (sin ningun concepto de "cadena seleccionada" involucrado en
    absoluto -- eth_account.sign_message no toma ni necesita un chainId) y
    confirmando que /auth/verify la acepta iguamente bajo network_id="evm".
    Esto es la demostracion ejecutable de por que NUNCA se puede -- ni se
    intenta -- declarar "bsc" a partir de esta firma."""
    account = _fresh_evm_account()
    _, message = _evm_nonce_and_message(client, account.address)
    signature = _sign(account, message)  # ningun chainId participa en absoluto

    resp = client.post(
        "/auth/verify",
        json={"wallet_address": account.address, "signature": signature, "network_id": "evm"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "chain_id" not in body and "chainId" not in body  # nunca se afirma ni se expone un chainId


def test_evm_auth_identity_has_no_entry_in_the_operational_registry_at_all():
    """"evm" (la identidad auth-only) NO es una cadena real -- no tiene
    ninguna entrada en chain/networks.py, a diferencia de "tron" (que si
    corresponde 1:1 con la red operacional homonima). get_network("evm")
    debe fallar por desconocido, nunca resolver silenciosamente a alguna
    cadena EVM en particular."""
    with pytest.raises(ValueError, match="desconocido"):
        get_network("evm")


def test_real_bsc_operational_registry_entry_independently_stays_disabled():
    """La ENTRADA OPERACIONAL real de BNB Smart Chain (chain_id=56, ver
    chain/networks.py) sigue DISABLED -- esto es independiente de que "evm"
    exista como identidad auth-only; ninguno de los cambios de esta fase la
    tocó."""
    assert get_network("bsc").status == NetworkStatus.DISABLED


def test_evm_auth_only_never_unlocks_operational_capability_for_any_evm_chain():
    """Confirma explicitamente (revision de seguridad 2026-09-25) que
    permitir "evm" para autenticacion NO habilita NINGUNA cadena EVM para
    ordenes, escrow, adapters, RPC ni fondos:

    1. get_chain_adapter("evm") (la ruta basada en el registry) falla --
       "evm" ni siquiera es una entrada del registry operacional.
    2. get_chain_adapter("bsc") (la cadena EVM real que Trust Wallet
       probablemente tenga seleccionada) sigue fallando por DISABLED,
       independientemente de lo anterior.
    3. build_adapter_for_network() falla cerrado para la entrada real de
       bsc, llamado directamente (sin pasar por la cache).
    4. La NetworkConfig real de bsc no tiene rpc_url NI
       escrow_contract_address poblados -- estructuralmente no hay forma
       de operar, incluso si alguien intentara construir un adapter de
       otra forma."""
    from backend.chain import build_adapter_for_network, get_chain_adapter, reset_network_adapters_for_tests

    reset_network_adapters_for_tests()

    with pytest.raises(ValueError, match="desconocido"):
        get_chain_adapter("evm")

    try:
        get_chain_adapter("bsc")
        assert False, "get_chain_adapter('bsc') no deberia poder construir un adapter -- bsc sigue DISABLED"
    except ValueError as exc:
        assert "DISABLED" in str(exc)

    try:
        build_adapter_for_network(get_network("bsc"))
        assert False, "build_adapter_for_network no deberia poder construir un adapter para bsc -- sigue DISABLED"
    except ValueError as exc:
        assert "DISABLED" in str(exc)

    bsc_config = get_network("bsc")
    assert bsc_config.rpc_url is None
    assert bsc_config.escrow_contract_address is None
    assert bsc_config.escrow_contract_abi_path is None


def test_evm_allowed_for_link_issuance_and_consumption_via_the_auth_only_gate(db_session, user):
    """El gate de linkable-network (messaging/identity.py) acepta "evm" por
    estar en la whitelist auth-only -- sin tocar el registry operacional
    (que ni siquiera tiene una entrada "evm")."""
    token = identity.issue_link_token(db_session, "wa-evm-linkable-gate", ttl_seconds=600, network_id="evm")
    link = identity.consume_link_token(db_session, token, user, network_id="evm")
    assert link.user_id == user.id


# ---------------------------------------------------------------------------
# Flujo feliz, con dos wallets/dos numeros distintos.
# ---------------------------------------------------------------------------


def test_evm_auth_full_flow_real_crypto_succeeds(client):
    account = _fresh_evm_account()
    token = _evm_login(client, account)
    assert token

    me = client.get("/me", headers=auth_headers(token))
    assert me.status_code == 200
    assert me.json()["wallet_address"] == account.address


def test_two_evm_wallets_link_to_two_distinct_whatsapp_ids(client, db_session):
    account_a = _fresh_evm_account()
    account_b = _fresh_evm_account()

    token_a = identity.issue_link_token(db_session, "wa-evm-two-a", 600, network_id="evm")
    token_b = identity.issue_link_token(db_session, "wa-evm-two-b", 600, network_id="evm")

    jwt_a = _evm_login(client, account_a)
    jwt_b = _evm_login(client, account_b)

    resp_a = client.post("/whatsapp/link", json={"link_token": token_a, "network_id": "evm"}, headers=auth_headers(jwt_a))
    assert resp_a.status_code == 200, resp_a.text
    assert resp_a.json() == {"whatsapp_id": "wa-evm-two-a", "linked": True}

    resp_b = client.post("/whatsapp/link", json={"link_token": token_b, "network_id": "evm"}, headers=auth_headers(jwt_b))
    assert resp_b.status_code == 200, resp_b.text
    assert resp_b.json() == {"whatsapp_id": "wa-evm-two-b", "linked": True}

    user_a = identity.get_linked_user(db_session, "wa-evm-two-a")
    user_b = identity.get_linked_user(db_session, "wa-evm-two-b")
    assert user_a.wallet_address == account_a.address
    assert user_b.wallet_address == account_b.address
    assert user_a.id != user_b.id


# ---------------------------------------------------------------------------
# Casos adversos pedidos explicitamente.
# ---------------------------------------------------------------------------


def test_signature_from_a_different_address_is_rejected(client):
    """Firma de otra direccion: A pide el nonce, pero quien firma es B."""
    account_a = _fresh_evm_account()
    account_b = _fresh_evm_account()
    _, message = _evm_nonce_and_message(client, account_a.address)

    wrong_signature = _sign(account_b, message)
    resp = client.post(
        "/auth/verify",
        json={"wallet_address": account_a.address, "signature": wrong_signature, "network_id": "evm"},
    )
    assert resp.status_code == 401


def test_nonce_issued_for_one_network_cannot_be_verified_under_another(client):
    """Nonce de otra red: se pide el nonce para tron, se intenta canjear
    afirmando evm para la MISMA wallet_address -- la busqueda del nonce
    pendiente filtra por network_id, asi que no encuentra nada."""
    account = _fresh_evm_account()
    client.post("/auth/nonce", json={"wallet_address": account.address, "network_id": "tron"})

    # nunca se pidio un nonce evm para esta wallet -- firmar cualquier cosa
    # y afirmar evm no puede encontrar un nonce pendiente bajo esa red.
    fake_message = auth_module.build_sign_message("0000000000000000000000000000000")
    signature = _sign(account, fake_message)
    resp = client.post(
        "/auth/verify",
        json={"wallet_address": account.address, "signature": signature, "network_id": "evm"},
    )
    assert resp.status_code == 400
    assert "nonce" in resp.json()["detail"].lower()


def test_replay_of_the_same_evm_signature_is_rejected(client):
    account = _fresh_evm_account()
    _, message = _evm_nonce_and_message(client, account.address)
    signature = _sign(account, message)

    first = client.post(
        "/auth/verify", json={"wallet_address": account.address, "signature": signature, "network_id": "evm"}
    )
    assert first.status_code == 200, first.text

    replay = client.post(
        "/auth/verify", json={"wallet_address": account.address, "signature": signature, "network_id": "evm"}
    )
    assert replay.status_code in (400, 401)
    assert "access_token" not in replay.json()


def test_network_switch_mid_flow_is_rejected(client):
    """Cambio de red durante el flujo: se pide el nonce para evm, pero se
    intenta verificar afirmando tron (para la misma wallet_address) --
    ningun nonce tron pendiente existe para esa wallet, la firma real de evm
    tampoco corresponde al mensaje que un nonce tron hubiera generado."""
    account = _fresh_evm_account()
    _, evm_message = _evm_nonce_and_message(client, account.address)
    signature = _sign(account, evm_message)

    resp = client.post(
        "/auth/verify",
        json={"wallet_address": account.address, "signature": signature, "network_id": "tron"},
    )
    assert resp.status_code == 400
    assert "nonce" in resp.json()["detail"].lower()


def test_jwt_authenticated_on_one_network_rejected_for_link_of_another(client, db_session):
    """JWT de red distinta: el JWT prueba evm, pero se intenta usar contra un
    link_token emitido (y afirmado en el body) para tron -- get_authenticated_network
    lee la red REAL del JWT, no la que el cliente afirme en el body, asi que
    esto se rechaza ANTES de siquiera mirar el link_token."""
    account = _fresh_evm_account()
    evm_jwt = _evm_login(client, account)

    tron_token = identity.issue_link_token(db_session, "wa-jwt-network-mismatch", 600, network_id="tron")
    resp = client.post(
        "/whatsapp/link",
        json={"link_token": tron_token, "network_id": "tron"},
        headers=auth_headers(evm_jwt),
    )
    assert resp.status_code == 400
    assert "network mismatch" in resp.json()["detail"]
    assert identity.get_linked_user(db_session, "wa-jwt-network-mismatch") is None


def test_evm_token_swap_between_two_identities_links_the_token_issuers_whatsapp_id(client, db_session):
    """Token intercambiado: mismo hallazgo documentado para TRON
    (test_wallet_link_two_identities_real_crypto.py) pero para evm -- el
    token pertenece al WhatsApp ID para el que se emitio, no a quien lo
    firma."""
    account_a = _fresh_evm_account()
    account_b = _fresh_evm_account()

    token_a = identity.issue_link_token(db_session, "wa-evm-swap-a", 600, network_id="evm")
    token_b = identity.issue_link_token(db_session, "wa-evm-swap-b", 600, network_id="evm")

    jwt_a = _evm_login(client, account_a)
    jwt_b = _evm_login(client, account_b)

    resp = client.post("/whatsapp/link", json={"link_token": token_a, "network_id": "evm"}, headers=auth_headers(jwt_b))
    assert resp.status_code == 200, resp.text
    assert resp.json()["whatsapp_id"] == "wa-evm-swap-a"

    resp2 = client.post("/whatsapp/link", json={"link_token": token_b, "network_id": "evm"}, headers=auth_headers(jwt_a))
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["whatsapp_id"] == "wa-evm-swap-b"

    assert identity.get_linked_user(db_session, "wa-evm-swap-a").wallet_address == account_b.address
    assert identity.get_linked_user(db_session, "wa-evm-swap-b").wallet_address == account_a.address


def test_unauthorized_network_rejected_at_nonce_even_if_it_exists_in_the_operational_registry(client):
    """Redes no autorizadas: 'ethereum' existe en chain/networks.py (DISABLED,
    con EvmEscrowAdapter implementado) pero NO esta en la whitelist
    auth-only -- /auth/nonce debe rechazarla igual que a una red inventada."""
    resp = client.post("/auth/nonce", json={"wallet_address": "0x" + "11" * 20, "network_id": "ethereum"})
    assert resp.status_code == 400


def test_completely_unknown_network_rejected_at_nonce(client):
    resp = client.post("/auth/nonce", json={"wallet_address": "0x" + "11" * 20, "network_id": "not-a-real-network"})
    assert resp.status_code == 400


def test_unauthorized_network_rejected_at_verify(client):
    account = _fresh_evm_account()
    resp = client.post(
        "/auth/verify",
        json={"wallet_address": account.address, "signature": "0x" + "00" * 65, "network_id": "ethereum"},
    )
    assert resp.status_code == 400
