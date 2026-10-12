from __future__ import annotations

from datetime import date as Date, time
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .agent_schemas import AgentSchema
from .budget import BudgetStatus
from .destination_research import SourceReference, IntercityTransfer

ItineraryActivityType = Literal[
    "attraction",
    "meal_break",
    "rest",
    "arrival_departure",
]
ValidationSeverity = Literal["info", "warning", "error"]


class ItineraryActivity(AgentSchema):
    intercity_transfer: IntercityTransfer | None = None
    activity_type: ItineraryActivityType
    title: str
    description: str
    start_time: time
    end_time: time
    duration_minutes: int = Field(gt=0, le=720)
    place_id: str | None = None
    location: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    coordinates_verified: bool = False
    travel_time_from_previous_minutes: Decimal | None = Field(
        default=None, ge=0, max_digits=10, decimal_places=2
    )
    travel_time_verified: bool = False
    travel_time_provider: Literal["openrouteservice"] | None = None
    estimated_cost: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    cost_currency: str | None = None
    cost_classification: Literal["estimated", "verified", "unknown"] = "unknown"
    source_references: list[SourceReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_times_and_evidence(self) -> ItineraryActivity:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time.")
        duration = int(
            (
                self.end_time.hour * 60
                + self.end_time.minute
                - self.start_time.hour * 60
                - self.start_time.minute
            )
        )
        if duration != self.duration_minutes:
            raise ValueError("duration_minutes must match the activity time range.")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Latitude and longitude must be supplied together.")
        if self.coordinates_verified and (
            self.latitude is None or self.longitude is None
        ):
            raise ValueError("Verified coordinates are required when marked verified.")
        if self.travel_time_verified and self.travel_time_from_previous_minutes is None:
            raise ValueError("Verified travel time requires a duration.")
        if self.travel_time_verified and self.travel_time_provider is None:
            raise ValueError("Verified travel time requires its provider.")
        if not self.travel_time_verified and self.travel_time_provider is not None:
            raise ValueError("A travel-time provider requires verified travel time.")
        if self.estimated_cost is None and self.cost_classification != "unknown":
            raise ValueError("Unpriced activity cost must be classified as unknown.")
        return self


class DailyCostEstimate(AgentSchema):
    amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    currency: str | None = None
    classification: Literal["estimated", "unknown"] = "unknown"
    allocation_basis: str | None = None
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_classification(self) -> DailyCostEstimate:
        if self.amount is None and self.classification != "unknown":
            raise ValueError("Unknown daily cost must use unknown classification.")
        if self.amount is not None and self.classification != "estimated":
            raise ValueError("Priced daily cost must use estimated classification.")
        return self


class DailyItinerary(AgentSchema):
    date: Date
    day_number: int = Field(ge=1)
    theme: str
    activities: list[ItineraryActivity] = Field(default_factory=list)
    daily_cost: DailyCostEstimate
    weather_condition: str | None = None
    weather_warning: str | None = None
    requires_route_verification: bool = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_non_overlapping(self) -> DailyItinerary:
        ordered = sorted(self.activities, key=lambda activity: activity.start_time)
        for previous, current in zip(ordered, ordered[1:]):
            if current.start_time < previous.end_time:
                raise ValueError("Daily itinerary activities cannot overlap.")
        return self


class ItineraryValidationIssue(AgentSchema):
    code: str
    message: str
    severity: ValidationSeverity = "warning"
    date: Date | None = None
    place_id: str | None = None


class TripItinerary(AgentSchema):
    destination_name: str | None = None
    start_date: Date | None = None
    end_date: Date | None = None
    currency: str | None = None
    days: list[DailyItinerary] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(default_factory=list)
    estimated_total_cost: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    budget_status: BudgetStatus = "INCOMPLETE_ESTIMATE"
    warnings: list[str] = Field(default_factory=list)
    validation_issues: list[ItineraryValidationIssue] = Field(default_factory=list)
    requires_revision: bool = False
    allow_repeat_attractions: bool = False
