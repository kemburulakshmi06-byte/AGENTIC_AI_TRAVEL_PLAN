from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, Literal, TypedDict

from pydantic import Field

from ..schemas.accommodation_transport import AccommodationTransportPlan
from ..schemas.agent_schemas import (
    AgentSchema,
    NormalizedTravelRequirements,
    TravelRequest,
)
from ..schemas.budget import BudgetBreakdown
from ..schemas.destination_research import DestinationResearch, SourceReference
from ..schemas.itinerary import TripItinerary
from ..schemas.optimization import OptimizationReview

WorkflowStatus = Literal[
    "RUNNING",
    "COMPLETED",
    "INVALID_REQUEST",
    "INSUFFICIENT_DATA",
    "REVISION_REQUIRED",
    "FAILED",
]
AgentExecutionStatus = Literal[
    "completed",
    "needs_input",
    "failed",
    "timed_out",
]


class AgentExecutionRecord(AgentSchema):
    agent_name: str
    status: AgentExecutionStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    error: str | None = None
    output_data: dict | None = None


class RevisionDirective(AgentSchema):
    excluded_attraction_ids: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(min_length=1, max_length=300)


class TravelWorkflowState(TypedDict, total=False):
    request: TravelRequest
    normalized_requirements: NormalizedTravelRequirements
    destination_research: DestinationResearch
    accommodation_transport_plan: AccommodationTransportPlan
    budget_breakdown: BudgetBreakdown
    trip_itinerary: TripItinerary
    optimization_review: OptimizationReview
    current_stage: str
    execution_status: WorkflowStatus
    warnings: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]
    revision_count: int
    max_revisions: int
    agent_timeout_seconds: float
    agent_history: Annotated[list[AgentExecutionRecord], operator.add]
    source_references: Annotated[list[SourceReference], operator.add]
    excluded_attraction_ids: list[str]
    revision_directive: RevisionDirective | None
    budget_inputs_changed: bool
    budget_revision: dict | None
    budget_revision_fingerprints: list[str]
