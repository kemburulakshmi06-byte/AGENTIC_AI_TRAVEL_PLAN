from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.main import app, create_app

client = TestClient(app)


def test_health_returns_healthy_status() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_api_status_returns_safe_service_information() -> None:
    response = client.get("/api/v1/status")

    assert response.status_code == 200
    assert response.json() == {
        "service": "Agentic AI Travel Planner",
        "status": "healthy",
        "environment": "development",
    }


def test_docs_and_openapi_are_available() -> None:
    assert client.get("/docs").status_code == 200

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/status" in response.json()["paths"]


def test_cors_allows_local_frontend_origin() -> None:
    response = client.get(
        "/health",
        headers={"Origin": "http://localhost:5173"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


LOCAL_FRONTEND_ORIGINS = (
    "http://localhost:5173", "http://localhost:5174",
    "http://127.0.0.1:5173", "http://127.0.0.1:5174",
)


@pytest.mark.parametrize("origin", LOCAL_FRONTEND_ORIGINS)
def test_local_trip_post_preflight_succeeds_with_environment_override(origin, monkeypatch):
    # Reproduce a local .env override that accidentally omits Vite's active port.
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:5175"]')
    settings = Settings(_env_file=None, APP_ENV="development", FRONTEND_URL="http://localhost:5175",
                        DATABASE_URL=None, TRIP_WORKER_ENABLED=False)
    local_client = TestClient(create_app(settings))
    response = local_client.options("/api/v1/trips/plan", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "POST" in response.headers["access-control-allow-methods"].split(", ")
    assert "content-type" in response.headers["access-control-allow-headers"].lower()
    assert response.headers["access-control-allow-credentials"] == "true"
    assert local_client.get("/health").json() == {"status": "healthy"}
    assert "post" in local_client.get("/openapi.json").json()["paths"]["/api/v1/trips/plan"]


@pytest.mark.parametrize("origin", ["https://unexpected.example", "http://localhost:5174.evil.example", "http://localhost:9999"])
def test_cors_rejects_unconfigured_origins(origin):
    local_client = TestClient(create_app(Settings(_env_file=None, APP_ENV="development", CORS_ORIGINS=[],
                                                  FRONTEND_URL="http://localhost:5173", DATABASE_URL=None)))
    response = local_client.options("/api/v1/trips/plan", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
    assert response.text == "Disallowed CORS origin"


@pytest.mark.parametrize("origin", LOCAL_FRONTEND_ORIGINS)
def test_production_does_not_automatically_allow_development_origins(origin):
    production_client = TestClient(create_app(Settings(_env_file=None, APP_ENV="production",
        CORS_ORIGINS=["https://travel.example"], FRONTEND_URL="https://travel.example", DATABASE_URL=None)))
    headers = {"Origin": origin, "Access-Control-Request-Method": "POST",
               "Access-Control-Request-Headers": "content-type"}
    rejected = production_client.options("/api/v1/trips/plan", headers=headers)
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers
    allowed = production_client.options("/api/v1/trips/plan", headers={**headers, "Origin": "https://travel.example"})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://travel.example"


@pytest.mark.parametrize("setting", [{"CORS_ORIGINS": ["*"]}, {"FRONTEND_URL": "*"}])
def test_wildcard_origins_are_rejected_with_credentials(setting):
    with pytest.raises(ValidationError, match="explicit origins"):
        Settings(_env_file=None, **setting)


def test_missing_optional_api_keys_do_not_break_app_startup() -> None:
    settings = Settings(_env_file=None)
    test_app = create_app(settings)

    with TestClient(test_app) as test_client:
        assert test_client.get("/health").status_code == 200

    assert settings.GEMINI_API_KEY is None
    assert settings.GEOAPIFY_API_KEY is None


def test_status_does_not_expose_sensitive_configuration() -> None:
    secret = "sensitive-test-value"
    settings = Settings(
        _env_file=None,
        GEMINI_API_KEY=secret,
        DATABASE_URL=f"postgresql://user:{secret}@host/database",
        UPSTASH_REDIS_REST_TOKEN=secret,
    )
    test_app = create_app(settings)

    with TestClient(test_app) as test_client:
        response = test_client.get("/api/v1/status")

    assert response.status_code == 200
    assert secret not in response.text
    assert "postgresql://" not in response.text


def test_unexpected_errors_return_safe_json() -> None:
    test_app = create_app(Settings(_env_file=None))

    @test_app.get("/test-error")
    def raise_unexpected_error() -> None:
        raise RuntimeError("sensitive-internal-detail")

    with TestClient(test_app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/test-error")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_server_error",
            "message": "An unexpected error occurred.",
        }
    }
    assert "sensitive-internal-detail" not in response.text


def test_debug_mode_does_not_return_tracebacks() -> None:
    test_app = create_app(Settings(_env_file=None, APP_DEBUG=True))

    @test_app.get("/test-debug-error")
    def raise_debug_error() -> None:
        raise RuntimeError("sensitive-traceback-detail")

    with TestClient(test_app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/test-debug-error")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_server_error"
    assert "sensitive-traceback-detail" not in response.text


def test_unrelated_debug_environment_variable_does_not_break_settings(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEBUG", "release")

    settings = Settings(_env_file=None)

    assert settings.APP_DEBUG is False
