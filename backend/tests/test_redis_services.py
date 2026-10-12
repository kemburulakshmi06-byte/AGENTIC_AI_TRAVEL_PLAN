import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.services.cache_service import CACHE_MISS_VALUE, CacheService
from app.services.redis_client import (
    CACHE_MISS,
    RedisClientError,
    RedisNotConfiguredError,
    RedisResponseError,
    RedisUnavailableError,
    UpstashRedisClient,
)
from app.services.task_status_service import TaskStatus, TaskStatusService


def settings_with_redis() -> Settings:
    return Settings(
        _env_file=None,
        UPSTASH_REDIS_REST_URL=SecretStr("https://redis.example"),
        UPSTASH_REDIS_REST_TOKEN=SecretStr("unit-test-token"),
    )


def response(result: object) -> httpx.Response:
    return httpx.Response(200, json={"result": result})


def test_redis_ping_returns_pong() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/"
            assert json.loads(request.content) == ["PING"]
            assert request.headers["Authorization"] == "Bearer unit-test-token"
            return response("PONG")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            redis = UpstashRedisClient(settings_with_redis(), client=client)
            assert await redis.ping() == "PONG"

    asyncio.run(run())


def test_redis_json_set_get_and_expiration_command() -> None:
    async def run() -> None:
        stored: dict[str, str] = {}
        commands: list[list[str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            command = json.loads(request.content)
            commands.append(command)
            if command[0] == "SET":
                stored[command[1]] = command[2]
                assert command[3:] == ["EX", "123"]
                return response("OK")
            if command[0] == "GET":
                return response(stored.get(command[1]))
            raise AssertionError("Unexpected command")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            redis = UpstashRedisClient(settings_with_redis(), client=client)
            value = {"places": [{"name": "Museum"}], "empty": []}
            assert await redis.set("sample", value, 123)
            assert await redis.get("sample") == value
            assert commands[0] == [
                "SET",
                "sample",
                json.dumps(value, separators=(",", ":")),
                "EX",
                "123",
            ]

    asyncio.run(run())


def test_redis_get_distinguishes_missing_from_falsey_values() -> None:
    async def run() -> None:
        stored_values = {
            "null": "null",
            "false": "false",
            "zero": "0",
            "empty-string": '""',
            "empty-object": "{}",
            "empty-array": "[]",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            key = json.loads(request.content)[1]
            return response(None if key == "missing" else stored_values[key])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            redis = UpstashRedisClient(settings_with_redis(), client=client)
            assert await redis.get("missing") is CACHE_MISS
            assert await redis.get("null") is None
            assert await redis.get("false") is False
            assert await redis.get("zero") == 0
            assert await redis.get("empty-string") == ""
            assert await redis.get("empty-object") == {}
            assert await redis.get("empty-array") == []

    asyncio.run(run())


def test_redis_delete_and_exists_commands() -> None:
    async def run() -> None:
        commands: list[list[str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            command = json.loads(request.content)
            commands.append(command)
            return response(1 if command[0] == "EXISTS" else 0)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            redis = UpstashRedisClient(settings_with_redis(), client=client)
            assert await redis.exists("present")
            assert not await redis.delete("absent")
            assert commands == [["EXISTS", "present"], ["DEL", "absent"]]

    asyncio.run(run())


def test_redis_connection_failure_is_sanitized() -> None:
    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("sensitive host and credentials")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            redis = UpstashRedisClient(settings_with_redis(), client=client)
            with pytest.raises(RedisUnavailableError) as error:
                await redis.ping()
            assert str(error.value) == "Upstash Redis REST endpoint is unavailable."
            assert "sensitive" not in str(error.value)

    asyncio.run(run())


def test_redis_http_and_upstash_payload_errors_are_sanitized() -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(503, text="secret response")
            )
        ) as client:
            redis = UpstashRedisClient(settings_with_redis(), client=client)
            with pytest.raises(RedisResponseError, match="HTTP 503"):
                await redis.ping()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200, json={"error": "credential-containing upstream detail"}
                )
            )
        ) as client:
            redis = UpstashRedisClient(settings_with_redis(), client=client)
            with pytest.raises(RedisResponseError) as error:
                await redis.ping()
            assert "credential-containing" not in str(error.value)

    asyncio.run(run())


