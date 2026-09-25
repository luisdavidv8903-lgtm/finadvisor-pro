"""
Fase 7 del refactor multichain: registry central + dispatch por ChainFamily.
No abre ninguna conexion real (ni RPC EVM ni nodo TRON) -- build_adapter_for_network
se prueba con monkeypatch sobre las clases de adaptador, nunca construyendolas de verdad.
"""

import pytest

from backend.chain import build_adapter_for_network, get_chain_adapter, get_network
from backend.chain.networks import (
    NETWORKS,
    ChainFamily,
    NetworkConfig,
    NetworkStatus,
    SourceStatus,
    UsdtOrigin,
    get_network as networks_get_network,
    is_operational,
    list_networks,
)


def _minimal_network(**overrides) -> NetworkConfig:
    base = dict(
        network_id="test-net",
        display_name="Test Net",
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
        escrow_contract_address=None,
        escrow_contract_abi_path=None,
        confirmations_required=0,
        status=NetworkStatus.DISABLED,
        source_status=SourceStatus.UNKNOWN,
        source_reference="test fixture",
    )
    base.update(overrides)
    return NetworkConfig(**base)


_EVM_WITH_ADAPTER = {"ethereum", "avalanche", "bsc", "celo", "kava", "kaia"}
_UNIMPLEMENTED_FAMILY_NETWORKS = {"liquid", "solana", "polkadot_assethub", "tezos", "near", "ton", "aptos"}
_HISTORICAL_NETWORKS = {"algorand", "vaulta", "kusama_assethub", "omni", "bch_slp"}


# --- registry content ---


def test_registry_contains_expected_networks():
    """Fase 9 (revision): el registry representa, como minimo, todos los
    protocolos que tether.to/en/supported-protocols lista hoy como
    soportados, mas los deprecados que se conservan solo como referencia
    historica -- ver networks.py."""
    expected = {"tron"} | _EVM_WITH_ADAPTER | _UNIMPLEMENTED_FAMILY_NETWORKS | _HISTORICAL_NETWORKS
    assert set(NETWORKS) == expected


def test_only_tron_is_non_disabled_and_non_historical():
    """Ninguna red EVM (ni ninguna otra familia) queda ACTIVE/PILOT/TESTNET
    todavia -- BSC_ACTIVE=NO, etc. TRON es TESTNET; todo lo demas es
    DISABLED, salvo los 5 protocolos deprecados por Tether, que son
    HISTORICAL (nunca DISABLED-que-podria-activarse-despues)."""
    for network_id, network in NETWORKS.items():
        if network_id == "tron":
            assert network.status == NetworkStatus.TESTNET
        elif network_id in _HISTORICAL_NETWORKS:
            assert network.status == NetworkStatus.HISTORICAL, f"{network_id} deberia ser HISTORICAL"
        else:
            assert network.status == NetworkStatus.DISABLED, f"{network_id} deberia seguir DISABLED"


def test_historical_networks_never_operational():
    for network_id in _HISTORICAL_NETWORKS:
        assert not is_operational(network_id)
        with pytest.raises(ValueError, match="HISTORICAL"):
            get_chain_adapter(network_id)


def test_historical_networks_carry_no_operational_usdt_address():
    """El identificador legacy (Asset ID, contrato, etc.) se documenta en
    `notes`, nunca se carga en usdt_asset_address -- para que no se pueda
    confundir con una direccion operacional verificada."""
    for network_id in _HISTORICAL_NETWORKS:
        network = get_network(network_id)
        assert network.usdt_asset_address is None
        assert network.notes  # el identificador legacy SI queda documentado, solo que no como campo operacional


def test_every_populated_usdt_address_is_marked_verified():
    """Regla de verdad #4 (revision): ningun campo operacional queda en
    PROPOSED/INFERENCE -- si usdt_asset_address esta poblado, la entrada
    entera debe estar marcada VERIFIED_FACT (SourceStatus ya no tiene otros
    valores para datos no verificados que no sean UNKNOWN)."""
    for network in list_networks():
        if network.usdt_asset_address is not None:
            assert network.source_status == SourceStatus.VERIFIED_FACT, network.network_id


def test_bsc_usdt_is_classified_as_binance_peg_not_tether_native():
    """tether.to/en/supported-protocols NO lista una direccion USD₮ para BSC
    (solo XAU₮) -- verificado explicitamente en esta sesion. El token
    ampliamente usado como "USDT" en BSC (0x55d398...) es un puente emitido
    por Binance ("Binance Bridged USDT" / BSC-USD en CoinGecko, categoria
    Bridged-Tokens), nunca Tether-native."""
    bsc = get_network("bsc")
    assert bsc.usdt_origin == UsdtOrigin.BINANCE_PEG
    assert bsc.usdt_origin != UsdtOrigin.TETHER_NATIVE
    assert bsc.usdt_asset_address == "0x55d398326f99059fF775485246999027B3197955"
    assert bsc.usdt_decimals == 18  # verificado on-chain via eth_call decimals(), distinto de los 6 de USD₮ real


def test_tron_network_sourced_from_verified_settings():
    tron = get_network("tron")
    assert tron.chain_family == ChainFamily.TRON
    assert tron.source_status == SourceStatus.VERIFIED_FACT
    # A diferencia de la version anterior de este archivo: ahora SI hay una direccion
    # verificada (tether.to/en/supported-protocols + decimals() on-chain), no UNKNOWN.
    assert tron.usdt_asset_address == "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    assert tron.usdt_decimals == 6
    assert tron.usdt_origin == UsdtOrigin.TETHER_NATIVE


