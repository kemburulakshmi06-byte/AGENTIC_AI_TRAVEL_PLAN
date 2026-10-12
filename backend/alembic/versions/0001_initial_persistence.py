"""Create initial travel planning persistence tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "travel_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("traveler_name", sa.String(length=200), nullable=True),
        sa.Column("origin", sa.String(length=200), nullable=False),
        sa.Column("destination", sa.String(length=200), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("travelers_count", sa.Integer(), nullable=False),
        sa.Column("total_budget", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "currency", sa.String(length=3), server_default=sa.text("'INR'"), nullable=False
        ),
        sa.Column("travel_style", sa.String(length=100), nullable=True),
        sa.Column(
            "interests",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "constraints",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("travelers_count > 0", name="ck_travelers_count_positive"),
        sa.CheckConstraint("total_budget > 0", name="ck_total_budget_positive"),
        sa.CheckConstraint("end_date >= start_date", name="ck_travel_dates_ordered"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "trips",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("preference_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=250), nullable=False),
        sa.Column(
            "status", sa.String(length=50), server_default=sa.text("'planned'"), nullable=False
        ),
        sa.Column("total_estimated_cost", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "currency", sa.String(length=3), server_default=sa.text("'INR'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "total_estimated_cost IS NULL OR total_estimated_cost >= 0",
            name="ck_trip_estimated_cost_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["preference_id"],
            ["travel_preferences.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trips_preference_id", "trips", ["preference_id"])
    op.create_table(
        "itinerary_days",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day_number", sa.Integer(), nullable=False),
        sa.Column("itinerary_date", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "estimated_daily_cost",
            sa.Numeric(12, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("day_number > 0", name="ck_itinerary_day_number_positive"),
        sa.CheckConstraint(
            "estimated_daily_cost >= 0",
            name="ck_itinerary_daily_cost_nonnegative",
        ),
        sa.ForeignKeyConstraint(["trip_id"], ["trips.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trip_id", "day_number", name="uq_itinerary_day_trip_number"),
    )
    op.create_index("ix_itinerary_days_trip_id", "itinerary_days", ["trip_id"])
    op.create_table(
        "itinerary_activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("itinerary_day_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=250), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location_name", sa.String(length=250), nullable=True),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column(
            "estimated_cost",
            sa.Numeric(12, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "estimated_cost >= 0", name="ck_activity_cost_nonnegative"
        ),
        sa.CheckConstraint(
            "start_time IS NULL OR end_time IS NULL OR end_time > start_time",
            name="ck_activity_times_ordered",
        ),
        sa.ForeignKeyConstraint(
            ["itinerary_day_id"], ["itinerary_days.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_itinerary_activities_itinerary_day_id",
        "itinerary_activities",
        ["itinerary_day_id"],
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column(
            "input_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("output_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["trip_id"], ["trips.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_trip_id", "agent_runs", ["trip_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_trip_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index(
        "ix_itinerary_activities_itinerary_day_id", table_name="itinerary_activities"
    )
    op.drop_table("itinerary_activities")
    op.drop_index("ix_itinerary_days_trip_id", table_name="itinerary_days")
    op.drop_table("itinerary_days")
    op.drop_index("ix_trips_preference_id", table_name="trips")
    op.drop_table("trips")
    op.drop_table("travel_preferences")
