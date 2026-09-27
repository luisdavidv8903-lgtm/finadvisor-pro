"""Configuración vía variables de entorno. Nunca hardcodear secretos en el código fuente."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "sqlite:///./chambeando_v2.db"
    SECRET_KEY: str  # obligatorio: la app falla al arrancar si no está seteada — usado solo para firmar JWT de sesión
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24
    NONCE_EXPIRE_SECONDS: int = 300

    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # Cadena / contrato — que adaptador usar, ver chain/adapter.py. Solo "tron" existe hoy;
    # "bsc" es un valor reservado a proposito, no implementado (ver chain/__init__.py).
    CHAIN_ADAPTER: str = "tron"
    TRON_NODE_URL: str = "https://api.shasta.trongrid.io"  # testnet por defecto — cambiar a mainnet solo tras auditoría
    ESCROW_CONTRACT_ADDRESS: str = ""
    ESCROW_CONTRACT_ABI_PATH: str = "./contracts/EscrowP2P.abi.json"
    CONFIRMATIONS_REQUIRED: int = 19
    INDEXER_POLL_INTERVAL_SECONDS: int = 5
    INDEXER_MAX_BLOCK_RANGE: int = 500

    # Liquidacion (fiat) — cifrado en reposo. Clave simetrica Fernet (44 chars urlsafe-base64,
    # generar con `Fernet.generate_key()`). OBLIGATORIA: la app falla al arrancar si falta,
    # igual que SECRET_KEY. NUNCA con valor por defecto, NUNCA en el repo. Ver security/crypto.py
    # para el limite de esta implementacion y el camino de upgrade a KMS/HSM.
    SETTLEMENT_ENCRYPTION_KEY: str

    # Rate limiting — ver security/rate_limit.py. El limitador en memoria es SOLO para
    # dev/tests locales (se resetea si el proceso reinicia, no es compartido entre workers);
    # produccion debe reemplazarlo por Redis o limitacion a nivel de gateway.
    RATE_LIMIT_NONCE_PER_MINUTE: int = 5
    RATE_LIMIT_VERIFY_PER_MINUTE: int = 10
    RATE_LIMIT_INVITE_REDEEM_PER_MINUTE: int = 5

    # WhatsApp (Phase 2C/2D.1) — see WHATSAPP_ARCHITECTURE.md. Safe sandbox
    # defaults on purpose for the verify token/app secret (unlike
    # SECRET_KEY/SETTLEMENT_ENCRYPTION_KEY, which fail closed): there is no
    # production Meta app configured by default, so requiring a real secret
    # here would block local sandbox use for nothing. A real deployment MUST
    # override these via env before going anywhere near a real Meta webhook.
    WHATSAPP_VERIFY_TOKEN: str = "sandbox-verify-token-never-use-in-production"
    WHATSAPP_APP_SECRET: str = "sandbox-app-secret-never-use-in-production"
    WHATSAPP_LINK_TOKEN_EXPIRE_SECONDS: int = 600
    RATE_LIMIT_WHATSAPP_MESSAGE_PER_MINUTE: int = 20

    # Fail-closed webhook signature policy (deployment hardening). The
    # WHATSAPP_APP_SECRET default above is a PUBLIC placeholder committed to
    # this repo -- if a real deployment left it unconfigured, HMAC signature
    # verification would still "pass" for any attacker who read this file and
    # signed a forged request with that same known value. WHATSAPP_ALLOW_UNVERIFIED_WEBHOOKS
    # gates that: default False means the webhook route refuses ALL POSTs
    # while WHATSAPP_APP_SECRET is still the public default, regardless of
    # whether a signature header is present or "valid" against it. Only an
    # explicit, deliberate True (dev/sandbox-only, never set in production)
    # lifts that refusal -- it is never inferred from APP_SECRET being empty.
    WHATSAPP_ALLOW_UNVERIFIED_WEBHOOKS: bool = False

    # Phase 2D.1: config-driven provider selection — NEVER auto-detected from
    # "are credentials present" (that would silently start using a real Meta
    # transport the moment someone exports an env var for local testing).
    # "sandbox" (default) -> SandboxMetaClient, never touches the network.
    # "meta" -> MetaCloudWhatsAppClient, requires the three fields below.
    WHATSAPP_PROVIDER: str = "sandbox"
    WHATSAPP_ACCESS_TOKEN: str | None = None
    WHATSAPP_PHONE_NUMBER_ID: str | None = None
    WHATSAPP_GRAPH_API_VERSION: str = "v21.0"

    # Telegram V1 pilot -- same safe-default / config-driven-provider pattern
    # as WhatsApp above. "sandbox" (default) never touches the network;
    # "telegram" requires TELEGRAM_BOT_TOKEN.
    TELEGRAM_WEBHOOK_SECRET: str = "sandbox-telegram-secret-never-use-in-production"
    TELEGRAM_PROVIDER: str = "sandbox"
    TELEGRAM_BOT_TOKEN: str | None = None
    RATE_LIMIT_TELEGRAM_MESSAGE_PER_MINUTE: int = 20

    # Wallet-signing handoff (Fase de vinculacion segura, revision 2026-09-25).
    # Antes vivia implicito en WHATSAPP_PROVIDER ("meta" => siempre False, sin
    # variable propia) -- eso acoplaba dos decisiones independientes ("¿mando
    # mensajes reales por Meta?" y "¿ya existe un dApp HTTPS real para
    # firmar?"). Ahora son dos flags separados: activar Meta real NUNCA
    # habilita el handoff por si solo. Default False (fail-closed) en ambos
    # casos -- activar el handoff es una decision humana explicita, nunca un
    # efecto colateral de otra config.
    WALLET_HANDOFF_ENABLED: bool = False
    # Dominio local de desarrollo, nunca un placeholder que aparente ser un
    # servicio real (ver revision: "sin dominio ficticio operativo"). Un
    # deploy real DEBE sobreescribir esto con el HTTPS real del dApp antes de
    # que WALLET_HANDOFF_ENABLED tenga algun efecto util.
    DAPP_BASE_URL: str = "http://localhost:5173/app"

    # WhatsApp VIP offer-board pilot -- see messaging/whatsapp_vip_router.py
    # and services/whatsapp_outbox.py. Completely separate from the wallet/
    # escrow settings above -- the VIP flow never reads or sets
    # WALLET_HANDOFF_ENABLED/DAPP_BASE_URL, and never touches P2POrderDB.
    # Inbound rate limiting reuses the existing per-whatsapp_id
    # RATE_LIMIT_WHATSAPP_MESSAGE_PER_MINUTE limiter already enforced in
    # routers/whatsapp.py for every inbound message -- no separate
    # VIP-specific limit is needed.
    # Periodic outbox sweep: retries PENDING/FAILED_RETRYABLE outbound
    # replies while the process is running, not just once at startup (see
    # services/whatsapp_outbox.py's module docstring for the full design).
    # False in tests (set via conftest.py) to keep the suite fast/deterministic;
    # tests call flush_pending_outbound() directly and synchronously instead.
    WHATSAPP_VIP_OUTBOX_AUTOSTART: bool = True
    WHATSAPP_VIP_OUTBOX_POLL_SECONDS: int = 15
    WHATSAPP_VIP_OUTBOX_MAX_ATTEMPTS: int = 6
    # A row stuck in SENDING longer than this is treated as an AMBIGUOUS
    # outcome (process may have crashed between "sent to Meta" and "recorded
    # the result") -- moved to UNCERTAIN_NEEDS_RECONCILIATION, never
    # auto-retried (see services/whatsapp_outbox.py).
    WHATSAPP_VIP_OUTBOX_STUCK_SECONDS: int = 30
    WHATSAPP_VIP_OUTBOX_LEASE_SECONDS: int = 20

    # Telegram-as-source-of-truth VIP pilot (2026-09-27) -- see
    # messaging/telegram_vip_router.py. None by default on purpose: the
    # t.me/<bot>?start=offer_<id> deep link this pilot generates for the
    # operator to copy into the WhatsApp VIP group is worthless (or
    # actively wrong) if it points at a placeholder/guessed username, so
    # telegram_vip_router.validate_telegram_bot_username() refuses to build
    # a link at all while this is unset or malformed -- it never fabricates
    # one. Must be the bot's real @username (without the leading @), set by
    # whoever registered the bot with @BotFather.
    TELEGRAM_VIP_BOT_USERNAME: str | None = None


settings = Settings()
