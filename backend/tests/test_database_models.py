import importlib.util
import re
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.dialects import postgresql

from app.database.base import Base
from app.database.models import (
    AgentRun,
    ItineraryActivity,
    ItineraryDay,
    TravelPreference,
    Trip,
)
from app.schemas.travel import (
    ItineraryActivityCreate,
    ItineraryActivityRead,
    ItineraryDayCreate,
    TravelPreferenceCreate,
)


def test_metadata_contains_all_phase_three_tables() -> None:
    assert set(Base.metadata.tables) == {
        "travel_preferences",
        "trips",
        "itinerary_days",
        "itinerary_activities",
        "agent_runs",
    }


def test_initial_migration_matches_all_model_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    revision_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0001_initial_persistence.py"
    )
    spec = importlib.util.spec_from_file_location(
        "initial_persistence_revision", revision_path
    )
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    migration_metadata = sa.MetaData()

    def capture_table(name: str, *elements: object, **kwargs: object) -> sa.Table:
        return sa.Table(name, migration_metadata, *elements, **kwargs)

    def capture_index(
        name: str,
        table_name: str,
        columns: list[str],
        unique: bool = False,
        **_kwargs: object,
    ) -> sa.Index:
        table = migration_metadata.tables[table_name]
        return sa.Index(name, *(table.c[column] for column in columns), unique=unique)

    monkeypatch.setattr(revision.op, "create_table", capture_table)
    monkeypatch.setattr(revision.op, "create_index", capture_index)
    revision.upgrade()

    assert set(migration_metadata.tables) == set(Base.metadata.tables)
    assert len(migration_metadata.tables) == 5

    dialect = postgresql.dialect()

    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    def table_signature(table: sa.Table) -> tuple[object, ...]:
        columns = tuple(
            (
                column.name,
                column.type.compile(dialect=dialect),
                column.nullable,
                column.primary_key,
                (
                    normalize(str(column.server_default.arg.compile(dialect=dialect)))
                    if column.server_default is not None
                    else None
                ),
            )
            for column in table.columns
        )
        primary_key = tuple(column.name for column in table.primary_key.columns)
        foreign_keys = tuple(
            sorted(
                (
                    next(iter(constraint.columns)).name,
                    next(iter(constraint.elements)).target_fullname,
                    next(iter(constraint.elements)).ondelete,
                )
                for constraint in table.foreign_key_constraints
            )
        )
        checks = tuple(
            sorted(
                (
                    constraint.name,
                    normalize(str(constraint.sqltext.compile(dialect=dialect))),
                )
                for constraint in table.constraints
                if isinstance(constraint, sa.CheckConstraint)
            )
        )
        unique_constraints = tuple(
            sorted(
                (constraint.name, tuple(column.name for column in constraint.columns))
                for constraint in table.constraints
                if isinstance(constraint, sa.UniqueConstraint)
            )
        )
        indexes = tuple(
            sorted(
                (
                    index.name,
                    tuple(column.name for column in index.columns),
                    index.unique,
                )
                for index in table.indexes
            )
        )
        return columns, primary_key, foreign_keys, checks, unique_constraints, indexes

    for table_name in Base.metadata.tables:
        assert table_signature(migration_metadata.tables[table_name]) == table_signature(
            Base.metadata.tables[table_name]
        ), table_name


def test_postgresql_uuid_jsonb_and_safe_metadata_attribute() -> None:
    assert isinstance(TravelPreference.__table__.c.id.type, UUID)
    assert isinstance(TravelPreference.__table__.c.interests.type, JSONB)
    assert isinstance(ItineraryActivity.__table__.c.metadata.type, JSONB)
    assert "metadata_json" in ItineraryActivity.__mapper__.attrs
    assert ItineraryActivity.__mapper__.attrs["metadata_json"].columns[0].name == "metadata"


def test_model_relationships_match_foreign_key_graph() -> None:
    assert {relationship.key for relationship in TravelPreference.__mapper__.relationships} == {
        "trips"
    }
    assert {relationship.key for relationship in Trip.__mapper__.relationships} == {
        "preference",
        "itinerary_days",
        "agent_runs",
    }
    assert {relationship.key for relationship in ItineraryDay.__mapper__.relationships} == {
        "trip",
        "activities",
    }
    assert {
        relationship.key for relationship in ItineraryActivity.__mapper__.relationships
    } == {"itinerary_day"}
    assert {relationship.key for relationship in AgentRun.__mapper__.relationships} == {
        "trip"
    }


def test_preference_schema_validates_dates_and_positive_values() -> None:
    data = {
        "origin": "Delhi",
        "destination": "Jaipur",
        "start_date": date(2026, 11, 1),
        "end_date": date(2026, 11, 3),
        "travelers_count": 2,
        "total_budget": Decimal("25000"),
    }
    assert TravelPreferenceCreate(**data).currency == "INR"

    with pytest.raises(ValidationError):
        TravelPreferenceCreate(**{**data, "end_date": date(2026, 10, 31)})
    with pytest.raises(ValidationError):
        TravelPreferenceCreate(**{**data, "travelers_count": 0})
    with pytest.raises(ValidationError):
        TravelPreferenceCreate(**{**data, "total_budget": Decimal("0")})


def test_itinerary_schemas_reject_negative_costs_and_invalid_time_ranges() -> None:
    with pytest.raises(ValidationError):
        ItineraryDayCreate(
            day_number=1,
            itinerary_date=date(2026, 11, 1),
            estimated_daily_cost=Decimal("-1"),
        )
    with pytest.raises(ValidationError):
        ItineraryActivityCreate(
            title="Visit museum",
            start_time=time(14, 0),
            end_time=time(13, 0),
        )
    with pytest.raises(ValidationError):
        ItineraryActivityCreate(title="Visit museum", estimated_cost=Decimal("-1"))


def test_activity_read_schema_maps_safe_orm_attribute_to_metadata() -> None:
    item = SimpleNamespace(
        id=uuid4(),
        itinerary_day_id=uuid4(),
        title="Museum",
        description=None,
        location_name=None,
        start_time=None,
        end_time=None,
        estimated_cost=Decimal("0"),
        category=None,
        metadata_json={"ticket_required": True},
        created_at="2026-10-10T00:00:00Z",
        updated_at="2026-10-10T00:00:00Z",
    )

    assert ItineraryActivityRead.model_validate(item).metadata == {
        "ticket_required": True
    }
