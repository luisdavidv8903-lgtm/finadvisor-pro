"""
Interfaz chain-agnostic hacia el contrato de escrow. NINGUN modulo fuera de una
implementacion concreta de EscrowChainAdapter puede importar una libreria
especifica de una cadena (p.ej. `tronpy`) — ver tron_adapter.py como la UNICA
excepcion en todo el backend. La logica de negocio (membership, reputation,
visibility, settlement, routers) solo conoce este archivo.

Hoy solo existe TronEscrowAdapter. BscEscrowAdapter queda deliberadamente sin
implementar en esta fase (ver __init__.py) — el objetivo de este modulo es
unicamente dejar la costura lista para que agregarlo despues sea un archivo
nuevo, no una reescritura de la capa de negocio.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChainEvent:
    """Evento on-chain, ya traducido a un formato neutral (ni tronpy ni web3)."""

    name: str
    order_id: int
    block_number: int
    tx_hash: str
    data: dict[str, Any]


@dataclass(frozen=True)
class ChainOrder:
    """Espejo chain-agnostic de la struct `Order` de EscrowP2P.sol."""

    onchain_order_id: int
    seller: str
    buyer: str
    token: str
    amount: int
    status: str  # nombre del enum on-chain tal cual: "OPEN", "CLAIMED", "PAID", ...
    arbiter_snapshot: str


class EscrowChainAdapter(ABC):
    """Puerto que toda la logica de negocio usa para leer la cadena. Es de SOLO
    LECTURA a proposito: ninguna transaccion de negocio (crear orden, reclamar,
    confirmar pago, liberar, votar disputa) la firma nunca el backend — esas
    siempre las firma el usuario desde su propia wallet (ver chambeando/README.md,
    principio de diseño #1). El backend solo lee estado para indexarlo y para
    resolver autorizacion (p.ej. "es esta wallet el arbiter de esta orden?")."""

    @abstractmethod
    def get_order(self, onchain_order_id: int) -> ChainOrder:
        """Lectura directa del contrato — usada como fallback si el indexer esta atrasado."""

    @abstractmethod
    def get_status(self, onchain_order_id: int) -> str:
        """Atajo sobre get_order(...).status, para callers que no necesitan la orden completa."""

    @abstractmethod
    def verify_transaction(self, tx_hash: str) -> bool:
        """True si la transaccion existe on-chain y se ejecuto exitosamente (no revertida)."""

    @abstractmethod
    def get_events(self, from_block: int, to_block: int) -> list[ChainEvent]:
        """Todos los eventos del contrato en [from_block, to_block], ordenados por bloque."""

    @abstractmethod
    def get_confirmations(self, block_number: int) -> int:
        """Cuantos bloques de confirmacion tiene `block_number` respecto al head actual."""

    @abstractmethod
    def get_contract_identity(self) -> str:
        """Identificador opaco y estable de "que cadena + que contrato", p.ej. 'tron:TR7...'.
        Util para namespacing una vez que exista mas de un adaptador activo a la vez."""

    @abstractmethod
    def normalize_address(self, address: str) -> str:
        """Forma canonica de una direccion para esta cadena — para poder comparar
        direcciones por igualdad de string de forma segura (p.ej. mayusculas/minusculas,
        formatos alternativos) sin que cada caller conozca las reglas de la cadena.
        Tambien es la via de VALIDACION: lanza ValueError si `address` no tiene
        forma valida para esta cadena (usado por bootstrap_admin.py para
        rechazar wallets malformadas antes de tocar la base de datos)."""

    @abstractmethod
    def current_block(self) -> int:
        """Altura de bloque actual segun el nodo configurado."""

    @abstractmethod
    def verify_wallet_signature(self, address: str, message: str, signature: str) -> bool:
        """Verifica que `signature` corresponde a `message` firmado por la clave
        privada de `address`. Esto es deliberadamente parte del adaptador (y no de
        auth.py) porque el esquema de firma/recuperacion de direccion es
        especifico de cada cadena (curva, formato de direccion) — exactamente el
        tipo de detalle que auth.py, membership, reputation y demas logica de
        negocio no deben conocer (ver seccion 5: "must NOT import tronpy
        directly")."""
