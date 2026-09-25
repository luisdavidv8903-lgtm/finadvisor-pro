"""
Fase 4/7 del refactor multichain: firma EVM real (eth_account, claves
sinteticas generadas al vuelo -- nunca una clave con valor real, mismo
principio que tests/test_tron_signature_real.py) y EvmEscrowAdapter contra un
cliente web3 FALSO (ninguna llamada de red real -- Fase 3 del encargo).
"""

import pytest

eth_account = pytest.importorskip("eth_account", reason="eth-account not installed in this environment")

from eth_account import Account  # noqa: E402

from backend.chain.adapter import EscrowChainAdapter  # noqa: E402
from backend.chain.evm_adapter import EvmEscrowAdapter, verify_evm_signature  # noqa: E402
from backend.chain.networks import ChainFamily, NetworkStatus, SourceStatus, UsdtOrigin, NetworkConfig  # noqa: E402

ABI_PATH = "./contracts/EscrowP2P.abi.json"  # cualquier JSON valido sirve: el contrato falso no lo usa para nada


def _addr(suffix: str) -> str:
    """Direccion EVM sintetica valida (40 hex chars) -- `suffix` rellenado con ceros a la izquierda."""
    return "0x" + suffix.rjust(40, "0")


def _network(**overrides) -> NetworkConfig:
    base = dict(
        network_id="test-evm",
        display_name="Test EVM",
        chain_family=ChainFamily.EVM,
        chain_id=1337,
        native_gas_token="TEST",
        usdt_asset_address=None,
        usdt_decimals=None,
        usdt_origin=UsdtOrigin.UNKNOWN,
        rpc_url=None,
        explorer_url=None,
        wallet_family="evm",
        wallet_connectors=(),
        escrow_contract_address=_addr("bEEF"),
        escrow_contract_abi_path=ABI_PATH,
        confirmations_required=3,
        status=NetworkStatus.TESTNET,
        source_status=SourceStatus.UNKNOWN,
        source_reference="test fixture",
    )
    base.update(overrides)
    return NetworkConfig(**base)


class _FakeContractFunctions:
    def __init__(self, order_tuple):
        self._order_tuple = order_tuple

    def getOrder(self, order_id):
        class _Call:
            def __init__(self, value):
                self._value = value

            def call(self_inner):
                return self_inner._value

        return _Call(self._order_tuple)


class _FakeTxHash:
    """Imita hexbytes.HexBytes lo justo -- .hex() es todo lo que EvmEscrowAdapter usa."""

    def __init__(self, value: str):
        self._value = value

    def hex(self):
        return self._value


class _FakeEventFactory:
    def __init__(self, logs: list[dict]):
        self._logs = logs

    def get_logs(self, from_block: int, to_block: int):
        return [log for log in self._logs if from_block <= log["blockNumber"] <= to_block]


class _FakeEvents:
    """logs_by_event: {event_name: [log_dict, ...]}, cada log_dict con las
    mismas claves que un log real de web3.py: args, blockNumber, transactionHash."""

    def __init__(self, logs_by_event: dict[str, list[dict]] | None):
        self._logs_by_event = logs_by_event or {}

    def __getattr__(self, event_name: str):
        logs = self._logs_by_event.get(event_name, [])
        return lambda: _FakeEventFactory(logs)


class _FakeContract:
    def __init__(self, order_tuple, logs_by_event=None):
        self.functions = _FakeContractFunctions(order_tuple)
        self.events = _FakeEvents(logs_by_event)


class _FakeEth:
    def __init__(self, block_number, contract_obj, receipts=None, chain_id=None):
        self.block_number = block_number
        self.chain_id = chain_id
        self._contract_obj = contract_obj
        self._receipts = receipts or {}

    def contract(self, address, abi):
        return self._contract_obj

    def get_transaction_receipt(self, tx_hash):
        return self._receipts.get(tx_hash)


class _FakeW3:
    def __init__(self, block_number=12345, order_tuple=None, receipts=None, logs_by_event=None, chain_id=None):
        contract_obj = _FakeContract(order_tuple, logs_by_event) if (order_tuple is not None or logs_by_event is not None) else None
        self.eth = _FakeEth(block_number, contract_obj, receipts, chain_id=chain_id)


