from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel

from ..schemas.agent_schemas import AgentContext, AgentResult

AgentOutputT = TypeVar("AgentOutputT", bound=BaseModel)


class BaseAgent(ABC, Generic[AgentOutputT]):
    """Common async contract for travel-planning agents."""

    agent_name: str

    @abstractmethod
    async def run(self, context: AgentContext) -> AgentResult[AgentOutputT]:
        """Process a typed context and return typed structured output."""
