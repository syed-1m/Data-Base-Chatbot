"""
app/config.py
=============
Application configuration.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "DB ChatBot"
    APP_VERSION: str = "1.0.0"
    APP_DESCRIPTION: str = "A production-ready Database Chatbot API built with FastAPI."
    DEBUG: bool = True
    ENVIRONMENT: str = "development"

    API_V1_PREFIX: str = "/api/v1"
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8080"]

    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "HUssain@12"
    POSTGRES_DB: str = "chatbot_db"

    DATABASE_URL: str = "postgresql+asyncpg://postgres:HUssain%4012@localhost:5432/chatbot_db"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://postgres:HUssain%4012@localhost:5432/chatbot_db"

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800
    DB_ECHO: bool = False

    SECRET_KEY: str = "dev-secret-key-change-this-before-going-to-production-32ch"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    LOG_LEVEL: str = "DEBUG"
    LOG_FORMAT: str = "text"

    DEFAULT_PAGE_SIZE: int = 20
    MAX_PAGE_SIZE: int = 100

    AI_PROVIDER: Literal["gemini", "openai"] = Field(default="gemini")
    GEMINI_API_KEY: str = Field(default="")
    OPENAI_API_KEY: str = Field(default="")
    AI_MODEL: str = Field(default="gemini-flash-lite-latest")
    AI_TEMPERATURE: float = Field(default=0.1)
    AI_MAX_OUTPUT_TOKENS: int = Field(default=2048)
    AI_MAX_RETRIES: int = Field(default=3)
    AI_REQUEST_TIMEOUT: int = Field(default=30)
    SCHEMA_CACHE_TTL_SECONDS: int = Field(default=300)
    MAX_QUERY_RESULTS: int = Field(default=500)
    MAX_SCHEMA_TABLES: int = Field(default=100)

    # -----------------------------------------------------------------------
    # Intelligent Query Cache
    # -----------------------------------------------------------------------
    CACHE_ENABLED: bool = Field(default=True)
    REDIS_URL: str = Field(default="redis://localhost:6379/0")
    CACHE_TTL_SECONDS: int = Field(default=86400)        # 24 hours
    CACHE_SIMILARITY_THRESHOLD: float = Field(default=0.92)
    CACHE_MAX_CANDIDATES: int = Field(default=1000)      # Max vectors per connection
    CACHE_PREVIEW_ROWS: int = Field(default=10)          # Rows stored in preview

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def get_settings() -> Settings:
    return Settings()


settings = get_settings()
