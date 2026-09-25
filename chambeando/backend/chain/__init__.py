"""
Punto de entrada unico para obtener un EscrowChainAdapter. La logica de negocio
importa SOLO desde aqui (o desde `.adapter` para los tipos) — nunca desde
`.tron_adapter`/`.evm_adapter` directamente, y nunca `tronpy`/`web3`.

Dos rutas conviven a proposito (Fase 2 del refactor multichain):

- `get_chain_adapter()` (sin argumentos): ruta LEGADO, exactamente el mismo
  comportamiento que antes de la Fase 1 — un unico adaptador global segun
  `settings.CHAIN_ADAPTER`. auth.py/routers siguen llamandola asi hoy; no se
  tocaron para no ampliar el alcance de esta fase (ver Fase 8 del encargo).
- `get_chain_adapter(network_id)`: ruta NUEVA, basada en el NetworkRegistry
  (`.networks`) — resuelve el adaptador segun la `ChainFamily` de esa red y
  falla cerrado si la red no existe o esta DISABLED.
"""

from ..config import settings
from .adapter import ChainEvent, ChainOrder, EscrowChainAdapter
from .networks import ChainFamily, NetworkConfig, NEVER_OPERATIONAL_STATUSES, get_network

__all__ = [
    "ChainEvent",
    "ChainOrder",
    "EscrowChainAdapter",
    "get_chain_adapter",
    "build_adapter_for_network",
    "reset_network_adapters_for_tests",
]

_adapter: EscrowChainAdapter | None = None
_network_adapters: dict[str, EscrowChainAdapter] = {}


def _get_legacy_adapter() -> EscrowChainAdapter:
    global _adapter
    if _adapter is None:
        if settings.CHAIN_ADAPTER == "tron":
            from .tron_adapter import TronEscrowAdapter

            _adapter = TronEscrowAdapter()
        elif settings.CHAIN_ADAPTER == "bsc":
            # Deliberadamente no implementado en esta fase (Phase 2B): el objetivo es
            # dejar la costura lista (interfaz + separacion), no fingir soporte BSC.
            # Cuando exista, agregar chain/bsc_adapter.py y un branch aqui — nada mas
            # deberia necesitar cambiar.
            raise NotImplementedError(
                "BscEscrowAdapter no esta implementado todavia. "
                "Esta fase solo define la interfaz EscrowChainAdapter; ver chain/adapter.py."
            )
        else:
            raise ValueError(f"CHAIN_ADAPTER desconocido: {settings.CHAIN_ADAPTER!r}")
    return _adapter


def build_adapter_for_network(network: NetworkConfig, *, web3_client=None) -> EscrowChainAdapter:
    """Dispatch por ChainFamily -- un archivo nuevo (adapter de familia) es
    todo lo que hace falta para agregar una familia de cadena futura (Solana,
    TON, ...), sin tocar esta funcion salvo agregar un branch. `web3_client`
    solo existe para inyeccion en tests (ver evm_adapter.py) — el codigo de
    produccion nunca lo pasa.

    El gate de DISABLED/HISTORICAL vive AQUI, no solo en get_chain_adapter():
    esta funcion es publica (los tests de dispatch la llaman directamente para
    probar la seleccion de familia sin construir un cliente real), asi que
    si el gate viviera unicamente en el caller, cualquier otro caller futuro
    que invocara build_adapter_for_network directamente se saltaria la
    proteccion por completo. Centralizarlo aqui hace que sea imposible
    construir un adapter operacional para una red DISABLED/HISTORICAL sin
    importar por donde se llegue -- los tests de dispatch usan un
    NetworkConfig sintetico en TESTNET/PILOT, nunca DISABLED, precisamente
    para no tener que saltarse esta proteccion (ver test_multichain_registry.py).

    Para EVM ademas se valida que el `chain_id` real que reporta el RPC
    coincide con `network.chain_id` ANTES de devolver el adapter -- una
    NetworkConfig de "bsc" apuntando por error a un RPC de Ethereum falla
    cerrado aqui, nunca queda operando silenciosamente contra la red
    equivocada (ver EvmEscrowAdapter para el detalle)."""
    if network.status in NEVER_OPERATIONAL_STATUSES:
        raise ValueError(f"network {network.network_id!r} esta {network.status.value}, no puede usarse operacionalmente")
    if network.chain_family == ChainFamily.TRON:
        from .tron_adapter import TronEscrowAdapter

        return TronEscrowAdapter()
    if network.chain_family == ChainFamily.EVM:
        from .evm_adapter import EvmEscrowAdapter

        adapter = EvmEscrowAdapter(network, web3_client=web3_client)
        adapter.assert_chain_id_matches()  # falla cerrado si falta chain_id o si el RPC real no coincide
        return adapter
    raise NotImplementedError(
        f"no hay EscrowChainAdapter implementado todavia para chain_family={network.chain_family!r} "
        f"(network={network.network_id!r}) -- ver chain/networks.py, Fase 9: representacion estructural, no adapter."
    )


def get_chain_adapter(network_id: str | None = None) -> EscrowChainAdapter:
    if network_id is None:
        return _get_legacy_adapter()

    if network_id not in _network_adapters:
        network = get_network(network_id)  # falla cerrado si network_id no existe
        _network_adapters[network_id] = build_adapter_for_network(network)  # falla cerrado si esta DISABLED
    return _network_adapters[network_id]


def reset_network_adapters_for_tests() -> None:
    """Solo para tests: limpia la cache de adaptadores por-network (analogo a
    reset_chain_adapter_for_tests, pero para la ruta nueva basada en registry)."""
    _network_adapters.clear()


def reset_chain_adapter_for_tests() -> None:
    """Solo para tests: fuerza que la proxima get_chain_adapter() reconstruya el
    adaptador (util para inyectar un fake/mock entre tests)."""
    global _adapter
    _adapter = None


def set_chain_adapter_for_tests(adapter: EscrowChainAdapter) -> None:
    """Solo para tests: inyecta un EscrowChainAdapter (normalmente un fake/mock) sin
    pasar por CHAIN_ADAPTER/tronpy — asi la capa de negocio se prueba sin red ni nodo."""
    global _adapter
    _adapter = adapter
