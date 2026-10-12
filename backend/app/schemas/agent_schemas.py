from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

AgentOutputT = TypeVar("AgentOutputT", bound=BaseModel)
PlanningTransportMode = Literal[
    "driving",
    "walking",
    "cycling",
    "local_transit",
    "flight",
    "train",
    "bus",
]


class AgentSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TripCostPreferences(AgentSchema):
    accommodation_nightly_rate_per_room: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    room_count: int | None = Field(default=None, ge=1, le=50)
    transport_mode: PlanningTransportMode | None = None
    intercity_round_trip_fare_per_traveler: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    local_transport_allowance_per_day: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    food_allowance_per_traveler_per_day: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    activities_allowance_per_traveler_per_trip: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    contingency_percentage: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=5, decimal_places=2
    )


class TravelRequest(AgentSchema):
    origin: str | None = Field(default=None, min_length=1, max_length=200)
    destination: str | None = Field(default=None, min_length=1, max_length=200)
    start_date: date | None = None
    end_date: date | None = None
    travelers_count: int | None = Field(default=None, ge=1)
    total_budget: Decimal | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=2
    )
    currency: str | None = None
    travel_style: str | None = Field(default=None, min_length=1, max_length=100)
    interests: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    cost_preferences: TripCostPreferences | None = None
    user_message: str | None = Field(default=None, max_length=4000)

    @field_validator("origin", "destination", "travel_style")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank.")
        return normalized

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be a three-letter code.")
        return normalized

    @model_validator(mode="after")
    def validate_date_order(self) -> TravelRequest:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must be on or after start_date.")
        return self


class NormalizedTravelRequirements(AgentSchema):
    origin: str | None = None
    destination: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    travelers_count: int | None = Field(default=None, ge=1)
    total_budget: Decimal | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=2
    )
    currency: str | None = None
    travel_style: str | None = None
    interests: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    cost_preferences: TripCostPreferences | None = None
    travel_duration_days: int | None = Field(default=None, ge=1)
    missing_required_fields: list[str] = Field(default_factory=list)
    conflicting_requirements: list[str] = Field(default_factory=list)


class AgentContext(AgentSchema):
    request: TravelRequest = Field(default_factory=TravelRequest)
    normalized_requirements: NormalizedTravelRequirements | None = None
    destination_research: dict[str, Any] | None = None
    accommodation_transport_plan: dict[str, Any] | None = None
    budget_breakdown: dict[str, Any] | None = None
    trip_itinerary: dict[str, Any] | None = None
    trace_id: str | None = Field(default=None, max_length=100)


class AgentResult(AgentSchema, Generic[AgentOutputT]):
    agent_name: str
    status: Literal["completed", "needs_input", "failed"]
    output: AgentOutputT
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class TravelRequestInterpretation(AgentSchema):
    """Facts extracted from free text; absent facts remain unset."""

    origin: str | None = Field(default=None, min_length=1, max_length=200)
    destination: str | None = Field(default=None, min_length=1, max_length=200)
    start_date: date | None = None
    end_date: date | None = None
    travelers_count: int | None = Field(default=None, ge=1)
    total_budget: Decimal | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=2
    )
    currency: str | None = None
    travel_style: str | None = Field(default=None, min_length=1, max_length=100)
    interests: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator("origin", "destination", "travel_style")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("value must not be blank.")
        return normalized

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be a three-letter code.")
        return normalized


    @model_validator(mode="after")
    def validate_date_order(self) -> TravelRequestInterpretation:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must be on or after start_date.")
        return self
