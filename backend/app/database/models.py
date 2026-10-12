from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class TravelPreference(TimestampMixin, Base):
    __tablename__ = "travel_preferences"
    __table_args__ = (
        CheckConstraint("travelers_count > 0", name="ck_travelers_count_positive"),
        CheckConstraint("total_budget > 0", name="ck_total_budget_positive"),
        CheckConstraint("end_date >= start_date", name="ck_travel_dates_ordered"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    traveler_name: Mapped[str | None] = mapped_column(String(200))
    origin: Mapped[str] = mapped_column(String(200), nullable=False)
    destination: Mapped[str] = mapped_column(String(200), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    travelers_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_budget: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="INR", server_default=text("'INR'")
    )
    travel_style: Mapped[str | None] = mapped_column(String(100))
    interests: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    constraints: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    trips: Mapped[list[Trip]] = relationship(back_populates="preference")


class Trip(TimestampMixin, Base):
    __tablename__ = "trips"
    __table_args__ = (
        CheckConstraint(
            "total_estimated_cost IS NULL OR total_estimated_cost >= 0",
            name="ck_trip_estimated_cost_nonnegative",
        ),
        Index("ix_trips_preference_id", "preference_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    preference_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("travel_preferences.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(250), nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="planned", server_default=text("'planned'")
    )
    total_estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="INR", server_default=text("'INR'")
    )

    preference: Mapped[TravelPreference] = relationship(back_populates="trips")
    itinerary_days: Mapped[list[ItineraryDay]] = relationship(back_populates="trip")
    agent_runs: Mapped[list[AgentRun]] = relationship(back_populates="trip")


class ItineraryDay(TimestampMixin, Base):
    __tablename__ = "itinerary_days"
    __table_args__ = (
        CheckConstraint("day_number > 0", name="ck_itinerary_day_number_positive"),
        CheckConstraint(
            "estimated_daily_cost >= 0",
            name="ck_itinerary_daily_cost_nonnegative",
        ),
        UniqueConstraint("trip_id", "day_number", name="uq_itinerary_day_trip_number"),
        Index("ix_itinerary_days_trip_id", "trip_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
    )
    day_number: Mapped[int] = mapped_column(Integer, nullable=False)
    itinerary_date: Mapped[date] = mapped_column(Date, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    estimated_daily_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0"), server_default=text("0")
    )

    trip: Mapped[Trip] = relationship(back_populates="itinerary_days")
    activities: Mapped[list[ItineraryActivity]] = relationship(
        back_populates="itinerary_day"
    )


class ItineraryActivity(TimestampMixin, Base):
    __tablename__ = "itinerary_activities"
    __table_args__ = (
        CheckConstraint(
            "estimated_cost >= 0", name="ck_activity_cost_nonnegative"
        ),
        CheckConstraint(
            "start_time IS NULL OR end_time IS NULL OR end_time > start_time",
            name="ck_activity_times_ordered",
        ),
        Index("ix_itinerary_activities_itinerary_day_id", "itinerary_day_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    itinerary_day_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("itinerary_days.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(250), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    location_name: Mapped[str | None] = mapped_column(String(250))
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    estimated_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    category: Mapped[str | None] = mapped_column(String(100))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )

    itinerary_day: Mapped[ItineraryDay] = relationship(back_populates="activities")


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_trip_id", "trip_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    input_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    output_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    trip: Mapped[Trip] = relationship(back_populates="agent_runs")
