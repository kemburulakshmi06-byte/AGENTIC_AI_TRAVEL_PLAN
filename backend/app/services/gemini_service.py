from __future__ import annotations

import asyncio
import json
import re
from time import perf_counter
from typing import Any, TypeVar, overload

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from ..core.config import Settings, get_settings, _is_placeholder
from ..core.diagnostics import record_event
from ..core.retries import retry_after_seconds, retry_delay

ModelT = TypeVar("ModelT", bound=BaseModel)


class GeminiServiceError(RuntimeError):
    """Base class for sanitized Gemini service failures."""
    category = "provider_error"

    def __init__(self, message, *, status_code=None, category=None):
        self.status_code = status_code
        if category is not None:
            self.category = category
        super().__init__(message)


class GeminiNotConfiguredError(GeminiServiceError):
    """Raised when Gemini credentials are missing or are placeholders."""
    category = "authentication"


class GeminiTimeoutError(GeminiServiceError):
    """Raised when a Gemini request exceeds its configured timeout."""
    category = "timeout"


class GeminiRateLimitError(GeminiServiceError):
    """Raised when Gemini continues rate limiting after retries."""
    category = "rate_limit"


class GeminiModelUnavailableError(GeminiServiceError):
    """Raised when the configured model cannot serve the request."""
    category = "model_unavailable"


class GeminiAPIError(GeminiServiceError):
    """Raised for sanitized Gemini API or transport failures."""
    category = "http_error"


class GeminiInvalidResponseError(GeminiServiceError):
    """Raised when Gemini returns empty, malformed, or schema-invalid output."""
    category = "invalid_schema"


class GeminiUnavailableError(GeminiAPIError):
    category = "network"


class GeminiRefusalError(GeminiInvalidResponseError):
    category = "refusal"


class GeminiTruncatedResponseError(GeminiInvalidResponseError):
    category = "truncated"


class GeminiEmptyResponseError(GeminiInvalidResponseError):
    category = "empty_response"


class GeminiInvalidJSONError(GeminiInvalidResponseError):
    category = "invalid_json"


