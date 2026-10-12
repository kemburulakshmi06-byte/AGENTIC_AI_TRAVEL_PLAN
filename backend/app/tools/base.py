from __future__ import annotations

import asyncio
from decimal import Decimal
from time import perf_counter
from urllib.parse import urlsplit
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx
from pydantic import SecretStr

from ..core.config import Settings, get_settings, _is_placeholder
from ..core.diagnostics import record_event
from ..core.retries import retry_after_seconds, retry_delay
from ..services.cache_service import CACHE_MISS_VALUE, CacheService

T = TypeVar("T")


class TravelAPIError(RuntimeError):
    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        category: str = "provider_error",
        retry_after: float | None = None,
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        self.category = category
        self.retry_after = retry_after
        super().__init__(f"{provider}: {message}")


class TravelAPIAuthenticationError(TravelAPIError):
    pass


class TravelAPIRateLimitError(TravelAPIError):
    pass


class TravelAPITimeoutError(TravelAPIError):
    pass


class TravelAPIUnavailableError(TravelAPIError):
    pass


class TravelAPIResponseError(TravelAPIError):
    def __init__(self, provider, message, **kwargs):
        kwargs.setdefault("category", "invalid_schema")
        super().__init__(provider, message, **kwargs)


class TravelAPITool:
    provider = "external"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        cache: CacheService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        self.cache = cache or CacheService(settings=self.settings)

    async def __aenter__(self) -> TravelAPITool:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        await self.cache.aclose()

    def api_key(self, setting_name: str) -> str:
        secret: SecretStr | None = getattr(self.settings, setting_name)
        if secret is None or _is_placeholder(secret.get_secret_value()):
            raise TravelAPIAuthenticationError(
                self.provider,
                f"{setting_name} is not configured.",
                category="not_configured",
            )
        return secret.get_secret_value()

    async def request_json(
        self, method: str, url: str, **kwargs: Any,
    ) -> dict[str, Any]:
        started = perf_counter()
        operation = method + " " + urlsplit(url).path
        for attempt in range(self.settings.EXTERNAL_API_MAX_RETRIES + 1):
            try:
                result = await self._request_once(method, url, **kwargs)
            except TravelAPIError as error:
                retryable = error.category in {"timeout", "network", "rate_limit"} or (
                    error.status_code is not None and 500 <= error.status_code < 600
                )
                delay = retry_delay(attempt, retry_after=error.retry_after)
                if retryable and attempt < self.settings.EXTERNAL_API_MAX_RETRIES and delay is not None:
                    record_event(self.provider, operation, "retry", started, category=error.category,
                                 retry_count=attempt + 1, status_code=error.status_code)
                    await asyncio.sleep(delay)
                    continue
                record_event(self.provider, operation, "failed", started, category=error.category,
                             retry_count=attempt, status_code=error.status_code)
                raise
            record_event(self.provider, operation, "success", started, retry_count=attempt)
            return result
        raise AssertionError("Unreachable retry state")

    async def _request_once(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, str] | None = None,
        decimal_json: bool = False,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        client = self._get_client()
        try:
            response = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                data=form_body,
                headers=headers,
                timeout=self.settings.EXTERNAL_API_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            raise TravelAPITimeoutError(
                self.provider, "request timed out.", category="timeout"
            ) from exc
        except httpx.RequestError as exc:
            raise TravelAPIUnavailableError(
                self.provider, "request could not reach the provider.", category="network"
            ) from exc

        if response.status_code in {401, 403}:
            raise TravelAPIAuthenticationError(
                self.provider,
                "provider rejected authentication.",
                status_code=response.status_code,
                category="authentication",
            )
        if response.status_code == 429:
            raise TravelAPIRateLimitError(
                self.provider,
                "provider rate limit reached.",
                status_code=429,
                category="rate_limit",
                retry_after=retry_after_seconds(response.headers.get("Retry-After")),
            )
        if not response.is_success:
            category = "http_error"
            if self.provider == "openrouteservice" and response.status_code in {400, 404}:
                try:
                    body = response.json()
                    if isinstance(body, dict) and isinstance(body.get("error"), dict) and body["error"].get("code") in {2009, 2010, 2011}:
                        category = "no_route"
                except ValueError:
                    pass
            raise TravelAPIResponseError(
                self.provider,
                "provider returned an unsuccessful response.",
                status_code=response.status_code,
                category=category,
                retry_after=retry_after_seconds(response.headers.get("Retry-After")),
            )
        try:
            payload = response.json(parse_float=Decimal) if decimal_json else response.json()
        except ValueError as exc:
            raise TravelAPIResponseError(
                self.provider, "provider returned invalid JSON.", category="invalid_json"
            ) from exc
        if not isinstance(payload, dict):
            raise TravelAPIResponseError(
                self.provider, "provider returned an invalid response.", category="invalid_shape"
            )
        return payload

    async def cached(
        self,
        namespace: str,
        identifier: dict[str, Any],
        parser: Callable[[Any], T],
        loader: Callable[[], Awaitable[T | None]],
        *,
        ttl_seconds: int | None = None,
        serializer: Callable[[T | None], Any] | None = None,
    ) -> T | None:
        cached = await self.cache.get_cached(namespace, identifier)
        if cached is not CACHE_MISS_VALUE:
            try:
                return None if cached is None else parser(cached)
            except (ValueError, TypeError, KeyError):
                record_event(self.provider, namespace + ".cache", "failed", perf_counter(), category="invalid_schema")

        result = await loader()
        try:
            cache_value = serializer(result) if serializer is not None else (
                result.model_dump(mode="json") if hasattr(result, "model_dump") else (
                    [item.model_dump(mode="json") for item in result]
                    if isinstance(result, list)
                    else result
                )
            )
            await self.cache.set_cached(
                namespace,
                identifier,
                cache_value,
                ttl_seconds=ttl_seconds,
            )
        except ValueError:
            # A response rejected by the shared sensitive-data guard is still
            # returned to the caller; it is simply not cached.
            pass
        return result

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client
