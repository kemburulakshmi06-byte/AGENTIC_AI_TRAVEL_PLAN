from functools import lru_cache
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "Agentic AI Travel Planner"
    APP_ENV: Literal["development", "testing", "production"] = "development"
    # Use an application-specific name. Generic DEBUG variables are commonly
    # injected by shells and hosting tools with non-boolean values.
    APP_DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    FRONTEND_URL: str = "http://localhost:5173"
    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    GEMINI_API_KEY: SecretStr | None = None
    GEMINI_MODEL: str = "gemini-2.5-flash-lite"
    GEMINI_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0, le=120)
    GEMINI_MAX_RETRIES: int = Field(default=2, ge=0, le=5)
    GEOAPIFY_API_KEY: SecretStr | None = None
    OPENROUTESERVICE_API_KEY: SecretStr | None = None
    TAVILY_API_KEY: SecretStr | None = None
    AUTOMATIC_COST_ESTIMATION_ENABLED: bool = True
    COST_REFERENCE_FILE: Path = BACKEND_DIR / "data" / "cost_references.json"
    DEFAULT_ESTIMATE_CONTINGENCY_PERCENT: Decimal = Field(default=Decimal("10"), ge=0, le=100)
    REFERENCE_FX_ENABLED: bool = True
    REFERENCE_FX_MAX_AGE_DAYS: int = Field(default=7, ge=1, le=30)
    AMADEUS_API_KEY: SecretStr | None = None
    AMADEUS_API_SECRET: SecretStr | None = None
    AMADEUS_ENVIRONMENT: Literal["test", "production"] = "test"
    DATABASE_URL: SecretStr | None = None
    TEST_DATABASE_URL: SecretStr | None = None
    UPSTASH_REDIS_REST_URL: SecretStr | None = None
    UPSTASH_REDIS_REST_TOKEN: SecretStr | None = None
    UPSTASH_REDIS_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0, le=60)
    REDIS_TASK_STATUS_TTL_SECONDS: int = Field(default=86_400, gt=0)
    EXTERNAL_API_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=120)
    EXTERNAL_API_MAX_RETRIES: int = Field(default=2, ge=0, le=3)
    TRIP_WORKFLOW_TIMEOUT_SECONDS: float = Field(default=180.0, gt=0, le=1800)
    TRIP_AGENT_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0, le=600)
    TRIP_MAX_REVISIONS: int = Field(default=2, ge=0, le=5)
    TRIP_ADMIN_API_ENABLED: bool = False
    TRIP_HISTORY_API_ENABLED: bool = False
    TRIP_EXECUTION_MODE: Literal["database_worker", "background"] = "database_worker"
    TRIP_WORKER_ENABLED: bool = True
    TRIP_JOB_LEASE_SECONDS: float = Field(default=45, ge=10, le=300)
    TRIP_JOB_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    TRIP_WORKER_POLL_SECONDS: float = Field(default=2, gt=0, le=30)

    @property
    def allowed_origins(self) -> list[str]:
        return list(dict.fromkeys([*self.CORS_ORIGINS, self.FRONTEND_URL]))

    def require_integration_settings(self, *integrations: str) -> None:
        required_settings = {
            "gemini": ("GEMINI_API_KEY",),
            "geoapify": ("GEOAPIFY_API_KEY",),
            "openrouteservice": ("OPENROUTESERVICE_API_KEY",),
            "tavily": ("TAVILY_API_KEY",),
            "database": ("DATABASE_URL",),
            "upstash_redis": (
                "UPSTASH_REDIS_REST_URL",
                "UPSTASH_REDIS_REST_TOKEN",
            ),
        }
        missing: list[str] = []

        for integration in integrations:
            if integration not in required_settings:
                raise ValueError(f"Unknown integration: {integration}")
            for setting_name in required_settings[integration]:
                value = getattr(self, setting_name)
                if value is None or _is_placeholder(value.get_secret_value()):
                    missing.append(setting_name)

        if missing:
            names = ", ".join(dict.fromkeys(missing))
            raise ValueError(f"Missing required settings: {names}")


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return not normalized or normalized.startswith(
        ("replace_with_", "your_", "placeholder")
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