class GeminiService:
    """Lazily initialized asynchronous Google Gen AI content generation."""

    def __init__(
        self,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client: genai.Client | None = None
        self._closed = False
        self._last_retry_count = 0

    async def __aenter__(self) -> GeminiService:
        if self._closed:
            raise GeminiServiceError("Gemini service is closed.")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            await self._client.aio.aclose()
            self._client = None

    @overload
    async def generate_content(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str: ...

    @overload
    async def generate_content(
        self,
        prompt: str,
        *,
        response_model: type[ModelT],
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> ModelT: ...

    async def generate_content(
        self,
        prompt: str,
        *,
        response_model: type[ModelT] | None = None,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str | ModelT:
        """Generate text, or validate JSON output against a Pydantic model."""
        if not prompt.strip():
            raise ValueError("prompt must not be empty.")
        if system_instruction is not None and not system_instruction.strip():
            raise ValueError("system_instruction must not be empty.")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2.")
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive.")

        config_values: dict[str, Any] = {}
        if system_instruction is not None:
            config_values["system_instruction"] = system_instruction
        if temperature is not None:
            config_values["temperature"] = temperature
        if max_output_tokens is not None:
            config_values["max_output_tokens"] = max_output_tokens
        if response_model is not None:
            config_values["response_mime_type"] = "application/json"
            # Send JSON Schema directly. The installed SDK's OpenAPI Schema
            # conversion emits additional_properties, rejected by this API.
            config_values["response_json_schema"] = response_model.model_json_schema()

        started = perf_counter()
        self._last_retry_count = 0
        try:
            text = await self._generate(prompt, config_values)
        except GeminiServiceError as error:
            record_event("gemini", "generate_content", "failed", started,
                         category=error.category, status_code=error.status_code,
                         retry_count=self._last_retry_count)
            raise
        if response_model is None:
            record_event("gemini", "generate_content", "success", started, retry_count=self._last_retry_count)
            return text
        try:
            json.loads(text)
        except ValueError as exc:
            record_event("gemini", "validate_output", "failed", started, category="invalid_json", retry_count=self._last_retry_count)
            raise GeminiInvalidJSONError("Gemini returned invalid JSON.") from exc
        try:
            result = response_model.model_validate_json(text)
        except ValidationError as exc:
            record_event("gemini", "validate_output", "failed", started, category="invalid_schema",
                         retry_count=self._last_retry_count)
            raise GeminiInvalidResponseError(
                "Gemini returned JSON that did not match the requested schema."
            ) from exc
        record_event("gemini", "generate_content", "success", started, retry_count=self._last_retry_count)
        return result

    async def generate_structured(
        self,
        prompt: str,
        response_model: type[ModelT],
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> ModelT:
        """Generate JSON constrained by and validated against a Pydantic model."""
        result = await self.generate_content(
            prompt,
            response_model=response_model,
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        if not isinstance(result, response_model):
            raise GeminiInvalidResponseError(
                "Gemini returned a response in an unexpected format."
            )
        return result

    async def _generate(
        self,
        prompt: str,
        config_values: dict[str, Any],
    ) -> str:
        client = self._get_client()
        model = self.settings.GEMINI_MODEL.strip()
        if not model:
            raise GeminiModelUnavailableError(
                "GEMINI_MODEL is not configured."
            )
        config = types.GenerateContentConfig(**config_values) if config_values else None
        retry_count = self.settings.GEMINI_MAX_RETRIES

        for attempt in range(retry_count + 1):
            self._last_retry_count = attempt
            try:
                async with asyncio.timeout(self.settings.GEMINI_TIMEOUT_SECONDS):
                    response = await client.aio.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=config,
                    )
            except (TimeoutError, httpx.TimeoutException) as exc:
                if attempt < retry_count:
                    await self._retry_delay(attempt, category="timeout")
                    continue
                raise GeminiTimeoutError(
                    "Gemini request timed out."
                ) from exc
            except httpx.RequestError as exc:
                if attempt < retry_count:
                    await self._retry_delay(attempt, category="network")
                    continue
                raise GeminiUnavailableError(
                    "Gemini service is unavailable."
                ) from exc
            except errors.APIError as exc:
                status_code = _status_code(exc)
                if status_code == 429:
                    wait = _provider_retry_after(exc)
                    if attempt < retry_count and await self._retry_delay(attempt, category="rate_limit", status_code=429, retry_after=wait):
                        continue
                    raise GeminiRateLimitError(
                        "Gemini rate limit remains in effect after bounded retries.", status_code=429,
                    ) from exc
                if status_code is not None and 500 <= status_code < 600:
                    if attempt < retry_count and await self._retry_delay(attempt, category="http_error", status_code=status_code, retry_after=_provider_retry_after(exc)):
                        continue
                    raise GeminiAPIError(
                        "Gemini service is temporarily unavailable.", status_code=status_code,
                    ) from exc
                if status_code == 404:
                    raise GeminiModelUnavailableError(
                        "The configured Gemini model is unavailable.", status_code=404,
                    ) from exc
                if status_code in {401, 403}:
                    raise GeminiNotConfiguredError(
                        "Gemini rejected the configured credentials.", status_code=status_code,
                    ) from exc
                raise GeminiAPIError(
                    f"Gemini request failed (HTTP {status_code})."
                    if status_code is not None
                    else "Gemini request failed.", status_code=status_code,
                ) from exc

            feedback = getattr(response, "prompt_feedback", None)
            block = getattr(feedback, "block_reason", None)
            if block and str(getattr(block, "value", block)) not in {"BLOCK_REASON_UNSPECIFIED", "0"}:
                raise GeminiRefusalError("Gemini declined to generate AI suggestions.")
            candidates = getattr(response, "candidates", None) or []
            for candidate in candidates:
                reason = getattr(candidate, "finish_reason", None)
                reason = str(getattr(reason, "value", reason))
                if reason == "MAX_TOKENS":
                    raise GeminiTruncatedResponseError("Gemini output reached the token limit.")
                if reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}:
                    raise GeminiRefusalError("Gemini declined to generate AI suggestions.")
            text = getattr(response, "text", None)
            if not isinstance(text, str) or not text.strip():
                raise GeminiEmptyResponseError(
                    "Gemini returned an empty response."
                )
            return text.strip()

        raise GeminiAPIError("Gemini request could not be completed.")

    def _get_client(self) -> genai.Client:
        if self._closed:
            raise GeminiServiceError("Gemini service is closed.")
        if self._client is None:
            secret = self.settings.GEMINI_API_KEY
            if secret is None or _is_placeholder(secret.get_secret_value()):
                raise GeminiNotConfiguredError(
                    "GEMINI_API_KEY is not configured.", category="not_configured",
                )
            self._client = genai.Client(
                api_key=secret.get_secret_value(),
                http_options=types.HttpOptions(
                    timeout=round(self.settings.GEMINI_TIMEOUT_SECONDS * 1000),
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        return self._client

    @staticmethod
    async def _retry_delay(attempt: int, *, category="transient", status_code=None, retry_after=None) -> bool:
        delay = retry_delay(attempt, retry_after=retry_after)
        if delay is None:
            return False
        record_event("gemini", "generate_content", "retry", perf_counter(), retry_count=attempt + 1, category=category, status_code=status_code)
        await asyncio.sleep(delay)
        return True


def _status_code(error: errors.APIError) -> int | None:
    try:
        return int(error.code)
    except (TypeError, ValueError):
        return None


def _provider_retry_after(error):
    response = getattr(error, "response", None)
    header = retry_after_seconds(getattr(response, "headers", {}).get("Retry-After"))
    if header is not None:
        return header
    payload = getattr(error, "response_json", None) or getattr(error, "details", None)
    body = payload.get("error", {}) if isinstance(payload, dict) else {}
    details = body.get("details", []) if isinstance(body, dict) else []
    for detail in details if isinstance(details, list) else []:
        if isinstance(detail, dict) and str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
            value = str(detail.get("retryDelay", ""))
            if re.fullmatch(r"\d+(?:\.\d+)?s", value):
                return float(value[:-1])
    return None