def test_missing_redis_configuration_fails_without_network() -> None:
    async def run() -> None:
        redis = UpstashRedisClient(Settings(_env_file=None))
        with pytest.raises(RedisNotConfiguredError):
            await redis.ping()
        assert await CacheService(redis).get_cached("weather", "Paris") is CACHE_MISS
        service = TaskStatusService(redis)
        assert await service.create_task_status(uuid4(), uuid4()) is None

    asyncio.run(run())


def test_cache_key_is_deterministic_namespaced_and_hashed() -> None:
    first = CacheService.key_for("weather", {"city": "Paris", "units": "metric"})
    second = CacheService.key_for("weather", {"units": "metric", "city": "Paris"})

    assert first == second
    assert first.startswith("travel-planner:weather:")
    assert len(first.rsplit(":", 1)[1]) == 64
    assert "Paris" not in first
    with pytest.raises(ValueError):
        CacheService.key_for("unknown", "id")


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[tuple[object, ...]] = []
        self.error: RedisClientError | None = None

    async def get(self, key: str) -> object:
        self.calls.append(("get", key))
        if self.error:
            raise self.error
        return self.values.get(key, CACHE_MISS)

    async def set(self, key: str, value: object, ttl_seconds: int) -> bool:
        self.calls.append(("set", key, value, ttl_seconds))
        if self.error:
            raise self.error
        self.values[key] = value
        self.ttls[key] = ttl_seconds
        return True

    async def delete(self, key: str) -> bool:
        self.calls.append(("delete", key))
        if self.error:
            raise self.error
        return self.values.pop(key, CACHE_MISS) is not CACHE_MISS

    async def exists(self, _key: str) -> bool:
        return False

    async def eval(
        self, _script: str, _keys: list[str], _arguments: list[str]
    ) -> object:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def test_cache_hit_miss_falsey_values_and_unavailable_redis() -> None:
    async def run() -> None:
        fake = FakeRedis()
        cache = CacheService(fake)  # type: ignore[arg-type]
        assert await cache.get_cached("search", "term") is CACHE_MISS_VALUE
        key = cache.key_for("search", "term")
        for value in (False, 0, "", {}, [], None):
            fake.values[key] = value
            assert await cache.get_cached("search", "term") == value
            assert await cache.get_cached("search", "term") is not CACHE_MISS_VALUE
        fake.error = RedisUnavailableError("offline")
        assert await cache.get_cached("search", "term") is CACHE_MISS_VALUE
        assert not await cache.set_cached("search", "term", [])

    asyncio.run(run())


def test_cache_ttl_override_cache_aside_and_invalidation() -> None:
    async def run() -> None:
        fake = FakeRedis()
        cache = CacheService(fake)  # type: ignore[arg-type]
        identifier = {"query": "museum"}
        called = 0

        async def loader() -> list[str]:
            nonlocal called
            called += 1
            return ["Museum"]

        result = await cache.get_or_set(
            "places", identifier, loader, ttl_seconds=77
        )
        key = cache.key_for("places", identifier)
        assert result == ["Museum"]
        assert fake.ttls[key] == 77
        assert await cache.get_or_set("places", identifier, loader) == ["Museum"]
        assert called == 1
        assert await cache.invalidate_cache("places", identifier)
        assert key not in fake.values

    asyncio.run(run())


def test_cache_rejects_sensitive_payload_fields() -> None:
    async def run() -> None:
        cache = CacheService(FakeRedis())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Sensitive field"):
            await cache.set_cached(
                "destination", "id", {"api_key": "must not be cached"}
            )
        with pytest.raises(ValueError, match="Sensitive value"):
            await cache.set_cached(
                "search", "id", {"detail": "Bearer sensitive-token"}
            )

    asyncio.run(run())


def test_task_status_creation_and_ttl() -> None:
    async def run() -> None:
        fake = FakeRedis()
        service = TaskStatusService(fake, settings=Settings(_env_file=None))  # type: ignore[arg-type]
        task_id, trip_id = uuid4(), uuid4()
        status = await service.create_task_status(
            task_id, trip_id, ttl_seconds=600, message="Planning started"
        )
        assert status is not None
        assert status.status == "pending"
        assert status.progress_percentage == 0
        assert fake.ttls[service.key_for(task_id)] == 600

    asyncio.run(run())


