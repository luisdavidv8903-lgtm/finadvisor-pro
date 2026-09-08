from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from . import escrow_service
from .config import settings
from .database import Base, engine, get_db
from .deps import get_current_user
from .models import UserDB, WalletDB
from .schemas import OrderCreate, UserCreate
from .security import create_access_token, get_password_hash, verify_password

Base.metadata.create_all(bind=engine)  # solo para desarrollo local; usar Alembic en producción

app = FastAPI(title="Chambeando P2P Exchange API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"message": "Bienvenido al motor backend de Chambeando"}


@app.post("/token")
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
):
    user = db.query(UserDB).filter(UserDB.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Correo o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(
        data={"sub": user.email},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.post("/users/", status_code=status.HTTP_201_CREATED)
def register_user(user: UserCreate, db: Session = Depends(get_db)):
    if db.query(UserDB).filter(UserDB.email == user.email).first():
        raise HTTPException(status_code=400, detail="El correo ya está registrado")

    new_user = UserDB(
        username=user.username,
        email=user.email,
        hashed_password=get_password_hash(user.password),  # nunca guardar la contraseña en texto plano
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    db.add(WalletDB(user_id=new_user.id, currency="USDT", balance=0))
    db.commit()

    return {"message": "Usuario registrado exitosamente en Chambeando", "user_id": new_user.id}


@app.post("/p2p/orders/", status_code=status.HTTP_201_CREATED)
def create_p2p_order(
    order: OrderCreate,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    new_order = escrow_service.create_p2p_order(
        db,
        seller=current_user,
        crypto_amount=order.crypto_amount,
        fiat_amount=order.fiat_amount,
        fiat_currency=order.fiat_currency,
        payment_method=order.payment_method,
    )
    return {
        "message": "Anuncio P2P creado y fondos retenidos en garantía (Escrow)",
        "order_id": new_order.id,
    }


@app.post("/p2p/orders/{order_id}/pay", status_code=status.HTTP_200_OK)
def mark_order_as_paid(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    order = escrow_service.mark_order_as_paid(db, order_id, buyer=current_user)
    return {
        "message": "Orden marcada como pagada. Esperando confirmación y liberación del vendedor.",
        "order_id": order.id,
    }


@app.post("/p2p/orders/{order_id}/release", status_code=status.HTTP_200_OK)
def release_escrow(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    order = escrow_service.release_escrow(db, order_id, seller=current_user)
    return {
        "message": "¡Fondos liberados con éxito! Transacción completada en Chambeando",
        "order_id": order.id,
    }


@app.post("/p2p/orders/{order_id}/cancel", status_code=status.HTTP_200_OK)
def cancel_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    order = escrow_service.cancel_order(db, order_id, seller=current_user)
    return {
        "message": "Orden cancelada correctamente. Los fondos han sido devueltos a tu balance.",
        "order_id": order.id,
    }
