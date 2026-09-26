"""
Auditoria de seguridad pedida explicitamente (2026-09-25): "_require_linked_member
solo protege comandos de WhatsApp. Reporta cualquier ruta HTTP que permita
iniciar acciones de ordenes o escrow con esa identidad y cierrala si existe."

Renombrado de "bsc" a "evm" (revision posterior, ver
backend/chain/auth_networks.py:EVM_AUTH_ONLY_LIMITATIONS): la identidad
auth-only EVM nunca afirma ser una cadena especifica.

Hallazgo (ver bundles de revision para el reporte completo): NINGUN
endpoint necesito cerrarse. routers/orders.py, routers/disputes.py y
routers/settlement.py gatean toda accion sensible sobre una orden real
mediante `is_order_party`/`is_authorized_arbitrator`
(services/authorization.py), que comparan `current_user.wallet_address`
EXACTAMENTE contra `P2POrderDB.seller_wallet`/`buyer_wallet`/
`arbiter_snapshot_wallet` -- valores que el indexer SOLO escribe a partir de
eventos on-chain TRON reales (formato base58 'T...'). Una wallet evm
autenticada (formato hex '0x...') nunca puede ser igual-string a una
direccion TRON real -- estructuralmente, no por un chequeo de red
explicito, pero con el mismo efecto: fail-closed. Este archivo lo prueba
con una identidad evm real, MEMBER incluido, contra una orden TRON real.

`list_orders`/`get_order` (lectura del marketplace, sin mask de wallet
propia) y el CRUD de `settlement-details` (instrucciones de pago propias,
nunca ligadas a una orden hasta attach_order_metadata) quedan accesibles a
cualquier member sea cual sea su red -- deliberadamente NO se bloquean aca:
ninguno de los dos inicia una accion de orden/escrow, son operaciones
self-service o de solo lectura sin riesgo de identidad cruzada.
"""

from decimal import Decimal

from eth_account import Account
from eth_account.messages import encode_defunct

from backend.models import MemberRole, MembershipDB, MembershipStatus, OrderStatus, P2POrderDB, UserDB

from .conftest import auth_headers


def _evm_authenticated_member(client, db_session):
    account = Account.create()
    nonce_resp = client.post("/auth/nonce", json={"wallet_address": account.address, "network_id": "evm"})
    assert nonce_resp.status_code == 200, nonce_resp.text
    message = nonce_resp.json()["message"]
    signature = Account.sign_message(encode_defunct(text=message), private_key=account.key).signature.hex()
    verify_resp = client.post(
        "/auth/verify", json={"wallet_address": account.address, "signature": signature, "network_id": "evm"}
    )
    assert verify_resp.status_code == 200, verify_resp.text
    token = verify_resp.json()["access_token"]

    user = db_session.query(UserDB).filter(UserDB.wallet_address == account.address, UserDB.network_id == "evm").first()
    db_session.add(MembershipDB(user_id=user.id, role=MemberRole.MEMBER, status=MembershipStatus.ACTIVE))
    db_session.commit()
    return token, user


def _real_tron_order(db_session, status: OrderStatus = OrderStatus.OPEN) -> P2POrderDB:
    order = P2POrderDB(
        onchain_order_id=999001,
        token_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        seller_wallet="TRealSellerWalletFromIndexer001",
        buyer_wallet=None,
        crypto_amount=Decimal("100"),
        onchain_status=status,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)
    return order


def test_evm_member_cannot_attach_order_metadata_to_a_real_tron_order(client, db_session):
    """orders.py: attach_order_metadata es la UNICA via HTTP que adjunta
    datos a una orden ya indexada -- gateada por igualdad exacta de
    wallet_address contra el vendedor real on-chain."""
    token, _user = _evm_authenticated_member(client, db_session)
    order = _real_tron_order(db_session)

    resp = client.post(
        "/orders/metadata",
        json={
            "onchain_order_id": order.onchain_order_id,
            "fiat_amount": "1000",
            "fiat_currency": "CUP",
            "payment_method": "cup_transfer",
        },
        headers=auth_headers(token),
    )
    assert resp.status_code == 403
    assert "vendedor" in resp.json()["detail"].lower()

    # confirmado: la orden real sigue sin metadata adjunta, sin cambios
    db_session.refresh(order)
    assert order.fiat_amount is None
    assert order.created_by_user_id is None