def _make_adapter(order_tuple=None, block_number=12345, receipts=None, network=None, logs_by_event=None, chain_id=None):
    fake_w3 = _FakeW3(block_number=block_number, order_tuple=order_tuple, receipts=receipts, logs_by_event=logs_by_event, chain_id=chain_id)
    return EvmEscrowAdapter(network or _network(), web3_client=fake_w3)


# --- verify_evm_signature: vectores puros, misma forma que test_tron_signature_real.py ---


@pytest.fixture()
def synthetic_keypair():
    acct = Account.create()
    return acct, acct.address


def test_correct_address_verifies(synthetic_keypair):
    acct, address = synthetic_keypair
    message = "Chambeando login: deadbeefcafef00d0123456789abcde"
    signed = Account.sign_message(eth_account.messages.encode_defunct(text=message), private_key=acct.key)
    assert verify_evm_signature(address, message, signed.signature.hex()) is True


def test_wrong_address_fails(synthetic_keypair):
    acct, address = synthetic_keypair
    other = Account.create()
    message = "Chambeando login: deadbeefcafef00d0123456789abcde"
    signed = Account.sign_message(eth_account.messages.encode_defunct(text=message), private_key=acct.key)
    assert verify_evm_signature(other.address, message, signed.signature.hex()) is False


def test_modified_message_fails(synthetic_keypair):
    acct, address = synthetic_keypair
    message = "Chambeando login: deadbeefcafef00d0123456789abcde"
    signed = Account.sign_message(eth_account.messages.encode_defunct(text=message), private_key=acct.key)
    assert verify_evm_signature(address, message + " extra", signed.signature.hex()) is False


def test_malformed_signature_fails(synthetic_keypair):
    _, address = synthetic_keypair
    message = "Chambeando login: deadbeefcafef00d0123456789abcde"
    assert verify_evm_signature(address, message, "not-hex-at-all") is False
    assert verify_evm_signature(address, message, "0xdeadbeef") is False  # hex valido pero muy corto
    assert verify_evm_signature(address, message, "") is False


def test_signature_verifies_regardless_of_address_casing(synthetic_keypair):
    acct, address = synthetic_keypair
    message = "Chambeando login: deadbeefcafef00d0123456789abcde"
    signed = Account.sign_message(eth_account.messages.encode_defunct(text=message), private_key=acct.key)
    assert verify_evm_signature(address.lower(), message, signed.signature.hex()) is True


# --- EvmEscrowAdapter, contrato/cliente falsos (cero red real) ---


def test_evm_adapter_implements_the_shared_interface():
    assert isinstance(_make_adapter(), EscrowChainAdapter)


def test_normalize_address_checksums_valid_addresses():
    adapter = _make_adapter()
    lower = _addr("beef").lower()
    assert adapter.normalize_address(lower).startswith("0x")


def test_normalize_address_rejects_malformed_addresses():
    adapter = _make_adapter()
    with pytest.raises(ValueError):
        adapter.normalize_address("not-an-address")


def test_verify_wallet_signature_delegates_to_verify_evm_signature(synthetic_keypair):
    acct, address = synthetic_keypair
    adapter = _make_adapter()
    message = "Chambeando login: deadbeefcafef00d0123456789abcde"
    signed = Account.sign_message(eth_account.messages.encode_defunct(text=message), private_key=acct.key)
    assert adapter.verify_wallet_signature(address, message, signed.signature.hex()) is True
    assert adapter.verify_wallet_signature(address, message, "garbage") is False


def test_get_order_reads_struct_fields_in_declared_order():
    order_tuple = (
        "0x1111111111111111111111111111111111111a",  # seller
        "0x2222222222222222222222222222222222222b",  # buyer
        "0x3333333333333333333333333333333333333c",  # token
        1_000_000,  # amount
        0,  # createdAt
        0,  # claimedAt
        0,  # paidAt
        0,  # feeBpsSnapshot
        "0x0000000000000000000000000000000000000d",  # feeCollectorSnapshot
        "0x4444444444444444444444444444444444444e",  # arbiterSnapshot
        1,  # status -> CLAIMED
    )
    adapter = _make_adapter(order_tuple=order_tuple)
    order = adapter.get_order(7)
    assert order.onchain_order_id == 7
    assert order.seller == order_tuple[0]
    assert order.token == order_tuple[2]
    assert order.amount == 1_000_000
    assert order.status == "CLAIMED"
    assert order.arbiter_snapshot == order_tuple[9]
    assert adapter.get_status(7) == "CLAIMED"


