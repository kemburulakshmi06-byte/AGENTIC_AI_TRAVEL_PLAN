from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class ORMReadModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TravelPreferenceCreate(BaseModel):
    traveler_name: str | None = Field(default=None, max_length=200)
    origin: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    start_date: date
    end_date: date
    travelers_count: int = Field(gt=0)
    total_budget: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    travel_style: str | None = Field(default=None, max_length=100)
    interests: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dates(self) -> "TravelPreferenceCreate":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class TravelPreferenceRead(ORMReadModel):
    id: UUID
    traveler_name: str | None
    origin: str
    destination: str
    start_date: date
    end_date: date
    travelers_count: int
    total_budget: Decimal
    currency: str
    travel_style: str | None
    interests: list[str]
    constraints: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class TripCreate(BaseModel):
    preference_id: UUID
    title: str = Field(min_length=1, max_length=250)
    status: str = Field(default="planned", min_length=1, max_length=50)
    total_estimated_cost: Decimal | None = Field(
        default=None, ge=0, max_digits=12, decimal_places=2
    )
    currency: str = Field(default="INR", min_length=3, max_length=3)


class TripRead(ORMReadModel):
    id: UUID
    preference_id: UUID
    title: str
    status: str
    total_estimated_cost: Decimal | None
    currency: str
    created_at: datetime
    updated_at: datetime


class ItineraryDayCreate(BaseModel):
    day_number: int = Field(gt=0)
    itinerary_date: date
    summary: str | None = None
    estimated_daily_cost: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=12, decimal_places=2
    )


class ItineraryDayRead(ORMReadModel):
    id: UUID
    trip_id: UUID
    day_number: int
    itinerary_date: date
    summary: str | None
    estimated_daily_cost: Decimal
    created_at: datetime
    updated_at: datetime


class ItineraryActivityCreate(BaseModel):
    title: str = Field(min_length=1, max_length=250)
    description: str | None = None
    location_name: str | None = Field(default=None, max_length=250)
    start_time: time | None = None
    end_time: time | None = None
    estimated_cost: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=12, decimal_places=2
    )
    category: str | None = Field(default=None, max_length=100)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("metadata", "metadata_json"),
    )

    @model_validator(mode="after")
    def validate_times(self) -> "ItineraryActivityCreate":
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.end_time <= self.start_time
        ):
            raise ValueError("end_time must be after start_time")
        return self


class ItineraryActivityRead(ORMReadModel):
    id: UUID
    itinerary_day_id: UUID
    title: str
    description: str | None
    location_name: str | None
    start_time: time | None
    end_time: time | None
    estimated_cost: Decimal
    category: str | None
    metadata: dict[str, Any] = Field(
        validation_alias=AliasChoices("metadata_json", "metadata")
    )
    created_at: datetime
    updated_at: datetime


class AgentRunCreate(BaseModel):
    agent_name: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=1, max_length=50)
    input_data: dict[str, Any] = Field(default_factory=dict)
    output_data: dict[str, Any] | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_timezone_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_completion_time(self) -> "AgentRunCreate":
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("completed_at must not precede started_at")
        return self


class AgentRunRead(ORMReadModel):
    id: UUID
    trip_id: UUID
    agent_name: str
    status: str
    input_data: dict[str, Any]
    output_data: dict[str, Any] | None
    error_message: str | None
    started_at: datetime
    completed_at: datetime | None
