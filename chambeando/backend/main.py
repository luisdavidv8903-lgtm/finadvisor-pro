from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from .auth import (
    build_sign_message,
    create_access_token,
    generate_nonce,
    verify_and_consume_nonce,
    verify_tron_signature,
)
from .config import settings
from .database import Base, engine, get_db
from .deps import get_current_user
from .models import AuthNonceDB, DisputeEvidenceDB, OrderStatus, P2POrderDB, UserDB
from .schemas import (
    DisputeEvidenceCreate,
    NonceRequest,
    NonceResponse,
    OrderMetadataCreate,
    TokenResponse,
    VerifyRequest,
)

Base.metadata.create_all(bind=engine)  # solo para desarrollo local; usar Alembic en producción

app = FastAPI(title="Chambeando P2P Crypto Exchange API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"message": "Chambeando — escrow P2P no-custodial sobre Tron"}


# ---------------------------------------------------------------------------
# Autenticación por firma de wallet
# ---------------------------------------------------------------------------


@app.post("/auth/nonce", response_model=NonceResponse)
def request_nonce(payload: NonceRequest, db: Session = Depends(get_db)):
    nonce = generate_nonce(db, payload.wallet_address)
    return NonceResponse(nonce=nonce, message=build_sign_message(nonce))


@app.post("/auth/verify", response_model=TokenResponse)
def verify_signature(payload: VerifyRequest, db: Session = Depends(get_db)):
    # el nonce se recupera implícitamente: buscamos el más reciente no usado para esta wallet
    entry = (
        db.query(AuthNonceDB)
        .filter(AuthNonceDB.wallet_address == payload.wallet_address, AuthNonceDB.used == 0)
        .order_by(AuthNonceDB.created_at.desc())
        .first()
    )
    if entry is None:
        raise HTTPException(status_code=400, detail="Solicitá un nonce primero con /auth/nonce")

    message = build_sign_message(entry.nonce)
    if not verify_tron_signature(payload.wallet_address, message, payload.signature):
        raise HTTPException(status_code=401, detail="Firma inválida")

    if not verify_and_consume_nonce(db, payload.wallet_address, entry.nonce):
        raise HTTPException(status_code=401, detail="Nonce expirado o ya utilizado")

    user = db.query(UserDB).filter(UserDB.wallet_address == payload.wallet_address).first()
    if user is None:
        user = UserDB(wallet_address=payload.wallet_address)
        db.add(user)
        db.commit()

    return TokenResponse(access_token=create_access_token(payload.wallet_address))


# ---------------------------------------------------------------------------
# Órdenes — el estado financiero lo escribe únicamente el indexer (indexer.py).
# Estos endpoints solo leen y agregan metadata off-chain (moneda fiat, método de pago).
# ---------------------------------------------------------------------------


@app.get("/p2p/orders/")
def list_orders(db: Session = Depends(get_db)):
    orders = (
        db.query(P2POrderDB)
        .filter(P2POrderDB.onchain_status == OrderStatus.OPEN)
        .order_by(P2POrderDB.created_at.desc())
        .all()
    )
    return orders


@app.get("/p2p/orders/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db)):
    order = db.query(P2POrderDB).filter(P2POrderDB.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    return order


@app.post("/p2p/orders/metadata", status_code=status.HTTP_201_CREATED)
def attach_order_metadata(
    payload: OrderMetadataCreate,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    """
    El vendedor llama esto DESPUÉS de que su transacción createOrder() ya fue
    indexada (ver indexer.py) — nunca antes. Verificamos que la orden on-chain
    exista y que quien llama sea realmente el vendedor registrado en el evento,
    para que nadie pueda adjuntar metadata falsa a la orden de otra persona.
    """
    order = db.query(P2POrderDB).filter(P2POrderDB.onchain_order_id == payload.onchain_order_id).first()
    if not order:
        raise HTTPException(
            status_code=404,
            detail="Orden no encontrada on-chain todavía (esperá a que el indexer la procese)",
        )
    if order.seller_wallet != current_user.wallet_address:
        raise HTTPException(status_code=403, detail="Solo el vendedor de esta orden puede adjuntar metadata")
    if order.fiat_amount is not None:
        raise HTTPException(status_code=400, detail="Esta orden ya tiene metadata adjunta")

    order.fiat_amount = payload.fiat_amount
    order.fiat_currency = payload.fiat_currency
    order.payment_method = payload.payment_method
    db.commit()
    db.refresh(order)
    return order


# ---------------------------------------------------------------------------
# Disputas — evidencia off-chain para que el árbitro (multisig) resuelva on-chain.
# ---------------------------------------------------------------------------


@app.post("/disputes/evidence", status_code=status.HTTP_201_CREATED)
def submit_dispute_evidence(
    payload: DisputeEvidenceCreate,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    order = db.query(P2POrderDB).filter(P2POrderDB.id == payload.order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    if order.onchain_status != OrderStatus.DISPUTED:
        raise HTTPException(status_code=400, detail="La orden no está en disputa on-chain")
    if current_user.wallet_address not in (order.seller_wallet, order.buyer_wallet):
        raise HTTPException(status_code=403, detail="No sos parte de esta orden")

    evidence = DisputeEvidenceDB(
        order_id=order.id,
        submitted_by_wallet=current_user.wallet_address,
        file_url=payload.file_url,
        note=payload.note,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence
