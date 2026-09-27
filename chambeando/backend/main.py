import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import SessionLocal
from .messaging import get_telegram_vip_router, get_whatsapp_vip_router
from .routers import admin, auth, disputes, invites, orders, reports, settlement, telegram, users, whatsapp
from .services import telegram_outbox
from .services.whatsapp_outbox import flush_pending_outbound

# El esquema de la base de datos lo gestiona Alembic (ver backend/alembic/),
# NO create_all() — Phase 2B.1. Correr `alembic upgrade head` antes de
# levantar la app (fuera de los tests, que crean su propio schema efimero
# via create_all() contra un sqlite temporal — ver tests/conftest.py).

logger = logging.getLogger("chambeando.main")

app = FastAPI(title="Chambeando P2P Crypto Exchange API", version="2.1.0")
_outbox_sweep_task: asyncio.Task | None = None


def _run_outbox_sweep_once() -> None:
    """Both VIP outboxes -- WhatsApp's and Telegram's -- swept in their own
    DB sessions, used for both the startup recovery pass and each tick of
    the periodic loop below. Deliberately TWO separate try/except blocks
    (and two separate sessions): a failure sweeping one outbox must never
    stop the other from being swept, and never lets an unexpected error
    kill the loop/startup (a stuck DB or transient issue here must not
    crash the whole process -- next tick tries again)."""
    db = SessionLocal()
    try:
        result = flush_pending_outbound(db, get_whatsapp_vip_router().meta_client)
        if result.get("lease_acquired"):
            logger.info("vip_outbox_sweep attempted=%s reconciled_uncertain=%s", result.get("attempted"), result.get("reconciled_uncertain"))
    except Exception:
        logger.exception("vip_outbox_sweep failed -- will retry next tick")
    finally:
        db.close()

    db = SessionLocal()
    try:
        result = telegram_outbox.flush_pending_outbound(db, get_telegram_vip_router().telegram_client)
        if result.get("lease_acquired"):
            logger.info("telegram_vip_outbox_sweep attempted=%s reconciled_uncertain=%s", result.get("attempted"), result.get("reconciled_uncertain"))
    except Exception:
        logger.exception("telegram_vip_outbox_sweep failed -- will retry next tick")
    finally:
        db.close()


async def _outbox_sweep_loop() -> None:
    while True:
        await asyncio.sleep(settings.WHATSAPP_VIP_OUTBOX_POLL_SECONDS)
        await asyncio.to_thread(_run_outbox_sweep_once)


@app.on_event("startup")
async def _start_outbox_sweep() -> None:
    """Recovery-on-boot (section 'incluye recuperacion ante fallos y
    reinicios'): sweep once immediately so anything left PENDING/
    FAILED_RETRYABLE/stuck-SENDING from before a restart is picked up right
    away, THEN keep sweeping periodically while the process runs (not just
    this once) -- see services/whatsapp_outbox.py's module docstring.
    Gated by WHATSAPP_VIP_OUTBOX_AUTOSTART=False in tests (set in
    conftest.py) so the test suite stays fast/deterministic; tests call
    flush_pending_outbound() directly and synchronously instead."""
    if not settings.WHATSAPP_VIP_OUTBOX_AUTOSTART:
        return
    await asyncio.to_thread(_run_outbox_sweep_once)
    global _outbox_sweep_task
    _outbox_sweep_task = asyncio.create_task(_outbox_sweep_loop())


@app.on_event("shutdown")
async def _stop_outbox_sweep() -> None:
    global _outbox_sweep_task
    if _outbox_sweep_task is not None:
        _outbox_sweep_task.cancel()
        _outbox_sweep_task = None

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
app.include_router(telegram.router)


@app.get("/")
def read_root():
    # PUBLIC: a proposito no devuelve NADA especifico del marketplace (seccion 1)
    return {"message": "Chambeando — marketplace P2P privado, solo para members autenticados"}


@app.get("/health")
def health():
    # Liveness/readiness probe para el orquestador de deployment (Docker
    # HEALTHCHECK, load balancer, etc.) -- deliberadamente no toca la DB ni
    # ningun servicio externo (Meta, Telegram, cadena): solo confirma que el
    # proceso FastAPI esta arriba y respondiendo. Sin auth, sin config sensible.
    return {"status": "ok", "app": "chambeando-backend"}
