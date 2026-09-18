"""
Adaptador de cadena falso, 100% en memoria — usado por TODOS los tests del
backend. Prueba por construccion que la capa de negocio puede correr contra
`EscrowChainAdapter` sin saber nada de TRON: este archivo no importa tronpy ni
ninguna libreria especifica de cadena.

La firma "valida" es deliberadamente trivial y determinista (no criptografia
real) para poder escribir tests de auth deterministas: una firma es valida
solo si es exactamente `f"valid-sig:{address}:{message}"`.
"""

from __future__ import annotations

from backend.chain.adapter import ChainEvent, ChainOrder, EscrowChainAdapter


def make_valid_signature(address: str, message: str) -> str:
    return f"valid-sig:{address}:{message}"


class FakeChainAdapter(EscrowChainAdapter):
    def __init__(self) -> None:
        self._orders: dict[int, ChainOrder] = {}
        self._events: list[ChainEvent] = []
        self._block = 1000

    # --- helpers de test para "escribir" estado on-chain sin un contrato real ---

    def seed_order_created(self, order_id: int, seller: str, token: str, amount: int) -> None:
        self._block += 1
        self._orders[order_id] = ChainOrder(
            onchain_order_id=order_id, seller=seller, buyer="", token=token, amount=amount, status="OPEN", arbiter_snapshot=""
        )
        self._events.append(
            ChainEvent(
                name="OrderCreated",
                order_id=order_id,
                block_number=self._block,
                tx_hash=f"tx-create-{order_id}",
                data={"seller": seller, "token": token, "amount": amount},
            )
        )

    def seed_order_claimed(self, order_id: int, buyer: str, arbiter_snapshot: str) -> None:
        self._block += 1
        o = self._orders[order_id]
        self._orders[order_id] = ChainOrder(
            onchain_order_id=o.onchain_order_id,
            seller=o.seller,
            buyer=buyer,
            token=o.token,
            amount=o.amount,
            status="CLAIMED",
            arbiter_snapshot=arbiter_snapshot,
        )
        self._events.append(
            ChainEvent(
                name="OrderClaimed",
                order_id=order_id,
                block_number=self._block,
                tx_hash=f"tx-claim-{order_id}",
                data={"buyer": buyer, "arbiterSnapshot": arbiter_snapshot},
            )
        )

    def seed_paid(self, order_id: int) -> None:
        self._block += 1
        o = self._orders[order_id]
        self._orders[order_id] = ChainOrder(**{**o.__dict__, "status": "PAID"})
        self._events.append(
            ChainEvent(name="PaidConfirmed", order_id=order_id, block_number=self._block, tx_hash=f"tx-paid-{order_id}", data={})
        )

    def seed_disputed(self, order_id: int) -> None:
        self._block += 1
        o = self._orders[order_id]
        self._orders[order_id] = ChainOrder(**{**o.__dict__, "status": "DISPUTED"})
        self._events.append(
            ChainEvent(name="DisputeRaised", order_id=order_id, block_number=self._block, tx_hash=f"tx-dispute-{order_id}", data={})
        )

    def seed_settled(self, order_id: int, final_status: str) -> None:
        """final_status: "RELEASED" o "REFUNDED" — el contrato Phase 2A.1 lo escribe
        explicito, el indexer solo lo espeja (nunca lo infiere del recipient)."""
        self._block += 1
        o = self._orders[order_id]
        self._orders[order_id] = ChainOrder(**{**o.__dict__, "status": final_status})
        self._events.append(
            ChainEvent(
                name="OrderSettled",
                order_id=order_id,
                block_number=self._block,
                tx_hash=f"tx-settle-{order_id}",
                data={"finalStatus": final_status},
            )
        )

    def seed_cancelled(self, order_id: int) -> None:
        self._block += 1
        o = self._orders[order_id]
        self._orders[order_id] = ChainOrder(**{**o.__dict__, "status": "CANCELLED"})
        self._events.append(
            ChainEvent(name="OrderCancelled", order_id=order_id, block_number=self._block, tx_hash=f"tx-cancel-{order_id}", data={})
        )

    # --- EscrowChainAdapter ---

    def get_order(self, onchain_order_id: int) -> ChainOrder:
        return self._orders[onchain_order_id]

    def get_status(self, onchain_order_id: int) -> str:
        return self._orders[onchain_order_id].status

    def verify_transaction(self, tx_hash: str) -> bool:
        return any(e.tx_hash == tx_hash for e in self._events)

    def get_events(self, from_block: int, to_block: int) -> list[ChainEvent]:
        return sorted((e for e in self._events if from_block <= e.block_number <= to_block), key=lambda e: e.block_number)

    def get_confirmations(self, block_number: int) -> int:
        return max(0, self._block - block_number)

    def get_contract_identity(self) -> str:
        return "fake:test-contract"

    def normalize_address(self, address: str) -> str:
        # validacion liviana pero real (no un passthrough incondicional) — misma
        # forma que las wallets sinteticas usadas en todo el test suite
        # ("TFake...", 25-34 chars), para poder probar "malformed wallet denied"
        # en bootstrap_admin.py sin necesitar tronpy real en ese test.
        if not address.startswith("T") or not (25 <= len(address) <= 34) or not address.isalnum():
            raise ValueError(f"direccion TRON invalida: {address!r}")
        return address

    def current_block(self) -> int:
        return self._block

    def verify_wallet_signature(self, address: str, message: str, signature: str) -> bool:
        return signature == make_valid_signature(address, message)
