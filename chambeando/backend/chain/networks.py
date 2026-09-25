"""
Registry central de redes soportadas por Chambeando. Este modulo es la UNICA
fuente de verdad sobre "que redes existen" y "en que estado operacional esta
cada una" — ninguna otra parte del backend deberia hardcodear una lista de
redes o un chain_id.

IMPORTANTE — "SUPPORTED" no es "ACTIVE": una entrada en NETWORKS documenta que
la arquitectura sabe representar esa red, no que Chambeando ya opere sobre
ella. Casi todas las entradas de hoy son DISABLED a proposito (ver
NetworkStatus). Ninguna entrada de este archivo debe inventar un dato no
verificado (direccion de contrato, RPC, explorer, chain_id, decimals) —
donde no hay evidencia verificable, el campo es `None`. Ver
MULTICHAIN_ARCHITECTURE.md para la clasificacion completa de que esta
verificado y que sigue pendiente -- cada entrada de abajo lleva su propio
`source_reference` con la evidencia cruda (URLs, llamadas RPC) detras de
cada campo no-None.

Fuente primaria de la lista de protocolos: https://tether.to/en/supported-protocols/
(verificada en esta sesion, no copiada de memoria). Todo dato numerico/
direccion se verifico ADEMAS de forma independiente (lectura on-chain directa
via RPC publico, o un segundo registro publico como chainid.network) antes de
marcarse VERIFIED_FACT -- ver `source_reference` de cada `NetworkConfig`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..config import settings


class ChainFamily(str, Enum):
    TRON = "tron"
    EVM = "evm"
    # Familias sin EscrowChainAdapter implementado todavia -- build_adapter_for_network
    # lanza NotImplementedError para estas (nunca silenciosamente "funcionan").
    LIQUID = "liquid"
    SOLANA = "solana"
    POLKADOT_ASSETHUB = "polkadot_assethub"
    KUSAMA_ASSETHUB = "kusama_assethub"
    TEZOS = "tezos"
    NEAR = "near"
    TON = "ton"
    APTOS = "aptos"
    ALGORAND = "algorand"
    EOSIO = "eosio"  # Vaulta blockchain (renombrado desde EOS por Tether)
    OMNI = "omni"  # protocolo sobre Bitcoin
    BCH_SLP = "bch_slp"


class NetworkStatus(str, Enum):
    DISABLED = "DISABLED"  # existe en el registry, no puede usarse operacionalmente, PODRIA activarse a futuro
    TESTNET = "TESTNET"  # adaptador funcional contra un nodo de pruebas, sin contrato productivo
    PILOT = "PILOT"  # deploy real limitado, bajo supervision — fuera de alcance de esta fase
    ACTIVE = "ACTIVE"  # produccion — exige configuracion completa, ver NetworkConfig.__post_init__
    # Deprecado por el propio Tether (ver tether.to/en/supported-protocols) -- NUNCA
    # operacional, distinto de DISABLED: DISABLED significa "todavia no", HISTORICAL
    # significa "Tether ya no emite/redime aqui, esto no se va a activar nunca".
    # Conservado solo por referencia documental (ver seccion "Deprecated Asset
    # Protocols" de la fuente).
    HISTORICAL = "HISTORICAL"


class UsdtOrigin(str, Enum):
    TETHER_NATIVE = "TETHER_NATIVE"  # emitido/listado directamente por Tether (tether.to/en/supported-protocols)
    BRIDGED = "BRIDGED"
    BINANCE_PEG = "BINANCE_PEG"
    UNKNOWN = "UNKNOWN"


class SourceStatus(str, Enum):
    """Clasificacion obligatoria (regla de verdad #4 del encargo) del origen
    de los datos no-None de una NetworkConfig. Un campo en `None` es UNKNOWN
    por construccion -- no necesita "justificar" su ausencia con este enum;
    este enum describe la entrada en su conjunto (ver `notes`/`source_reference`
    para el detalle campo-por-campo)."""

    VERIFIED_FACT = "VERIFIED_FACT"  # verificado de forma independiente esta sesion (on-chain RPC y/o fuente oficial)
    UNKNOWN = "UNKNOWN"  # entrada estructural, ningun campo no-trivial verificado


_ACTIVE_REQUIRED_FIELDS = (
    "usdt_asset_address",
    "usdt_decimals",
    "rpc_url",
    "escrow_contract_address",
    "escrow_contract_abi_path",
)

# Publica a proposito: build_adapter_for_network() (chain/__init__.py) y
# _resolve_and_require_operational_network() (messaging/identity.py) importan
# ESTA tupla en vez de repetir el par (DISABLED, HISTORICAL) cada una por su
# cuenta -- un unico criterio de "nunca operacional", nunca dos que puedan
# desincronizarse (ver revision humana: identity.py rechazaba DISABLED pero
# no HISTORICAL porque tenia su propia copia del criterio).
NEVER_OPERATIONAL_STATUSES = (NetworkStatus.DISABLED, NetworkStatus.HISTORICAL)


@dataclass(frozen=True)
class NetworkConfig:
    network_id: str
    display_name: str
    chain_family: ChainFamily
    chain_id: int | None
    native_gas_token: str | None

    usdt_asset_address: str | None
    usdt_decimals: int | None
    usdt_origin: UsdtOrigin

    rpc_url: str | None
    explorer_url: str | None

    wallet_family: str
    wallet_connectors: tuple[str, ...]

    escrow_contract_address: str | None
    escrow_contract_abi_path: str | None

    confirmations_required: int
    status: NetworkStatus

    source_status: SourceStatus
    source_reference: str
    notes: str = ""

    def __post_init__(self) -> None:
        if self.chain_family == ChainFamily.EVM and self.status == NetworkStatus.ACTIVE and self.chain_id is None:
            raise ValueError(f"network {self.network_id!r}: una red EVM ACTIVE exige chain_id")
        if self.status == NetworkStatus.ACTIVE:
            missing = [f for f in _ACTIVE_REQUIRED_FIELDS if getattr(self, f) in (None, "")]
            if missing:
                raise ValueError(f"network {self.network_id!r} no puede quedar ACTIVE: faltan campos obligatorios {missing}")


# ---------------------------------------------------------------------------
# TRON -- red existente del proyecto
# ---------------------------------------------------------------------------


def _tron_network() -> NetworkConfig:
    escrow_address = settings.ESCROW_CONTRACT_ADDRESS or None
    return NetworkConfig(
        network_id="tron",
        display_name="TRON",
        chain_family=ChainFamily.TRON,
        chain_id=None,  # TRON no usa chain_id estilo EIP-155
        native_gas_token="TRX",
        # USD₮ TRC-20, direccion oficial de tether.to/en/supported-protocols, verificada
        # independientemente contra el nodo TRON real (decimals() on-chain) -- ver
        # source_reference abajo.
        usdt_asset_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        usdt_decimals=6,  # verificado on-chain: TronGrid triggersmartcontract decimals() -> 6
        usdt_origin=UsdtOrigin.TETHER_NATIVE,
        rpc_url=settings.TRON_NODE_URL,
        explorer_url="https://tronscan.org",  # tether.to enlaza tronscan.org para el token TRC-20
        wallet_family="tron",
        wallet_connectors=(),  # UNKNOWN: no existe todavia integracion de wallet-connector en el frontend
        escrow_contract_address=escrow_address,
        escrow_contract_abi_path=settings.ESCROW_CONTRACT_ABI_PATH,
        confirmations_required=settings.CONFIRMATIONS_REQUIRED,
        # TESTNET, no ACTIVE: TRON_NODE_URL por defecto es Shasta (testnet) y no hay
        # escrow_contract_address configurado (vacio en .env.example) -- no hay
        # evidencia de un deploy productivo, aunque la direccion de USDT-TRON ya esta
        # verificada.
        status=NetworkStatus.TESTNET,
        source_status=SourceStatus.VERIFIED_FACT,
        source_reference=(
            "usdt_asset_address/explorer_url: https://tether.to/en/supported-protocols/ (verificado en esta "
            "sesion). usdt_decimals: TronGrid triggersmartcontract decimals() contra el contrato real. "
            "rpc_url/escrow_contract_address/escrow_contract_abi_path/confirmations_required: "
            "backend/config.py (Settings defaults)."
        ),
        notes="Adaptador TronEscrowAdapter completo y probado (ver tests/test_tron_signature_real.py).",
    )


# ---------------------------------------------------------------------------
# EVM -- familia con adaptador implementado (EvmEscrowAdapter, una clase para todas)
# ---------------------------------------------------------------------------


def _evm_network(
    network_id: str,
    display_name: str,
    chain_id: int,
    native_gas_token: str,
    explorer_url: str,
    rpc_url_verified: str,
    usdt_asset_address: str | None,
    usdt_decimals: int | None,
    usdt_origin: UsdtOrigin,
    usdt_source_note: str,
) -> NetworkConfig:
    """Entrada EVM con chain_id/native_gas_token/explorer_url verificados
    independientemente esta sesion (eth_chainId on-chain + chainid.network
    como segunda fuente -- ver `source_reference` en la NetworkConfig
    resultante). status queda
    DISABLED a proposito: verificar metadata de red no es lo mismo que
    autorizar produccion (rpc_url NO se puebla en la NetworkConfig real
    mientras status=DISABLED -- solo se registra en `notes` que un endpoint
    publico funcional existe, para no dar la impresion de que ya esta
    'conectado')."""
    return NetworkConfig(
        network_id=network_id,
        display_name=display_name,
        chain_family=ChainFamily.EVM,
        chain_id=chain_id,
        native_gas_token=native_gas_token,
        usdt_asset_address=usdt_asset_address,
        usdt_decimals=usdt_decimals,
        usdt_origin=usdt_origin,
        rpc_url=None,  # DISABLED: no se puebla un RPC operacional hasta activacion deliberada
        explorer_url=explorer_url,
        wallet_family="evm",
        wallet_connectors=(),  # UNKNOWN: ninguna integracion de wallet-connector verificada en el frontend
        escrow_contract_address=None,
        escrow_contract_abi_path=None,
        confirmations_required=0,
        status=NetworkStatus.DISABLED,
        source_status=SourceStatus.VERIFIED_FACT,
        source_reference=(
            f"chain_id: eth_chainId via {rpc_url_verified} (RPC publico, probado esta sesion) + "
            f"cross-check contra chainid.network. native_gas_token/explorer_url: chainid.network. "
            f"usdt_asset_address/usdt_origin: {usdt_source_note}"
        ),
        notes=f"RPC publico funcional verificado esta sesion: {rpc_url_verified} (no poblado en rpc_url mientras status=DISABLED).",
    )


# ---------------------------------------------------------------------------
# Familias sin adaptador implementado -- solo representacion estructural,
# usdt_asset_address viene de tether.to/en/supported-protocols (fuente
# primaria del emisor), el resto queda None (no verificado esta sesion).
# ---------------------------------------------------------------------------


def _unimplemented_family_network(
    network_id: str,
    display_name: str,
    chain_family: ChainFamily,
    usdt_asset_address: str,
) -> NetworkConfig:
    return NetworkConfig(
        network_id=network_id,
        display_name=display_name,
        chain_family=chain_family,
        chain_id=None,
        native_gas_token=None,  # UNKNOWN: no verificado esta sesion (familia sin adaptador, fuera de alcance)
        usdt_asset_address=usdt_asset_address,
        usdt_decimals=None,  # UNKNOWN: no verificado esta sesion
        usdt_origin=UsdtOrigin.TETHER_NATIVE,  # listado directamente por Tether en su pagina oficial
        rpc_url=None,
        explorer_url=None,
        wallet_family=chain_family.value,
        wallet_connectors=(),
        escrow_contract_address=None,
        escrow_contract_abi_path=None,
        confirmations_required=0,
        status=NetworkStatus.DISABLED,
        source_status=SourceStatus.VERIFIED_FACT,
        source_reference=(
            "usdt_asset_address: https://tether.to/en/supported-protocols/ (verificado en esta sesion). "
            "Resto de campos operacionales (rpc_url/explorer_url/chain_id/native_gas_token/decimals): "
            "UNKNOWN, no verificados esta sesion -- ningun EscrowChainAdapter existe para esta familia "
            "(ver build_adapter_for_network, que lanza NotImplementedError para ella)."
        ),
        notes="Entrada estructural (Fase 9): representa el protocolo, no lo activa. Sin adaptador implementado.",
    )


def _historical_network(
    network_id: str,
    display_name: str,
    chain_family: ChainFamily,
    identifier_note: str,
) -> NetworkConfig:
    """Protocolo que el propio Tether marca como deprecated/legacy -- ver
    'Deprecated Asset Protocols' en tether.to/en/supported-protocols. status
    SIEMPRE HISTORICAL: nunca puede activarse por construccion (ningun codigo
    de este archivo pone HISTORICAL en ninguna otra red, y NetworkStatus no
    tiene una via de 'ascender' de HISTORICAL a otro estado)."""
    return NetworkConfig(
        network_id=network_id,
        display_name=display_name,
        chain_family=chain_family,
        chain_id=None,
        native_gas_token=None,
        usdt_asset_address=None,  # no se registra el identificador legacy como "direccion operacional" -- ver notes
        usdt_decimals=None,
        usdt_origin=UsdtOrigin.UNKNOWN,
        rpc_url=None,
        explorer_url=None,
        wallet_family=chain_family.value,
        wallet_connectors=(),
        escrow_contract_address=None,
        escrow_contract_abi_path=None,
        confirmations_required=0,
        status=NetworkStatus.HISTORICAL,
        source_status=SourceStatus.VERIFIED_FACT,
        source_reference="https://tether.to/en/supported-protocols/ , seccion 'Deprecated Asset Protocols' (verificado en esta sesion).",
        notes=(
            f"Tether ya no emite ni esta obligado a redimir USD₮ aqui (ver aviso oficial de transicion). "
            f"Identificador legacy documental (NO cargado como usdt_asset_address operacional): {identifier_note}."
        ),
    )


def _build_registry() -> dict[str, NetworkConfig]:
    networks = [
        _tron_network(),
        # --- EVM, con adaptador (EvmEscrowAdapter) ---
        _evm_network(
            "ethereum", "Ethereum", chain_id=1, native_gas_token="ETH",
            explorer_url="https://etherscan.io", rpc_url_verified="https://ethereum-rpc.publicnode.com",
            usdt_asset_address="0xdAC17F958D2ee523a2206206994597C13D831Ec7", usdt_decimals=6,
            usdt_origin=UsdtOrigin.TETHER_NATIVE,
            usdt_source_note="tether.to/en/supported-protocols (direccion oficial) + decimals() on-chain via eth_call.",
        ),
        _evm_network(
            "avalanche", "Avalanche C-Chain", chain_id=43114, native_gas_token="AVAX",
            explorer_url="https://snowtrace.io", rpc_url_verified="https://api.avax.network/ext/bc/C/rpc",
            usdt_asset_address="0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7", usdt_decimals=6,
            usdt_origin=UsdtOrigin.TETHER_NATIVE,
            usdt_source_note="tether.to/en/supported-protocols (direccion oficial) + decimals() on-chain via eth_call.",
        ),
        _evm_network(
            "bsc", "BNB Smart Chain", chain_id=56, native_gas_token="BNB",
            explorer_url="https://bscscan.com", rpc_url_verified="https://bsc-dataseed.binance.org",
            # tether.to/en/supported-protocols NO lista una direccion USD₮ para BSC (solo XAU₮) --
            # verificado explicitamente en esta sesion, ver usdt_source_note abajo. El token
            # ampliamente usado como "USDT" en BSC es un puente de Binance, no una emision de Tether:
            # CoinGecko lo cataloga como "Binance Bridged USDT (BNB Smart Chain)" / ticker BSC-USD,
            # categoria "Bridged-Tokens". decimals()=18 confirmado on-chain via eth_call contra
            # bsc-dataseed.binance.org.
            usdt_asset_address="0x55d398326f99059fF775485246999027B3197955", usdt_decimals=18,
            usdt_origin=UsdtOrigin.BINANCE_PEG,
            usdt_source_note=(
                "tether.to/en/supported-protocols NO lista USD₮ para BSC (solo XAU₮) -- ausencia verificada "
                "explicitamente. Direccion/naturaleza 'Binance Bridged USDT': "
                "https://www.coingecko.com/en/coins/binance-bridged-usdt-bnb-smart-chain (categoria "
                "'Bridged-Tokens'). decimals()=18 verificado on-chain via eth_call directo al contrato."
            ),
        ),
        _evm_network(
            "celo", "Celo", chain_id=42220, native_gas_token="CELO",
            explorer_url="https://celoscan.io", rpc_url_verified="https://forno.celo.org",
            usdt_asset_address="0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e", usdt_decimals=6,
            usdt_origin=UsdtOrigin.TETHER_NATIVE,
            usdt_source_note="tether.to/en/supported-protocols (direccion oficial) + decimals() on-chain via eth_call.",
        ),
        _evm_network(
            "kava", "Cosmos (via Kava)", chain_id=2222, native_gas_token="KAVA",
            explorer_url="https://kavascan.com", rpc_url_verified="https://evm.kava.io",
            usdt_asset_address="0x919C1c267BC06a7039e03fcc2eF738525769109c", usdt_decimals=6,
            usdt_origin=UsdtOrigin.TETHER_NATIVE,
            usdt_source_note="tether.to/en/supported-protocols (direccion oficial) + decimals() on-chain via eth_call.",
        ),
        _evm_network(
            "kaia", "Kaia", chain_id=8217, native_gas_token="KAIA",
            explorer_url="https://kaiascan.io", rpc_url_verified="https://public-en.node.kaia.io",
            usdt_asset_address="0xd077a400968890eacc75cdc901f0356c943e4fdb", usdt_decimals=6,
            usdt_origin=UsdtOrigin.TETHER_NATIVE,
            usdt_source_note="tether.to/en/supported-protocols (direccion oficial) + decimals() on-chain via eth_call.",
        ),
        # --- familias sin adaptador implementado (representacion estructural, Fase 9) ---
        _unimplemented_family_network(
            "liquid", "Liquid", ChainFamily.LIQUID,
            usdt_asset_address="ce091c998b83c78bb71a632313ba3760f1763d9cfcffae02258ffa9865a37bd2",
        ),
        _unimplemented_family_network(
            "solana", "Solana", ChainFamily.SOLANA,
            usdt_asset_address="Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        ),
        _unimplemented_family_network(
            "polkadot_assethub", "Polkadot AssetHub", ChainFamily.POLKADOT_ASSETHUB,
            usdt_asset_address="asset-id:1984",  # Polkadot AssetHub identifica assets por ID, no por direccion
        ),
        _unimplemented_family_network(
            "tezos", "Tezos", ChainFamily.TEZOS,
            usdt_asset_address="KT1XnTn74bUtxHfDtBmm2bGZAQfhPbvKWR8o:0",  # contrato + token id, formato Tezos FA
        ),
        _unimplemented_family_network(
            "near", "NEAR", ChainFamily.NEAR,
            usdt_asset_address="usdt.tether-token.near",
        ),
        _unimplemented_family_network(
            "ton", "TON", ChainFamily.TON,
            usdt_asset_address="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        ),
        _unimplemented_family_network(
            "aptos", "Aptos", ChainFamily.APTOS,
            usdt_asset_address="0xf73e887a8754f540ee6e1a93bdc6dde2af69fc7ca5de32013e89dd44244473cb",
        ),
        # --- historico/deprecado por el propio Tether -- NUNCA operacional ---
        _historical_network(
            "algorand", "Algorand", ChainFamily.ALGORAND,
            identifier_note="Asset ID 312769",
        ),
        _historical_network(
            "vaulta", "Vaulta (previously EOS)", ChainFamily.EOSIO,
            identifier_note="contrato tethertether, simbolo USDT",
        ),
        _historical_network(
            "kusama_assethub", "Kusama AssetHub (formerly Statemine)", ChainFamily.KUSAMA_ASSETHUB,
            identifier_note="Asset ID 1984",
        ),
        _historical_network(
            "omni", "Omni Protocol (via Bitcoin)", ChainFamily.OMNI,
            identifier_note="Omni Property ID USD₮ #31",
        ),
        _historical_network(
            "bch_slp", "SLP (via Bitcoin Cash)", ChainFamily.BCH_SLP,
            identifier_note="SLP Token ID, ver tether.to para la transaccion de genesis",
        ),
    ]
    return {n.network_id: n for n in networks}


NETWORKS: dict[str, NetworkConfig] = _build_registry()


def get_network(network_id: str) -> NetworkConfig:
    """Falla cerrado: un network_id desconocido lanza, nunca devuelve un default."""
    try:
        return NETWORKS[network_id]
    except KeyError:
        raise ValueError(f"network_id desconocido: {network_id!r}") from None


def list_networks(status: NetworkStatus | None = None) -> list[NetworkConfig]:
    values = list(NETWORKS.values())
    if status is not None:
        values = [n for n in values if n.status == status]
    return values


def is_operational(network_id: str) -> bool:
    """DISABLED y HISTORICAL nunca son operacionales -- ver NetworkStatus."""
    return get_network(network_id).status not in NEVER_OPERATIONAL_STATUSES
