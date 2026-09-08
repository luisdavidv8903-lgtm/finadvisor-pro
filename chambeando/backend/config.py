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

    # Cadena / contrato
    TRON_NODE_URL: str = "https://api.shasta.trongrid.io"  # testnet por defecto — cambiar a mainnet solo tras auditoría
    ESCROW_CONTRACT_ADDRESS: str = ""
    ESCROW_CONTRACT_ABI_PATH: str = "./contracts/EscrowP2P.abi.json"
    CONFIRMATIONS_REQUIRED: int = 19
    INDEXER_POLL_INTERVAL_SECONDS: int = 5
    INDEXER_MAX_BLOCK_RANGE: int = 500


settings = Settings()