def test_current_block_and_confirmations_use_the_injected_client():
    adapter = _make_adapter(block_number=100)
    assert adapter.current_block() == 100
    assert adapter.get_confirmations(90) == 10
    assert adapter.get_confirmations(150) == 0  # nunca negativo


def test_verify_transaction_true_only_for_status_1():
    adapter = _make_adapter(receipts={"0xok": {"status": 1}, "0xbad": {"status": 0}})
    assert adapter.verify_transaction("0xok") is True
    assert adapter.verify_transaction("0xbad") is False
    assert adapter.verify_transaction("0xmissing") is False


def test_contract_identity_is_namespaced_by_network():
    network_a = _network(network_id="bsc-test", escrow_contract_address=_addr("aaa"))
    network_b = _network(network_id="ethereum-test", escrow_contract_address=_addr("bbb"))
    adapter_a = _make_adapter(network=network_a)
    adapter_b = _make_adapter(network=network_b)
    assert adapter_a.get_contract_identity() != adapter_b.get_contract_identity()
    assert "bsc-test" in adapter_a.get_contract_identity()
    assert "ethereum-test" in adapter_b.get_contract_identity()


def test_adapter_construction_without_rpc_url_or_client_fails_closed():
    network = _network(rpc_url=None)
    with pytest.raises(ValueError):
        EvmEscrowAdapter(network)  # sin web3_client inyectado y sin rpc_url -> no hay como construir un cliente real


# --- get_events(): logs falsos, cero red real (item 9 de la revision humana) ---


def test_get_events_maps_name_order_id_block_tx_hash_and_args():
    logs_by_event = {
        "OrderCreated": [
            {
                "args": {"orderId": 3, "seller": _addr("aaa"), "token": _addr("bbb"), "amount": 500, "feeBpsSnapshot": 0},
                "blockNumber": 101,
                "transactionHash": _FakeTxHash("0xcreate3"),
            }
        ],
        "OrderClaimed": [
            {
                "args": {"orderId": 3, "buyer": _addr("ccc"), "arbiterSnapshot": _addr("ddd")},
                "blockNumber": 102,
                "transactionHash": _FakeTxHash("0xclaim3"),
            }
        ],
    }
    adapter = _make_adapter(logs_by_event=logs_by_event)
    events = adapter.get_events(0, 1000)

    assert [e.name for e in events] == ["OrderCreated", "OrderClaimed"]  # ver ordering abajo
    created, claimed = events

    assert created.order_id == 3
    assert created.block_number == 101
    assert created.tx_hash == "0xcreate3"
    assert created.data["seller"] == _addr("aaa")
    assert created.data["amount"] == 500

    assert claimed.order_id == 3
    assert claimed.block_number == 102
    assert claimed.tx_hash == "0xclaim3"
    assert claimed.data["buyer"] == _addr("ccc")


def test_get_events_orders_by_block_number_regardless_of_event_type_order():
    logs_by_event = {
        "OrderSettled": [
            {"args": {"orderId": 1}, "blockNumber": 500, "transactionHash": _FakeTxHash("0xsettle1")},
        ],
        "OrderCreated": [
            {"args": {"orderId": 1}, "blockNumber": 100, "transactionHash": _FakeTxHash("0xcreate1")},
        ],
        "OrderClaimed": [
            {"args": {"orderId": 1}, "blockNumber": 300, "transactionHash": _FakeTxHash("0xclaim1")},
        ],
    }
    adapter = _make_adapter(logs_by_event=logs_by_event)
    events = adapter.get_events(0, 1000)
    assert [e.block_number for e in events] == [100, 300, 500]
    assert [e.name for e in events] == ["OrderCreated", "OrderClaimed", "OrderSettled"]


def test_get_events_respects_block_range():
    logs_by_event = {
        "OrderCreated": [
            {"args": {"orderId": 1}, "blockNumber": 50, "transactionHash": _FakeTxHash("0xbefore")},
            {"args": {"orderId": 2}, "blockNumber": 150, "transactionHash": _FakeTxHash("0xinside")},
            {"args": {"orderId": 3}, "blockNumber": 250, "transactionHash": _FakeTxHash("0xafter")},
        ],
    }
    adapter = _make_adapter(logs_by_event=logs_by_event)
    events = adapter.get_events(100, 200)
    assert [e.tx_hash for e in events] == ["0xinside"]


def test_get_events_returns_empty_list_when_no_logs_at_all():
    adapter = _make_adapter(logs_by_event={})
    assert adapter.get_events(0, 1000) == []


