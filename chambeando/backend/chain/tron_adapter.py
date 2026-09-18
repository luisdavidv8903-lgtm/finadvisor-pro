"""
Implementacion TRON de EscrowChainAdapter. Este es el UNICO archivo del backend
autorizado a importar `tronpy` — ver adapter.py. Si algun otro modulo necesita
hablar con la cadena, importa la interfaz (`EscrowChainAdapter`) o la factory
(`chain.get_chain_adapter`), nunca este modulo directamente.

NOTA DE IMPLEMENTACION (heredada de Phase 1): la API exacta de tronpy para
eventos/lectura de contrato puede variar segun version — validar contra la
version pineada en requirements.txt antes de produccion.

Runtime verificado: Python 3.12 + tronpy==0.6.2 (ver backend/.python-version y
requirements.txt) — Python 3.14 fue descartado porque la dependencia
transitiva `coincurve` no compila ahi (problema de packaging, no de este
codigo). Ver tests/test_tron_signature_real.py para los vectores de prueba
criptograficos reales contra esta version exacta de tronpy.
"""

import json

from ..config import settings
from .adapter import ChainEvent, ChainOrder, EscrowChainAdapter


def verify_tron_signature(address: str, message: str, signature: str) -> bool:
    """Logica pura de verificacion de firma TRON — funcion de modulo (no metodo)
    para poder probarla directamente con tronpy real, SIN construir un
    TronEscrowAdapter completo (que requiere un nodo/contrato configurado).
    Corregido en Phase 2B.1: `PublicKey.recover_from_msg` exige un objeto
    `tronpy.keys.Signature`, NO `bytes` crudos — pasarle bytes crudos lanza
    `AttributeError` en la version real de tronpy (0.6.2), un bug real que
    tenia el codigo heredado de Phase 1 y que nunca se habia probado contra la
    libreria real hasta ahora (ver tests/test_tron_signature_real.py)."""
    try:
        from tronpy.keys import PublicKey, Signature

        sig_bytes = bytes.fromhex(signature.removeprefix("0x"))
        recovered = PublicKey.recover_from_msg(message.encode(), Signature(sig_bytes))
        return recovered.to_base58check_address() == address
    except Exception:
        return False


class TronEscrowAdapter(EscrowChainAdapter):
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

    def get_order(self, onchain_order_id: int) -> ChainOrder:
        raw = self._contract.functions.getOrder(onchain_order_id)
        return ChainOrder(
            onchain_order_id=onchain_order_id,
            seller=raw["seller"],
            buyer=raw["buyer"],
            token=raw["token"],
            amount=int(raw["amount"]),
            status=raw["status"],  # tronpy decodifica el enum a su nombre de string
            arbiter_snapshot=raw["arbiterSnapshot"],
        )

    def get_status(self, onchain_order_id: int) -> str:
        return self.get_order(onchain_order_id).status

    def verify_transaction(self, tx_hash: str) -> bool:
        try:
            info = self._client.get_transaction_info(tx_hash)
            return info.get("receipt", {}).get("result") == "SUCCESS"
        except Exception:
            return False

    def get_events(self, from_block: int, to_block: int) -> list[ChainEvent]:
        raw_events = self._contract.events.get(since_block=from_block, until_block=to_block)
        events = [
            ChainEvent(
                name=e["event_name"],
                order_id=int(e["result"].get("orderId", -1)),
                block_number=e["block_number"],
                tx_hash=e["transaction_id"],
                data=e["result"],
            )
            for e in raw_events
        ]
        return sorted(events, key=lambda ev: ev.block_number)

    def get_confirmations(self, block_number: int) -> int:
        return max(0, self.current_block() - block_number)

    def get_contract_identity(self) -> str:
        return f"tron:{settings.ESCROW_CONTRACT_ADDRESS}"

    def normalize_address(self, address: str) -> str:
        # las direcciones base58check de TRON son case-sensitive y ya canonicas tal
        # como las devuelve tronpy/el nodo — no hay normalizacion de mayusculas como
        # en direcciones hex de EVM, asi que "normalizar" aqui es principalmente
        # VALIDAR. tronpy.keys.is_base58check_address puede lanzar (en vez de
        # devolver False) ante un checksum invalido — ver bug conocido, envuelto
        # aqui para que esta funcion tenga un contrato limpio: devuelve la
        # direccion si es valida, lanza ValueError si no.
        from tronpy.keys import is_base58check_address

        try:
            valid = is_base58check_address(address)
        except Exception:
            valid = False
        if not valid:
            raise ValueError(f"direccion TRON invalida: {address!r}")
        return address

    def verify_wallet_signature(self, address: str, message: str, signature: str) -> bool:
        return verify_tron_signature(address, message, signature)
