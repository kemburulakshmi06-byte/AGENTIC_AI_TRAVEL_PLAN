from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .agent_schemas import TravelRequest
from .optimization import OptimizationReview

ApiTripStatus = Literal[
    "planned",
    "queued",
    "running",
    "completed",
    "failed",
    "revision_required",
    "insufficient_data",
]


class TripPlanRequest(TravelRequest):
    origin: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    start_date: date
    end_date: date
    travelers_count: int = Field(ge=1)
    total_budget: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_trip_window(self) -> TripPlanRequest:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date.")
        return self


class TripRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_economy_accommodation: bool = False


class TripRevisionRecord(BaseModel):
    attempt: int
    original_total: Decimal
    revised_total: Decimal | None = None
    savings: Decimal | None = None
    remaining_over_budget: Decimal | None = None
    currency: str
    outcome: str
    changes: list[str] = Field(default_factory=list)
    explanation: str
    outcome_message: str | None = None
    original_budget: Decimal | None = None
    cumulative_savings: Decimal | None = None
    alternatives_exhausted: bool = False


class TripRevisionSummary(BaseModel):
    attempts: int
    max_attempts: int
    can_retry: bool
    requires_economy_permission: bool = False
    economy_accommodation_available: bool | None = None
    history: list[TripRevisionRecord] = Field(default_factory=list)


class TripCreatedResponse(BaseModel):
    trip_id: UUID
    task_id: UUID
    status: ApiTripStatus = "queued"
    message: str


class TripStatusResponse(BaseModel):
    trip_id: UUID
    task_id: UUID
    status: ApiTripStatus
    current_stage: str | None = None
    progress_percentage: int = Field(ge=0, le=100)
    message: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class TripSummaryResponse(BaseModel):
    id: UUID
    title: str
    status: ApiTripStatus
    origin: str
    destination: str
    start_date: date
    end_date: date
    travelers_count: int
    total_budget: Decimal
    currency: str
    total_estimated_cost: Decimal | None = None
    created_at: datetime
    updated_at: datetime


class TripListResponse(BaseModel):
    items: list[TripSummaryResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class PersistedActivityResponse(BaseModel):
    title: str
    description: str | None = None
    location_name: str | None = None
    start_time: time | None = None
    end_time: time | None = None
    estimated_cost: Decimal | None = None
    category: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersistedDayResponse(BaseModel):
    day_number: int
    itinerary_date: date
    summary: str | None = None
    estimated_daily_cost: Decimal | None = None
    activities: list[PersistedActivityResponse] = Field(default_factory=list)


class TripDetailsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    status: ApiTripStatus
    request: dict[str, Any]
    total_estimated_cost: Decimal | None
    currency: str
    created_at: datetime
    updated_at: datetime
    itinerary: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    optimization_review: OptimizationReview | None = None
    budget_revision_summary: TripRevisionSummary | None = None
    accommodation_transport: dict[str, Any] | None = None
    normalized_requirements: dict[str, Any] | None = None
    destination_research: dict[str, Any] | None = None
    days: list[PersistedDayResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
