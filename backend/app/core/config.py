from functools import lru_cache
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from ..schemas.ai_cost_estimation import AICostCategory, AIUnitRateBounds

BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND_DIR / ".env"
DEVELOPMENT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
)


def _ai_unit_rate_bounds():
    """Broad validation policy in each currency, not fares or FX conversions."""
    policies = {
        "USD": ((10, 20000), (5, 10000), (1, 1000), (1, 5000), (1, 10000)),
        "AED": ((30, 75000), (20, 40000), (5, 4000), (2, 20000), (5, 40000)),
        "INR": ((500, 1500000), (200, 800000), (50, 100000), (50, 500000), (50, 1000000)),
    }
    categories = ("intercity_transport", "accommodation", "food", "local_transport", "activities")
    return {currency: {category: AIUnitRateBounds(minimum=low, maximum=high)
                       for category, (low, high) in zip(categories, bounds)}
            for currency, bounds in policies.items()}


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
    AI_COST_ESTIMATION_ENABLED: bool = False
    AI_COST_ESTIMATION_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=20, allow_inf_nan=False)
    AI_COST_ESTIMATION_RATE_BOUNDS: dict[str, dict[AICostCategory, AIUnitRateBounds]] = Field(default_factory=_ai_unit_rate_bounds)
    COST_REFERENCE_FILE: Path = BACKEND_DIR / "data" / "cost_references.json"
    DEFAULT_ESTIMATE_CONTINGENCY_PERCENT: Decimal = Field(default=Decimal("10"), ge=0, le=100)
    REFERENCE_FX_ENABLED: bool = True
    REFERENCE_FX_MAX_AGE_DAYS: int = Field(default=7, ge=1, le=30)
    AMADEUS_API_KEY: SecretStr | None = None
    AMADEUS_API_SECRET: SecretStr | None = None
    AMADEUS_ENVIRONMENT: Literal["test", "production"] = "test"
    TRAVELPAYOUTS_API_TOKEN: SecretStr | None = None
    TRAVELPAYOUTS_ENABLED: bool = False
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
        origins = [*self.CORS_ORIGINS, self.FRONTEND_URL]
        if self.APP_ENV == "development":
            origins.extend(DEVELOPMENT_CORS_ORIGINS)
        return list(dict.fromkeys(origins))

    @field_validator("CORS_ORIGINS", "FRONTEND_URL")
    @classmethod
    def require_explicit_cors_origins(cls, value):
        origins = value if isinstance(value, list) else [value]
        if any("*" in origin for origin in origins):
            raise ValueError("CORS requires explicit origins because credentials are enabled.")
        return value

    @field_validator("AI_COST_ESTIMATION_RATE_BOUNDS")
    @classmethod
    def validate_ai_bound_currencies(cls, value):
        if any(len(currency) != 3 or not currency.isascii() or not currency.isupper()
               or not currency.isalpha() for currency in value):
            raise ValueError("AI sanity policies require uppercase currency codes.")
        return value

    def require_integration_settings(self, *integrations: str) -> None:
        required_settings = {
            "gemini": ("GEMINI_API_KEY",),
            "geoapify": ("GEOAPIFY_API_KEY",),
            "openrouteservice": ("OPENROUTESERVICE_API_KEY",),
            "tavily": ("TAVILY_API_KEY",),
            "travelpayouts": ("TRAVELPAYOUTS_API_TOKEN",),
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
