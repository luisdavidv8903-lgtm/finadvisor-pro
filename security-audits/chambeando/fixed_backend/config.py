"""Configuración vía variables de entorno. Nunca hardcodear secretos en el código fuente."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "sqlite:///./chambeando.db"
    SECRET_KEY: str  # obligatorio: la app falla al arrancar si no está seteada
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]


settings = Settings()
