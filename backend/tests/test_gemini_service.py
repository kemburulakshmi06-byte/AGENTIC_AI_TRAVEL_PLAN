import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, SecretStr

from app.core.config import Settings
from app.services.gemini_service import (
    GeminiAPIError,
    GeminiInvalidResponseError,
    GeminiModelUnavailableError,
    GeminiNotConfiguredError,
    GeminiRateLimitError,
    GeminiService,
    GeminiTimeoutError,
)


class SampleResponse(BaseModel):
    status: str
    count: int


class FakeModels:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeAioClient:
    def __init__(self, models: FakeModels) -> None:
        self.models = models
        self.closed = False
        self.close_loop: asyncio.AbstractEventLoop | None = None

    async def aclose(self) -> None:
        self.close_loop = asyncio.get_running_loop()
        self.closed = True


class FakeClient:
    def __init__(self, models: FakeModels) -> None:
        self.aio = FakeAioClient(models)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "GEMINI_API_KEY": SecretStr("unit-test-key"),
        "GEMINI_MODEL": "gemini-test-model",
        "GEMINI_TIMEOUT_SECONDS": 0.1,
        "GEMINI_MAX_RETRIES": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def install_fake_client(
    monkeypatch: pytest.MonkeyPatch, responses: list[Any]
) -> tuple[FakeClient, FakeModels, list[dict[str, Any]]]:
    models = FakeModels(responses)
    client = FakeClient(models)
    init_calls: list[dict[str, Any]] = []

    def client_factory(**kwargs: Any) -> FakeClient:
        init_calls.append(kwargs)
        return client

    monkeypatch.setattr(genai, "Client", client_factory)
    return client, models, init_calls


def test_generate_content_returns_text_and_uses_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        client, models, init_calls = install_fake_client(
            monkeypatch, [SimpleNamespace(text="  A short answer.  ")]
        )
        service = GeminiService(settings())
        assert service._client is None
        result = await service.generate_content(
            "Say hello",
            system_instruction="Be concise",
            temperature=0.2,
            max_output_tokens=32,
        )
        assert result == "A short answer."
        assert len(init_calls) == 1
        assert init_calls[0]["http_options"].timeout == 100
        call = models.calls[0]
        assert call["model"] == "gemini-test-model"
        assert call["contents"] == "Say hello"
        assert call["config"].system_instruction == "Be concise"
        assert call["config"].temperature == 0.2
        assert client.aio.closed is False
        await service.aclose()
        assert client.aio.closed is True

    asyncio.run(run())


def test_async_context_closes_client_on_generation_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        client, models, _ = install_fake_client(
            monkeypatch, [SimpleNamespace(text="OK")]
        )
        generation_loops: list[asyncio.AbstractEventLoop] = []
        original_generate = models.generate_content

        async def generate_content(**kwargs: Any) -> Any:
            generation_loops.append(asyncio.get_running_loop())
            return await original_generate(**kwargs)

        models.generate_content = generate_content
        async with GeminiService(settings()) as service:
            assert await service.generate_content("Reply OK") == "OK"

        assert client.aio.closed is True
        assert len(generation_loops) == 1
        assert client.aio.close_loop is generation_loops[0]

    asyncio.run(run())


def test_generate_structured_requests_json_and_validates_pydantic_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        _, models, _ = install_fake_client(
            monkeypatch, [SimpleNamespace(text='{"status":"ok","count":1}')]
        )
        service = GeminiService(settings())
        result = await service.generate_structured(
            "Return a small status object.", SampleResponse
        )
        assert result == SampleResponse(status="ok", count=1)
        config = models.calls[0]["config"]
        assert isinstance(config, types.GenerateContentConfig)
        assert config.response_mime_type == "application/json"
        assert config.response_schema is None
        assert config.response_json_schema == SampleResponse.model_json_schema()
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "response_text",
    ['not json', '{"status":"ok","count":"not-an-integer"}'],
)
def test_structured_generation_rejects_malformed_or_schema_invalid_json(
    monkeypatch: pytest.MonkeyPatch, response_text: str
) -> None:
    async def run() -> None:
        install_fake_client(monkeypatch, [SimpleNamespace(text=response_text)])
        service = GeminiService(settings())
        with pytest.raises(GeminiInvalidResponseError):
            await service.generate_structured("Return JSON.", SampleResponse)
        await service.aclose()

    asyncio.run(run())


def test_missing_api_key_does_not_initialize_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        calls: list[dict[str, Any]] = []

        def fail_if_called(**kwargs: Any) -> None:
            calls.append(kwargs)
            raise AssertionError("client should not be initialized")

        monkeypatch.setattr(genai, "Client", fail_if_called)
        service = GeminiService(settings(GEMINI_API_KEY=None))
        with pytest.raises(GeminiNotConfiguredError, match="not configured"):
            await service.generate_content("Hello")
        assert calls == []

    asyncio.run(run())


def test_placeholder_key_is_rejected_without_initializing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        install_fake_client(monkeypatch, [])
        service = GeminiService(
            settings(GEMINI_API_KEY=SecretStr("replace_with_gemini_api_key"))
        )
        with pytest.raises(GeminiNotConfiguredError):
            await service.generate_content("Hello")

    asyncio.run(run())


def test_timeouts_are_sanitized_and_retried_to_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        _, models, _ = install_fake_client(
            monkeypatch, [TimeoutError(), SimpleNamespace(text="Recovered.")]
        )
        service = GeminiService(settings(GEMINI_MAX_RETRIES=1))
        assert await service.generate_content("Try safely.") == "Recovered."
        assert len(models.calls) == 2
        await service.aclose()

        install_fake_client(monkeypatch, [httpx.ReadTimeout("private detail")])
        service = GeminiService(settings())
        with pytest.raises(GeminiTimeoutError) as raised:
            await service.generate_content("Timeout.")
        assert "private detail" not in str(raised.value)
        await service.aclose()

    asyncio.run(run())


def test_rate_limit_retries_then_raises_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        rate_limited = errors.APIError(429, {"error": {"message": "sensitive body"}})
        install_fake_client(monkeypatch, [rate_limited])
        service = GeminiService(settings())
        with pytest.raises(GeminiRateLimitError) as raised:
            await service.generate_content("Rate-limited.")
        assert "sensitive body" not in str(raised.value)
        await service.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (404, GeminiModelUnavailableError),
        (403, GeminiNotConfiguredError),
        (503, GeminiAPIError),
    ],
)
def test_api_failures_are_sanitized_and_classified(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_error: type[Exception],
) -> None:
    async def run() -> None:
        api_error = errors.APIError(
            status, {"error": {"message": "private provider details"}}
        )
        install_fake_client(monkeypatch, [api_error])
        service = GeminiService(settings())
        with pytest.raises(expected_error) as raised:
            await service.generate_content("Handle provider error.")
        assert "private provider details" not in str(raised.value)
        await service.aclose()

    asyncio.run(run())


def test_empty_response_and_closed_service_are_reported_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        client, _, _ = install_fake_client(monkeypatch, [SimpleNamespace(text=None)])
        service = GeminiService(settings())
        with pytest.raises(GeminiInvalidResponseError):
            await service.generate_content("Empty response.")
        await service.aclose()
        assert client.aio.closed is True
        with pytest.raises(GeminiServiceError):
            await service.generate_content("Closed service.")

    from app.services.gemini_service import GeminiServiceError

    asyncio.run(run())
