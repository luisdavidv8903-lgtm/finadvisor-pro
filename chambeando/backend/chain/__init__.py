"""
Punto de entrada unico para obtener un EscrowChainAdapter. La logica de negocio
importa SOLO desde aqui (o desde `.adapter` para los tipos) — nunca desde
`.tron_adapter` directamente, y nunca `tronpy`.
"""

from ..config import settings
from .adapter import ChainEvent, ChainOrder, EscrowChainAdapter

__all__ = ["ChainEvent", "ChainOrder", "EscrowChainAdapter", "get_chain_adapter"]

_adapter: EscrowChainAdapter | None = None


def get_chain_adapter() -> EscrowChainAdapter:
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
