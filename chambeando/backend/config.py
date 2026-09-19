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

    # Phase 2D.1: config-driven provider selection — NEVER auto-detected from
    # "are credentials present" (that would silently start using a real Meta
    # transport the moment someone exports an env var for local testing).
    # "sandbox" (default) -> SandboxMetaClient, never touches the network.
    # "meta" -> MetaCloudWhatsAppClient, requires the three fields below.
    WHATSAPP_PROVIDER: str = "sandbox"
    WHATSAPP_ACCESS_TOKEN: str | None = None
    WHATSAPP_PHONE_NUMBER_ID: str | None = None
    WHATSAPP_GRAPH_API_VERSION: str = "v21.0"


settings = Settings()
