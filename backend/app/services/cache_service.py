from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from ..core.config import Settings, get_settings
from .redis_client import CACHE_MISS, RedisClientError, UpstashRedisClient

CACHE_MISS_VALUE = CACHE_MISS
DEFAULT_TTLS = {
    "destination": 86_400,
    "weather": 1_800,
    "routes": 21_600,
    "places": 86_400,
    "search": 3_600,
}
_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "email",
    "phone",
    "traveler_name",
    "full_name",
    "user_name",
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(bearer\s+\S+|(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+)"
)


class CacheService:
    def __init__(
        self,
        redis: UpstashRedisClient | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._redis = redis or UpstashRedisClient(self._settings)

    async def aclose(self) -> None:
        await self._redis.aclose()

    @staticmethod
    def key_for(namespace: str, identifier: Any) -> str:
        if namespace not in DEFAULT_TTLS:
            raise ValueError(f"Unsupported cache namespace: {namespace}")
        canonical = json.dumps(
            identifier,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"travel-planner:{namespace}:{digest}"

    async def get_cached(self, namespace: str, identifier: Any) -> Any:
        key = self.key_for(namespace, identifier)
        try:
            return await self._redis.get(key)
        except RedisClientError:
            return CACHE_MISS

    async def set_cached(
        self,
        namespace: str,
        identifier: Any,
        data: Any,
        ttl_seconds: int | None = None,
    ) -> bool:
        _ensure_safe_data(data)
        if namespace not in DEFAULT_TTLS:
            raise ValueError(f"Unsupported cache namespace: {namespace}")
        ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTLS[namespace]
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive.")
        try:
            return await self._redis.set(
                self.key_for(namespace, identifier), data, ttl
            )
        except RedisClientError:
            return False

    async def invalidate_cache(self, namespace: str, identifier: Any) -> bool:
        try:
            return await self._redis.delete(self.key_for(namespace, identifier))
        except RedisClientError:
            return False

    async def get_or_set(
        self,
        namespace: str,
        identifier: Any,
        loader: Callable[[], Awaitable[Any]],
        ttl_seconds: int | None = None,
    ) -> Any:
        cached = await self.get_cached(namespace, identifier)
        if cached is not CACHE_MISS:
            return cached
        value = await loader()
        await self.set_cached(namespace, identifier, value, ttl_seconds)
        return value


def _ensure_safe_data(value: Any, path: str = "value") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
                raise ValueError(
                    f"Sensitive field is not cacheable: {path}.{key}"
                )
            _ensure_safe_data(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_safe_data(item, f"{path}[{index}]")
    elif isinstance(value, str) and _SENSITIVE_VALUE.search(value):
        raise ValueError(f"Sensitive value is not cacheable: {path}")
