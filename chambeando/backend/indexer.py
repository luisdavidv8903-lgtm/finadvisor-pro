"""
Worker independiente (proceso separado del API server) que sincroniza el
estado del contrato EscrowP2P hacia P2POrderDB.

Reglas de diseño (ver security-audits/chambeando para el porqué):
  - Idempotente: reprocesar el mismo evento dos veces no debe duplicar nada.
  - Checkpoint persistido en DB (IndexerCheckpointDB), no en memoria — si el
    proceso se reinicia, retoma exactamente donde quedó.
  - Solo avanza hasta (head - CONFIRMATIONS_REQUIRED) para no reaccionar a
    eventos que un reorg todavía podría revertir.
  - onchain_status, arbiter_snapshot_wallet, was_disputed y confirmed_block se
    escriben EXCLUSIVAMENTE aqui, nunca desde un endpoint.
  - Solo habla con la cadena a traves de EscrowChainAdapter (chain/) — nunca
    importa tronpy directamente (ver seccion 5 del informe de Phase 2B).

Correr como: `python -m chambeando.backend.indexer` (loop infinito) o como
un cronjob/servicio systemd separado del proceso uvicorn de la API.
"""

import logging
import time

from sqlalchemy.orm import Session

from .chain import ChainEvent, get_chain_adapter
from .config import settings
from .database import SessionLocal
from .models import IndexerCheckpointDB, OrderStatus, P2POrderDB

logger = logging.getLogger("chambeando.indexer")

# nombre de evento del contrato -> estado resultante en P2POrderDB.onchain_status
_EVENT_TO_STATUS = {
    "OrderClaimed": OrderStatus.CLAIMED,
    "ClaimExpired": OrderStatus.OPEN,
    "PaidConfirmed": OrderStatus.PAID,
    "OrderCancelled": OrderStatus.CANCELLED,
    "DisputeRaised": OrderStatus.DISPUTED,
}


def _get_or_create_checkpoint(db: Session, contract_identity: str) -> IndexerCheckpointDB:
    checkpoint = (
        db.query(IndexerCheckpointDB).filter(IndexerCheckpointDB.contract_address == contract_identity).first()
    )
    if checkpoint is None:
        checkpoint = IndexerCheckpointDB(contract_address=contract_identity, last_processed_block=0)
        db.add(checkpoint)
        db.commit()
        db.refresh(checkpoint)
    return checkpoint


def _apply_event(db: Session, event: ChainEvent) -> None:
    if event.name == "OrderCreated":
        # UPSERT idempotente por onchain_order_id: si el proceso reprocesa este
        # rango de bloques (p.ej. tras un reinicio), no debe crear duplicados.
        existing = db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == event.order_id).first()
        if existing:
            return
        db.add(
            P2POrderDB(
                onchain_order_id=event.order_id,
                escrow_tx_hash=event.tx_hash,
                token_address=event.data.get("token", ""),
                seller_wallet=event.data.get("seller", ""),
                crypto_amount=event.data.get("amount", 0),
                onchain_status=OrderStatus.OPEN,
                confirmed_block=event.block_number,
            )
        )
        # flush (no commit) para que eventos MAS TARDE en este MISMO batch (p.ej. un
        # OrderClaimed del mismo orderId, si cayeron en el mismo rango de bloques)
        # puedan encontrar esta fila via query — la sesion usa autoflush=False
        # (SessionLocal en database.py), asi que sin este flush explicito la fila
        # queda invisible a queries hasta el commit final de run_indexer_once().
        db.flush()
        return

    if event.name == "OrderClaimed":
        order = db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == event.order_id).first()
        if order:
            order.buyer_wallet = event.data.get("buyer", order.buyer_wallet)
            # arbiterSnapshot se fija en el contrato en claimOrder() (Phase 2A.1) — el
            # indexer solo espeja lo que el evento ya trae, nunca lo calcula.
            order.arbiter_snapshot_wallet = event.data.get("arbiterSnapshot", order.arbiter_snapshot_wallet)

    if event.name == "OrderSettled":
        order = db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == event.order_id).first()
        if order:
            # Phase 2A.1: el contrato escribe RELEASED/REFUNDED explicitamente (evento
            # trae `finalStatus`) — el indexer YA NO infiere el estado comparando
            # `recipient` contra `seller_wallet`, solo espeja lo que el contrato dijo.
            final_status = event.data.get("finalStatus")
            order.onchain_status = (
                OrderStatus.RELEASED if str(final_status) in ("4", "RELEASED") else OrderStatus.REFUNDED
            )
            order.confirmed_block = event.block_number
        return

    new_status = _EVENT_TO_STATUS.get(event.name)
    if new_status is None:
        logger.warning("Evento desconocido del contrato: %s", event.name)
        return

    order = db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == event.order_id).first()
    if order is None:
        logger.error("Evento %s para orderId=%s sin OrderCreated previo indexado", event.name, event.order_id)
        return

    # guard idempotente: no retroceder ni reaplicar el mismo estado dos veces
    if order.onchain_status == new_status:
        return
    order.onchain_status = new_status
    order.confirmed_block = event.block_number
    if event.name == "DisputeRaised":
        order.was_disputed = True  # una vez True, nunca vuelve a False (ver models.py)


def run_indexer_once() -> None:
    adapter = get_chain_adapter()
    db = SessionLocal()
    try:
        contract_identity = adapter.get_contract_identity()
        checkpoint = _get_or_create_checkpoint(db, contract_identity)
        chain_head = adapter.current_block()
        safe_head = chain_head - settings.CONFIRMATIONS_REQUIRED

        from_block = checkpoint.last_processed_block + 1
        if safe_head < from_block:
            return  # nada nuevo con suficientes confirmaciones todavia

        to_block = min(safe_head, from_block + settings.INDEXER_MAX_BLOCK_RANGE)

        events = adapter.get_events(from_block, to_block)
        for event in events:
            _apply_event(db, event)

        checkpoint.last_processed_block = to_block
        db.commit()
        logger.info("Indexer: procesados bloques %s-%s (%s eventos)", from_block, to_block, len(events))
    except Exception:
        db.rollback()
        logger.exception("Error procesando bloques en el indexer")
    finally:
        db.close()


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("Indexer iniciado, adaptador=%s", settings.CHAIN_ADAPTER)
    while True:
        run_indexer_once()
        time.sleep(settings.INDEXER_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
