from __future__ import annotations

import re
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.config import Settings, get_settings
from .redis_client import RedisClientError, UpstashRedisClient

TaskStatusName = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "revision_required",
    "insufficient_data",
]

_ATOMIC_UPDATE_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then
  return false
end
local state = cjson.decode(raw)
local patch = cjson.decode(ARGV[1])
for key, value in pairs(patch) do
  state[key] = value
end
state.updated_at = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(state), 'EX', ARGV[3])
return cjson.encode(state)
"""
_SENSITIVE_TEXT = re.compile(
    r"(?i)(bearer\s+\S+|(?:api[_-]?key|token|password|secret|credential)\s*[:=]\s*\S+)"
)


class TaskStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    trip_id: UUID
    status: TaskStatusName
    current_agent: str | None = Field(default=None, max_length=100)
    progress_percentage: int = Field(default=0, ge=0, le=100)
    message: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime
    error_message: str | None = Field(default=None, max_length=1000)

    @field_validator("message", "error_message")
    @classmethod
    def reject_sensitive_text(cls, value: str | None) -> str | None:
        if value is not None and _SENSITIVE_TEXT.search(value):
            raise ValueError("Task status text must not contain credentials.")
        return value


class TaskStatusService:
    def __init__(
        self,
        redis: UpstashRedisClient | None = None,
        *,
        settings: Settings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._redis = redis or UpstashRedisClient(self._settings)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def aclose(self) -> None:
        await self._redis.aclose()

    @staticmethod
    def key_for(task_id: UUID | str) -> str:
        return f"travel-planner:task-status:{task_id}"

    async def create_task_status(
        self,
        task_id: UUID | str,
        trip_id: UUID | str,
        *,
        current_agent: str | None = None,
        progress_percentage: int = 0,
        message: str | None = None,
        ttl_seconds: int | None = None,
    ) -> TaskStatus | None:
        now = self._now()
        status = TaskStatus(
            task_id=task_id,
            trip_id=trip_id,
            status="pending",
            current_agent=current_agent,
            progress_percentage=progress_percentage,
            message=message,
            created_at=now,
            updated_at=now,
        )
        ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else self._settings.REDIS_TASK_STATUS_TTL_SECONDS
        )
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive.")
        try:
            saved = await self._redis.set(
                self.key_for(status.task_id), status.model_dump(mode="json"), ttl
            )
        except RedisClientError:
            return None
        if not saved:
            return None
        return status

    async def update_task_status(
        self,
        task_id: UUID | str,
        *,
        status: TaskStatusName | None = None,
        current_agent: str | None = None,
        progress_percentage: int | None = None,
        message: str | None = None,
        error_message: str | None = None,
        ttl_seconds: int | None = None,
    ) -> TaskStatus | None:
        patch: dict[str, Any] = {}
        if status is not None:
            if status not in {
                "pending",
                "running",
                "completed",
                "failed",
                "revision_required",
                "insufficient_data",
            }:
                raise ValueError("Unsupported task status.")
            patch["status"] = status
        if current_agent is not None:
            patch["current_agent"] = current_agent
        if progress_percentage is not None:
            if not 0 <= progress_percentage <= 100:
                raise ValueError("progress_percentage must be between 0 and 100.")
            patch["progress_percentage"] = progress_percentage
        if message is not None:
            patch["message"] = message
        if error_message is not None:
            patch["error_message"] = error_message
        # Validate user-supplied status text and field lengths before Redis access.
        now = self._now()
        TaskStatus.model_validate(
            {
                "task_id": task_id,
                "trip_id": UUID(int=0),
                "status": status or "pending",
                "current_agent": current_agent,
                "progress_percentage": (
                    progress_percentage if progress_percentage is not None else 0
                ),
                "message": message,
                "created_at": now,
                "updated_at": now,
                "error_message": error_message,
            }
        )
        ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else self._settings.REDIS_TASK_STATUS_TTL_SECONDS
        )
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive.")
        try:
            value = await self._redis.eval(
                _ATOMIC_UPDATE_SCRIPT,
                [self.key_for(task_id)],
                [
                    json.dumps(patch, separators=(",", ":")),
                    now.isoformat(timespec="microseconds"),
                    str(ttl),
                ],
            )
        except RedisClientError:
            return None
        if not isinstance(value, dict):
            return None
        return TaskStatus.model_validate(value)

    async def get_task_status(self, task_id: UUID | str) -> TaskStatus | None:
        try:
            value = await self._redis.get(self.key_for(task_id))
        except RedisClientError:
            return None
        if not isinstance(value, dict):
            return None
        return TaskStatus.model_validate(value)

    async def delete_task_status(self, task_id: UUID | str) -> bool:
        try:
            return await self._redis.delete(self.key_for(task_id))
        except RedisClientError:
            return False

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Task status clock must return a timezone-aware datetime.")
        return value.astimezone(UTC)
