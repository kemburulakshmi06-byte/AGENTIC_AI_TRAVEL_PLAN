from fastapi.testclient import TestClient

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
