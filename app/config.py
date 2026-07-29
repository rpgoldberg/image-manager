from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False
    )

    # App
    app_name: str = "image-manager"
    environment: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"
    allow_dev_tokens: bool = True

    # Security
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    token_exp_minutes: int = 60 * 24

    # Data stores
    database_url: str = "postgresql+psycopg2://postgres:postgres@postgres:5432/image_manager"
    redis_url: str = "redis://redis:6379/0"

    # S3/MinIO
    s3_endpoint_url: str | None = "http://minio:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "images"
    s3_secure: bool = False
    s3_presign_expiry_post: int = 15 * 60
    s3_presign_expiry_get: int = 10 * 60

    # DB connection pool
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800  # 30 minutes


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
