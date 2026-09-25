"""
Implementacion EVM (BSC/Ethereum/Avalanche/Celo/...) de EscrowChainAdapter.
Este archivo (junto con `verify_evm_signature`) es el UNICO lugar del backend
autorizado a importar `web3`/`eth_account` — misma regla de aislamiento que
tron_adapter.py tiene para `tronpy` (ver adapter.py y
tests/test_chain_separation.py, que la hace cumplir estaticamente para tronpy;
web3/eth_account no llevan hoy esa misma prueba automatica porque ningun
network EVM esta ACTIVE todavia, pero la convencion es identica).

Una sola clase sirve para TODAS las redes EVM (BSC, Ethereum, Avalanche,
Celo, ...): lo que cambia entre ellas es el `NetworkConfig` inyectado
(rpc_url, chain_id, contrato), nunca el codigo.

`web3` NO se importa a nivel de modulo (igual que `tronpy` en
tron_adapter.py) para que el resto del backend pueda importar este archivo
sin tener `web3` instalado; solo construir un adaptador real contra un RPC
en vivo lo requiere. Los tests inyectan un `web3_client`/contrato falso —
"No hagas network calls reales en tests" (Fase 3 del encargo).
"""

from __future__ import annotations

import json
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address, to_checksum_address

from .adapter import ChainEvent, ChainOrder, EscrowChainAdapter
from .networks import NetworkConfig

# Mismo orden que el enum OrderStatus en contracts/evm/EscrowP2P.sol -- ver ese archivo.
_ORDER_STATUS_NAMES = ("OPEN", "CLAIMED", "PAID", "DISPUTED", "RELEASED", "REFUNDED", "CANCELLED")

_CONTRACT_EVENT_NAMES = (
    "OrderCreated",
    "OrderClaimed",
    "ClaimExpired",
    "PaidConfirmed",
    "OrderCancelled",
    "DisputeRaised",
    "DisputeVoteCast",
    "OrderSettled",
    "ArbiterUpdated",
    "FeeUpdated",
)


def verify_evm_signature(address: str, message: str, signature: str) -> bool:
    """Logica pura de verificacion de firma EVM (EIP-191 `personal_sign`) —
    funcion de modulo (no metodo), igual que `verify_tron_signature`, para
    poder probarla directamente sin construir un EvmEscrowAdapter completo."""
    try:
        encoded = encode_defunct(text=message)
        recovered = Account.recover_message(encoded, signature=signature)
        return to_checksum_address(recovered) == to_checksum_address(address)
    except Exception:
        return False


class EvmEscrowAdapter(EscrowChainAdapter):
    def __init__(self, network: NetworkConfig, web3_client: Any | None = None) -> None:
        self._network = network
        if web3_client is not None:
            self._w3 = web3_client
        else:
            if not network.rpc_url:
                raise ValueError(f"network {network.network_id!r} no tiene rpc_url configurado")
            from web3 import Web3  # import perezoso: ver docstring del modulo

            self._w3 = Web3(Web3.HTTPProvider(network.rpc_url))

        self._contract = None
        if network.escrow_contract_address and network.escrow_contract_abi_path:
            with open(network.escrow_contract_abi_path) as f:
                abi = json.load(f)
            self._contract = self._w3.eth.contract(
                address=to_checksum_address(network.escrow_contract_address), abi=abi
            )

    def assert_chain_id_matches(self) -> None:
        """Falla cerrado si el chain_id real que reporta el RPC configurado no
        coincide con `network.chain_id` -- evita que una NetworkConfig para
        "bsc" (o cualquier otra red) quede operando silenciosamente contra un
        RPC de una red distinta (rpc_url mal configurado, typo, o un proxy
        que enruta a la red equivocada). `build_adapter_for_network` llama
        esto SIEMPRE antes de devolver un adapter EVM real, para toda red que
        ya paso el gate de DISABLED/HISTORICAL -- ver chain/__init__.py.

        `network.chain_id is None` TAMBIEN falla cerrado (revision humana del
        2026-09-25): una red EVM sin chain_id verificado nunca deberia llegar
        a construir un adapter operacional en primer lugar (las 7 entradas
        DISABLED de networks.py que no tienen chain_id nunca alcanzan este
        metodo, porque build_adapter_for_network rechaza DISABLED antes) --
        la version anterior de este metodo trataba chain_id=None como "nada
        que comparar, dejar pasar", lo que hubiera permitido a una
        NetworkConfig EVM en TESTNET/PILOT/ACTIVE sin chain_id operar sin
        verificar nunca contra que red esta hablando el RPC configurado."""
        if self._network.chain_id is None:
            raise ValueError(
                f"network {self._network.network_id!r} no tiene chain_id configurado -- "
                f"una red EVM operacional (status distinto de DISABLED/HISTORICAL) siempre "
                f"debe tener chain_id verificado antes de poder operar"
            )
        actual = self._w3.eth.chain_id
        if actual != self._network.chain_id:
            raise ValueError(
                f"chain_id mismatch para network {self._network.network_id!r}: "
                f"configurado={self._network.chain_id}, RPC real={actual} -- rpc_url apunta a la red equivocada"
            )

    def current_block(self) -> int:
        return self._w3.eth.block_number

    def get_order(self, onchain_order_id: int) -> ChainOrder:
        # Struct Order de contracts/evm/EscrowP2P.sol, en orden posicional:
        # (seller, buyer, token, amount, createdAt, claimedAt, paidAt,
        #  feeBpsSnapshot, feeCollectorSnapshot, arbiterSnapshot, status)
        raw = self._contract.functions.getOrder(onchain_order_id).call()
        seller, buyer, token, amount = raw[0], raw[1], raw[2], raw[3]
        arbiter_snapshot, status = raw[9], raw[10]
        return ChainOrder(
            onchain_order_id=onchain_order_id,
            seller=seller,
            buyer=buyer,
            token=token,
            amount=int(amount),
            status=_ORDER_STATUS_NAMES[status],
            arbiter_snapshot=arbiter_snapshot,
        )

    def get_status(self, onchain_order_id: int) -> str:
        return self.get_order(onchain_order_id).status

    def verify_transaction(self, tx_hash: str) -> bool:
        try:
            receipt = self._w3.eth.get_transaction_receipt(tx_hash)
            return receipt is not None and receipt.get("status") == 1
        except Exception:
            return False

    def get_events(self, from_block: int, to_block: int) -> list[ChainEvent]:
        events: list[ChainEvent] = []
        for event_name in _CONTRACT_EVENT_NAMES:
            event_factory = getattr(self._contract.events, event_name)
            for log in event_factory().get_logs(from_block=from_block, to_block=to_block):
                args = dict(log["args"])
                events.append(
                    ChainEvent(
                        name=event_name,
                        order_id=int(args.get("orderId", -1)),
                        block_number=log["blockNumber"],
                        tx_hash=log["transactionHash"].hex(),
                        data=args,
                    )
                )
        return sorted(events, key=lambda e: e.block_number)

    def get_confirmations(self, block_number: int) -> int:
        return max(0, self.current_block() - block_number)

    def get_contract_identity(self) -> str:
        return f"{self._network.network_id}:{self._network.escrow_contract_address}"

    def normalize_address(self, address: str) -> str:
        if not is_address(address):
            raise ValueError(f"direccion EVM invalida: {address!r}")
        return to_checksum_address(address)

    def verify_wallet_signature(self, address: str, message: str, signature: str) -> bool:
        return verify_evm_signature(address, message, signature)
