"""실행 위치와 무관하게 저장소 루트의 .env.api에서 실행 설정을 읽는다.

우선순위는 프로세스 환경변수 → .env.api → 기본값이다. 루트 .env는 Supabase 설정이므로 읽지 않는다.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env.api",
        env_file_encoding="utf-8",
        env_prefix="BACKEND_",
        extra="ignore",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # 실행 환경. 지정하지 않으면 운영으로 보고 개발용 기능을 등록하지 않는다.
    env: Literal["development", "production"] = "production"
    # 개발용 확인 URL(/dev/...) 활성화. 개발 환경에서만 효과가 있다.
    enable_devtools: bool = Field(default=False, validation_alias="ENABLE_DEVTOOLS")

    # 공공데이터포털 일반 인증키(Decoding). 접두사 없이 읽고 실제 TAGO 호출 시점에만 확인한다.
    tago_service_key: SecretStr | None = Field(default=None, validation_alias="TAGO_SERVICE_KEY")
    tago_timeout_seconds: float = Field(default=10.0, gt=0)
    tago_request_interval_seconds: float = Field(default=0.5, ge=0)
    tago_max_retries: int = Field(default=2, ge=0, le=5)
    tago_retry_backoff_seconds: float = Field(default=1.0, ge=0)

    @field_validator("tago_service_key", mode="before")
    @classmethod
    def blank_key_is_missing(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @property
    def devtools_enabled(self) -> bool:
        return self.env == "development" and self.enable_devtools
