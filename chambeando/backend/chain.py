"""
Cliente de lectura del contrato EscrowP2P sobre Tron, usado por el indexer y
por endpoints de consulta "read-through" (ver AUDIT de diseño: el backend no
firma transacciones de negocio, solo lee estado on-chain).

NOTA: la API exacta de tronpy para iterar eventos por rango de bloques puede
variar según la versión (get_event_result / eventos vía TronGrid API REST).
Validar contra la versión pineada en requirements.txt y, si TronGrid limita
el rango de bloques por consulta, respetar INDEXER_MAX_BLOCK_RANGE.
"""

import json
from dataclasses import dataclass
from typing import Any

from .config import settings


@dataclass
class ChainEvent:
    name: str
    order_id: int
    block_number: int
    tx_hash: str
    data: dict[str, Any]


class TronEscrowClient:
    def __init__(self) -> None:
        from tronpy import Tron
        from tronpy.providers import HTTPProvider

        self._client = Tron(HTTPProvider(settings.TRON_NODE_URL))
        with open(settings.ESCROW_CONTRACT_ABI_PATH) as f:
            abi = json.load(f)
        self._contract = self._client.get_contract(settings.ESCROW_CONTRACT_ADDRESS)
        self._contract.abi = abi

    def current_block(self) -> int:
        return self._client.get_latest_block_number()

    def get_order(self, onchain_order_id: int) -> dict[str, Any]:
        """Lectura directa del contrato — usada como fallback si el indexer está atrasado."""
        return self._contract.functions.getOrder(onchain_order_id)

    def get_events(self, from_block: int, to_block: int) -> list[ChainEvent]:
        """
        Devuelve todos los eventos del contrato (OrderCreated, OrderClaimed,
        ClaimExpired, PaidConfirmed, OrderCancelled, DisputeRaised, OrderSettled)
        en el rango [from_block, to_block], ordenados por bloque.
        """
        raw_events = self._contract.events.get(
            since_block=from_block,
            until_block=to_block,
        )
        events: list[ChainEvent] = []
        for e in raw_events:
            events.append(
                ChainEvent(
                    name=e["event_name"],
                    order_id=int(e["result"].get("orderId", -1)),
                    block_number=e["block_number"],
                    tx_hash=e["transaction_id"],
                    data=e["result"],
                )
            )
        return sorted(events, key=lambda ev: ev.block_number)


_client: TronEscrowClient | None = None


def get_chain_client() -> TronEscrowClient:
    global _client
    if _client is None:
        _client = TronEscrowClient()
    return _client
