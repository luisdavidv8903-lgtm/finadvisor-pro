from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import admin, auth, disputes, invites, orders, reports, settlement, users, whatsapp

# El esquema de la base de datos lo gestiona Alembic (ver backend/alembic/),
# NO create_all() — Phase 2B.1. Correr `alembic upgrade head` antes de
# levantar la app (fuera de los tests, que crean su propio schema efimero
# via create_all() contra un sqlite temporal — ver tests/conftest.py).

app = FastAPI(title="Chambeando P2P Crypto Exchange API", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(invites.router)
app.include_router(admin.router)
app.include_router(orders.router)
app.include_router(settlement.router)
app.include_router(disputes.router)
app.include_router(reports.router)
app.include_router(users.router)
app.include_router(whatsapp.router)


@app.get("/")
def read_root():
    # PUBLIC: a proposito no devuelve NADA especifico del marketplace (seccion 1)
    return {"message": "Chambeando — marketplace P2P privado, solo para members autenticados"}