def test_unimplemented_family_networks_have_no_adapter():
    for network_id in _UNIMPLEMENTED_FAMILY_NETWORKS:
        network = get_network(network_id)
        assert network.status == NetworkStatus.DISABLED
        with pytest.raises(ValueError, match="DISABLED"):
            get_chain_adapter(network_id)  # DISABLED gate corre antes de llegar al dispatch de familia


# --- fail-closed lookups ---


def test_unknown_network_id_fails_closed():
    with pytest.raises(ValueError):
        get_network("does-not-exist")


def test_get_chain_adapter_with_unknown_network_id_fails_closed():
    with pytest.raises(ValueError):
        get_chain_adapter("does-not-exist")


def test_disabled_network_cannot_be_used_operationally():
    assert get_network("bsc").status == NetworkStatus.DISABLED
    with pytest.raises(ValueError, match="DISABLED"):
        get_chain_adapter("bsc")


# --- ACTIVE gating ---


def test_active_network_requires_complete_config():
    with pytest.raises(ValueError):
        _minimal_network(status=NetworkStatus.ACTIVE)  # falta usdt_asset_address, rpc_url, etc.


def test_active_evm_network_requires_chain_id():
    with pytest.raises(ValueError):
        _minimal_network(
            chain_id=None,
            status=NetworkStatus.ACTIVE,
            usdt_asset_address="0x000000000000000000000000000000000000dEaD",
            usdt_decimals=6,
            rpc_url="https://example.invalid",
            escrow_contract_address="0x000000000000000000000000000000000000bEEF",
            escrow_contract_abi_path="./contracts/EscrowP2P.abi.json",
        )


def test_active_network_succeeds_with_full_config():
    network = _minimal_network(
        status=NetworkStatus.ACTIVE,
        usdt_asset_address="0x000000000000000000000000000000000000dEaD",
        usdt_decimals=6,
        rpc_url="https://example.invalid",
        escrow_contract_address="0x000000000000000000000000000000000000bEEF",
        escrow_contract_abi_path="./contracts/EscrowP2P.abi.json",
    )
    assert network.status == NetworkStatus.ACTIVE


def test_disabled_network_does_not_need_any_of_the_active_fields():
    network = _minimal_network()  # todo en None/DISABLED por defecto
    assert network.status == NetworkStatus.DISABLED
    assert network.usdt_asset_address is None


# --- family dispatch (sin construir clientes reales) ---


def test_dispatch_selects_tron_adapter_for_tron_family(monkeypatch):
    sentinel = object()

    class DummyTronAdapter:
        def __init__(self):
            pass

    monkeypatch.setattr("backend.chain.tron_adapter.TronEscrowAdapter", DummyTronAdapter)
    network = _minimal_network(network_id="fake-tron", chain_family=ChainFamily.TRON, chain_id=None, status=NetworkStatus.TESTNET)
    adapter = build_adapter_for_network(network)
    assert isinstance(adapter, DummyTronAdapter)


def test_dispatch_selects_evm_adapter_for_evm_family(monkeypatch):
    calls = {}

    class DummyEvmAdapter:
        def __init__(self, network, web3_client=None):
            calls["network"] = network
            calls["web3_client"] = web3_client

        def assert_chain_id_matches(self):
            calls["chain_id_checked"] = True

    monkeypatch.setattr("backend.chain.evm_adapter.EvmEscrowAdapter", DummyEvmAdapter)
    network = _minimal_network(network_id="fake-evm", status=NetworkStatus.TESTNET)
    adapter = build_adapter_for_network(network)
    assert isinstance(adapter, DummyEvmAdapter)
    assert calls["network"] is network
    assert calls.get("chain_id_checked") is True  # build_adapter_for_network SIEMPRE valida chain_id para EVM


def test_unknown_chain_family_fails_closed():
    class NotAFamily:
        pass

    network = _minimal_network(status=NetworkStatus.TESTNET)  # TESTNET on purpose: this must fail on the family check, not on the DISABLED gate
    object.__setattr__(network, "chain_family", NotAFamily())  # frozen dataclass, solo para forzar el caso invalido
    with pytest.raises(NotImplementedError):
        build_adapter_for_network(network)


def test_direct_builder_rejects_disabled_network():
    """El gate de DISABLED vive DENTRO de build_adapter_for_network, no solo en
    get_chain_adapter() -- llamar al builder directamente (como hacen los tests
    de dispatch de arriba, pero con una red DISABLED de verdad) tambien falla
    cerrado. Cierra el bypass: antes de este test, build_adapter_for_network
    no rechazaba una red DISABLED por si sola."""
    network = _minimal_network()  # DISABLED por defecto
    assert network.status == NetworkStatus.DISABLED
    with pytest.raises(ValueError, match="DISABLED"):
        build_adapter_for_network(network)


# --- isolation between networks (item 13 del checklist de Fase 7) ---


def test_two_network_configs_do_not_leak_fields_into_each_other():
    a = _minimal_network(network_id="net-a", escrow_contract_address="0xaaaa000000000000000000000000000000000a")
    b = _minimal_network(network_id="net-b", escrow_contract_address="0xbbbb000000000000000000000000000000000b")
    assert a.escrow_contract_address != b.escrow_contract_address
    assert networks_get_network("tron").escrow_contract_address != a.escrow_contract_address