# --- RPC chain_id fail-closed (item 7 original + revision humana 2026-09-25) ---


def _active_network(**overrides) -> NetworkConfig:
    """Variante ACTIVE de _network() -- __post_init__ exige usdt_asset_address/
    usdt_decimals/rpc_url ademas de escrow_contract_address/_abi_path (que
    _network() ya pone por defecto)."""
    base = dict(
        status=NetworkStatus.ACTIVE,
        usdt_asset_address=_addr("dEaD"),
        usdt_decimals=6,
        rpc_url="https://example.invalid",
    )
    base.update(overrides)
    return _network(**base)


@pytest.mark.parametrize("status", [NetworkStatus.TESTNET, NetworkStatus.PILOT])
def test_assert_chain_id_matches_passes_when_rpc_agrees(status):
    network = _network(chain_id=1337, status=status)
    adapter = _make_adapter(network=network, chain_id=1337)
    adapter.assert_chain_id_matches()  # no debe lanzar


def test_assert_chain_id_matches_passes_for_active_network_with_matching_chain_id():
    network = _active_network(chain_id=1337)
    adapter = _make_adapter(network=network, chain_id=1337)
    adapter.assert_chain_id_matches()  # no debe lanzar


@pytest.mark.parametrize("status", [NetworkStatus.TESTNET, NetworkStatus.PILOT])
def test_assert_chain_id_matches_fails_closed_on_mismatch(status):
    """Una NetworkConfig de 'bsc' (chain_id=56) apuntando por error a un RPC
    de otra red (p.ej. Ethereum, chain_id=1) debe fallar cerrado -- nunca
    operar silenciosamente contra la red equivocada."""
    network = _network(network_id="bsc", chain_id=56, status=status)
    adapter = _make_adapter(network=network, chain_id=1)  # RPC real reporta chain_id=1 (Ethereum), no 56
    with pytest.raises(ValueError, match="chain_id mismatch"):
        adapter.assert_chain_id_matches()


def test_assert_chain_id_matches_fails_closed_on_mismatch_for_active_network():
    network = _active_network(network_id="bsc", chain_id=56)
    adapter = _make_adapter(network=network, chain_id=1)
    with pytest.raises(ValueError, match="chain_id mismatch"):
        adapter.assert_chain_id_matches()


@pytest.mark.parametrize("status", [NetworkStatus.TESTNET, NetworkStatus.PILOT])
def test_assert_chain_id_matches_fails_closed_when_network_chain_id_is_none(status):
    """Revision humana 2026-09-25: una red EVM operacional (TESTNET/PILOT/
    ACTIVE) sin chain_id verificado debe fallar cerrado, NUNCA dejar pasar
    "no hay nada contra que comparar" -- version anterior de este test
    (test_assert_chain_id_matches_is_a_noop_when_network_chain_id_is_none)
    afirmaba exactamente el comportamiento contrario, ya corregido."""
    network = _network(chain_id=None, status=status)
    adapter = _make_adapter(network=network, chain_id=999)  # cualquier chain_id real -- no debe llegar a compararse
    with pytest.raises(ValueError, match="no tiene chain_id configurado"):
        adapter.assert_chain_id_matches()


def test_active_evm_network_cannot_even_be_constructed_without_chain_id():
    """NetworkConfig.__post_init__ ya bloquea esto en la capa de datos --
    una red EVM ACTIVE sin chain_id no llega ni a existir en memoria, asi
    que assert_chain_id_matches() nunca puede recibir esa combinacion para
    ACTIVE (a diferencia de TESTNET/PILOT, que si lo permiten a nivel de
    dataclass y por eso SI se prueban arriba)."""
    with pytest.raises(ValueError, match="ACTIVE exige chain_id"):
        _active_network(chain_id=None)


def test_disabled_network_is_never_forced_to_have_chain_id():
    """La representacion estructural de una red DISABLED sigue sin exigir
    chain_id -- NetworkConfig no lanza al construirse (assert_chain_id_matches
    nunca se llama para DISABLED en la ruta real, porque build_adapter_for_network
    rechaza DISABLED antes; esto solo confirma que construir la NetworkConfig
    en si misma sigue funcionando sin chain_id)."""
    network = _network(chain_id=None, status=NetworkStatus.DISABLED)
    assert network.chain_id is None
    assert network.status == NetworkStatus.DISABLED