def test_task_status_update_is_atomic_and_preserves_creation_time() -> None:
    class AtomicFakeRedis(FakeRedis):
        async def eval(
            self, script: str, keys: list[str], arguments: list[str]
        ) -> object:
            assert "redis.call('GET'" in script and "redis.call('SET'" in script
            existing = self.values.get(keys[0])
            if existing is CACHE_MISS:
                return False
            updated = dict(existing)  # type: ignore[arg-type]
            updated.update(json.loads(arguments[0]))
            updated["updated_at"] = arguments[1]
            self.values[keys[0]] = updated
            self.ttls[keys[0]] = int(arguments[2])
            return updated

    async def run() -> None:
        fake = AtomicFakeRedis()
        moments = iter(
            [
                datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC),
                datetime(2026, 10, 10, 12, 0, 1, tzinfo=UTC),
            ]
        )
        service = TaskStatusService(
            fake,
            settings=Settings(_env_file=None),
            clock=lambda: next(moments),
        )  # type: ignore[arg-type]
        task_id, trip_id = uuid4(), uuid4()
        created = await service.create_task_status(task_id, trip_id)
        assert created is not None
        updated = await service.update_task_status(
            task_id,
            status="running",
            current_agent="route_planner",
            progress_percentage=40,
            message="Routes being prepared",
        )
        assert updated is not None
        assert updated.created_at == created.created_at
        assert updated.updated_at > created.updated_at
        assert updated.status == "running"
        assert updated.progress_percentage == 40
        assert await service.get_task_status(task_id) == updated
        assert await service.delete_task_status(task_id)
        assert await service.get_task_status(task_id) is None

    asyncio.run(run())


def test_task_status_rejects_invalid_status_progress_and_credentials() -> None:
    with pytest.raises(ValidationError):
        TaskStatus(
            task_id=uuid4(),
            trip_id=uuid4(),
            status="unknown",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    with pytest.raises(ValidationError):
        TaskStatus(
            task_id=uuid4(),
            trip_id=uuid4(),
            status="running",
            progress_percentage=101,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def run() -> None:
        service = TaskStatusService(FakeRedis())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Unsupported task status"):
            await service.update_task_status(uuid4(), status="unknown")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="progress_percentage"):
            await service.update_task_status(uuid4(), progress_percentage=-1)
        with pytest.raises(ValidationError):
            await service.update_task_status(
                uuid4(), message="Bearer sensitive-token"
            )

    asyncio.run(run())


def test_task_status_supports_terminal_review_statuses() -> None:
    async def run() -> None:
        class AtomicFakeRedis(FakeRedis):
            async def eval(
                self, _script: str, keys: list[str], arguments: list[str]
            ) -> object:
                value = self.values[keys[0]]
                updated = dict(value)  # type: ignore[arg-type]
                updated.update(json.loads(arguments[0]))
                updated["updated_at"] = arguments[1]
                self.values[keys[0]] = updated
                return updated

        atomic = AtomicFakeRedis()
        service = TaskStatusService(atomic)  # type: ignore[arg-type]
        task_id, trip_id = uuid4(), uuid4()
        await service.create_task_status(task_id, trip_id)
        for terminal_status in ("revision_required", "insufficient_data"):
            result = await service.update_task_status(
                task_id,
                status=terminal_status,
            )
            assert result is not None
            assert result.status == terminal_status

    asyncio.run(run())


def test_task_status_expiration_and_missing_key() -> None:
    async def run() -> None:
        fake = FakeRedis()
        settings = Settings(_env_file=None, REDIS_TASK_STATUS_TTL_SECONDS=321)
        service = TaskStatusService(fake, settings=settings)  # type: ignore[arg-type]
        task_id = uuid4()
        assert await service.get_task_status(task_id) is None
        result = await service.create_task_status(task_id, uuid4())
        assert result is not None
        assert fake.ttls[service.key_for(task_id)] == 321

    asyncio.run(run())


def test_task_status_handles_missing_redis_configuration() -> None:
    async def run() -> None:
        service = TaskStatusService(UpstashRedisClient(Settings(_env_file=None)))
        assert await service.create_task_status(uuid4(), uuid4()) is None
        assert await service.get_task_status(uuid4()) is None
        assert not await service.delete_task_status(uuid4())

    asyncio.run(run())
