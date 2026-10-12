from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import SecretStr

from ..core.config import Settings, get_settings


class RedisClientError(RuntimeError):
    """Base error for sanitized Upstash REST failures."""


class RedisNotConfiguredError(RedisClientError):
    """Raised when Upstash credentials are unavailable."""


class RedisUnavailableError(RedisClientError):
    """Raised when the Upstash REST endpoint cannot be reached."""


class RedisResponseError(RedisClientError):
    """Raised when Upstash returns an unsuccessful or invalid response."""


class _CacheMiss:
    __slots__ = ()


# GET returns JSON null for a missing key. A stored JSON null is Redis string
# "null", which is decoded to Python None, so this sentinel remains unambiguous.
CACHE_MISS = _CacheMiss()


class UpstashRedisClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None
        self._closed = False

    async def __aenter__(self) -> UpstashRedisClient:
        self._ensure_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> str:
        return await self._command("PING")

    async def get(self, key: str) -> Any:
        value = await self._command("GET", key)
        if value is None:
            return CACHE_MISS
        if not isinstance(value, str):
            raise RedisResponseError("Upstash GET returned an invalid value.")
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise RedisResponseError("Upstash GET returned invalid JSON.") from exc

    async def set(self, key: str, value: Any, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive.")
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        result = await self._command("SET", key, encoded, "EX", str(ttl_seconds))
        return result == "OK"

    async def delete(self, key: str) -> bool:
        result = await self._command("DEL", key)
        return isinstance(result, int) and result > 0

    async def exists(self, key: str) -> bool:
        result = await self._command("EXISTS", key)
        return isinstance(result, int) and result > 0

    async def eval(
        self, script: str, keys: Sequence[str], arguments: Sequence[str]
    ) -> Any:
        result = await self._command(
            "EVAL", script, str(len(keys)), *keys, *arguments
        )
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return result
        return result

    async def _command(self, *command: str) -> Any:
        client = self._ensure_client()
        url = self._rest_url()
        token = self._token()
        try:
            response = await client.post(
                url,
                json=list(command),
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._settings.UPSTASH_REDIS_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            raise RedisUnavailableError(
                "Upstash Redis REST request timed out."
            ) from exc
        except httpx.RequestError as exc:
            raise RedisUnavailableError(
                "Upstash Redis REST endpoint is unavailable."
            ) from exc

        if not response.is_success:
            raise RedisResponseError(
                f"Upstash Redis REST returned HTTP {response.status_code}."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RedisResponseError(
                "Upstash Redis REST returned an invalid response."
            ) from exc
        if not isinstance(payload, dict):
            raise RedisResponseError("Upstash Redis REST returned an invalid response.")
        if "error" in payload:
            raise RedisResponseError("Upstash Redis REST reported a command error.")
        if "result" not in payload:
            raise RedisResponseError("Upstash Redis REST response has no result.")
        return payload["result"]

    def _ensure_client(self) -> httpx.AsyncClient:
        self._ensure_open()
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    def _rest_url(self) -> str:
        configured_url: SecretStr | None = self._settings.UPSTASH_REDIS_REST_URL
        if configured_url is None or not configured_url.get_secret_value().strip():
            raise RedisNotConfiguredError("Upstash Redis REST URL is not configured.")
        return configured_url.get_secret_value().strip().rstrip("/")

    def _token(self) -> str:
        configured_token: SecretStr | None = self._settings.UPSTASH_REDIS_REST_TOKEN
        if configured_token is None or not configured_token.get_secret_value().strip():
            raise RedisNotConfiguredError(
                "Upstash Redis REST token is not configured."
            )
        return configured_token.get_secret_value()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RedisClientError("Upstash Redis client is closed.")