def test_evm_member_cannot_submit_dispute_evidence_for_a_real_tron_order(client, db_session):
    token, _user = _evm_authenticated_member(client, db_session)
    order = _real_tron_order(db_session, status=OrderStatus.DISPUTED)

    resp = client.post(
        "/disputes/evidence",
        json={"order_id": order.id, "evidence_type": "note", "note": "intento de una identidad evm"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 403
    assert "parte" in resp.json()["detail"].lower()


def test_evm_member_cannot_view_dispute_evidence_of_a_real_tron_order(client, db_session):
    token, _user = _evm_authenticated_member(client, db_session)
    order = _real_tron_order(db_session, status=OrderStatus.DISPUTED)

    resp = client.get(f"/disputes/{order.id}/evidence", headers=auth_headers(token))
    assert resp.status_code == 403


def test_evm_member_cannot_view_dispute_evidence_content_of_a_real_tron_order(client, db_session):
    """Aisla get_evidence_content (ruta distinta de list_evidence) --
    inserta una fila de evidencia directamente (como si otra parte
    legitima la hubiera subido) e intenta leerla con la identidad evm."""
    from backend.models import DisputeEvidenceDB

    token, evm_user = _evm_authenticated_member(client, db_session)
    order = _real_tron_order(db_session, status=OrderStatus.DISPUTED)

    seller = UserDB(wallet_address=order.seller_wallet, network_id="tron")
    db_session.add(seller)
    db_session.commit()
    evidence = DisputeEvidenceDB(order_id=order.id, submitted_by_user_id=seller.id, evidence_type="note", note="evidencia real")
    db_session.add(evidence)
    db_session.commit()
    db_session.refresh(evidence)

    resp = client.get(f"/disputes/evidence/{evidence.id}/content", headers=auth_headers(token))
    assert resp.status_code in (403, 404)


def test_evm_member_cannot_reveal_settlement_of_a_real_tron_order(client, db_session):
    token, _user = _evm_authenticated_member(client, db_session)
    order = _real_tron_order(db_session)

    resp = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(token))
    assert resp.status_code == 403


def test_evm_member_cannot_act_as_arbitrator_on_a_real_tron_order(client, db_session):
    """is_authorized_arbitrator compara wallet_address contra
    arbiter_snapshot_wallet -- mismo principio, probado aparte porque usa
    una rama de codigo distinta de is_order_party."""
    token, _user = _evm_authenticated_member(client, db_session)
    order = _real_tron_order(db_session, status=OrderStatus.DISPUTED)
    order.arbiter_snapshot_wallet = "TRealArbiterWalletFromIndexer01"
    db_session.commit()

    resp = client.get(f"/orders/{order.id}/settlement", headers=auth_headers(token))
    assert resp.status_code == 403


def test_evm_member_read_only_marketplace_access_is_unaffected_and_harmless(client, db_session):
    """Control: list_orders/get_order (lectura publica del marketplace,
    sin exponer wallets ajenas completas) siguen funcionando para
    cualquier member -- confirma que la auditoria no rompio acceso
    legitimo de solo lectura, que no es una accion de orden/escrow."""
    token, _user = _evm_authenticated_member(client, db_session)
    order = _real_tron_order(db_session)

    list_resp = client.get("/orders/", headers=auth_headers(token))
    assert list_resp.status_code == 200
    assert any(o["id"] == order.id for o in list_resp.json())

    get_resp = client.get(f"/orders/{order.id}", headers=auth_headers(token))
    assert get_resp.status_code == 200
    assert "seller_wallet_masked" in get_resp.json()
    assert order.seller_wallet not in str(get_resp.json())  # nunca la wallet completa
