import uuid
from collections.abc import Generator
from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.base import Base
from app.schemas.travel import TravelPreferenceCreate, TripCreate
from app.schemas.agent_schemas import TravelRequest
from app.schemas.destination_research import SourceReference
from app.schemas.itinerary import (
    DailyCostEstimate,
    DailyItinerary as PersistedDailyItinerary,
    ItineraryActivity as PersistedItineraryActivity,
    TripItinerary,
)
from app.services.task_status_service import TaskStatusService
from app.services.trip_repository import TripRepository
from app.services.trip_planning_service import TripPlanningService
from app.workflows.state import AgentExecutionRecord


def _dedicated_test_url() -> str:
    settings = Settings()
    test_setting = settings.TEST_DATABASE_URL
    if test_setting is None:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database.")

    url = make_url(test_setting.get_secret_value())
    if "test" not in (url.database or "").lower():
        pytest.fail("TEST_DATABASE_URL database name must contain 'test'.")

    configured_url = settings.DATABASE_URL
    if configured_url is not None:
        production_url = make_url(configured_url.get_secret_value())
        if (
            production_url.host,
            production_url.port,
            production_url.database,
        ) == (url.host, url.port, url.database):
            pytest.fail("TEST_DATABASE_URL must differ from DATABASE_URL.")

    if url.drivername in {"postgres", "postgresql", "postgresql+psycopg2"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        pytest.fail("TEST_DATABASE_URL must use PostgreSQL with Psycopg 3.")
    return url.render_as_string(hide_password=False)


@pytest.fixture
def isolated_test_schema() -> Generator[tuple[Session, str], None, None]:
    url = _dedicated_test_url()
    engine = create_engine(url, pool_pre_ping=True)
    schema_name = f"phase3_test_{uuid.uuid4().hex}"

    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            connection.commit()

            schema_connection = connection.execution_options(
                schema_translate_map={None: schema_name}
            )
            Base.metadata.create_all(schema_connection)
            schema_connection.commit()

            try:
                with Session(bind=schema_connection, expire_on_commit=False) as session:
                    yield session, schema_name
            finally:
                Base.metadata.drop_all(schema_connection)
                schema_connection.commit()
                connection.execute(text(f'DROP SCHEMA "{schema_name}"'))
                connection.commit()
    finally:
        engine.dispose()


def test_repository_round_trips_records_in_dedicated_database(
    isolated_test_schema: tuple[Session, str],
) -> None:
    session, _schema_name = isolated_test_schema
    repository = TripRepository(session)

    preference = repository.create_preference(
        TravelPreferenceCreate(
            origin="Delhi",
            destination="Jaipur",
            start_date="2026-11-01",
            end_date="2026-11-03",
            travelers_count=2,
            total_budget=Decimal("25000"),
        )
    )
    trip = repository.create_trip(
        TripCreate(preference_id=preference.id, title="Weekend in Jaipur")
    )

    assert repository.get_preference(preference.id).id == preference.id
    assert repository.get_trip(trip.id).id == trip.id


def test_trip_planning_persistence_keeps_unknown_costs_and_delete_is_final(
    isolated_test_schema: tuple[Session, str],
) -> None:
    session, _schema_name = isolated_test_schema
    service = TripPlanningService(
        session_factory=lambda: Session(
            bind=session.get_bind(),
            expire_on_commit=False,
        ),
        task_status=TaskStatusService(),
    )
    request = TravelRequest(
        origin="Delhi",
        destination="Jaipur",
        start_date="2026-11-01",
        end_date="2026-11-01",
        travelers_count=2,
        total_budget=Decimal("25000"),
        currency="INR",
    )
    trip_id = uuid.uuid4()
    service._create_trip(trip_id, request)
    reference = SourceReference(
        title="Test source",
        url="https://example.com/test",
        topic="travel_tips",
    )
    trip_itinerary = TripItinerary(
        destination_name="Jaipur",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        currency="INR",
        days=[
            PersistedDailyItinerary(
                date=date(2026, 11, 1),
                day_number=1,
                theme="Culture",
                activities=[
                    PersistedItineraryActivity(
                        activity_type="attraction",
                        title="Museum",
                        description="Provider researched attraction.",
                        start_time=time(10),
                        end_time=time(11),
                        duration_minutes=60,
                        place_id="geoapify-1",
                        latitude=26.9,
                        longitude=75.8,
                        coordinates_verified=True,
                        source_references=[reference],
                    )
                ],
                daily_cost=DailyCostEstimate(),
            )
        ],
        source_references=[reference],
    )
    started = datetime.now(timezone.utc)
    result = {
        "request": request,
        "execution_status": "INSUFFICIENT_DATA",
        "current_stage": "optimization_review",
        "revision_count": 0,
        "warnings": [],
        "errors": [],
        "source_references": [reference],
        "agent_history": [
            AgentExecutionRecord(
                agent_name="itinerary",
                status="completed",
                started_at=started,
                finished_at=started,
                duration_ms=0,
            )
        ],
        "trip_itinerary": trip_itinerary,
    }

    assert service._persist_result(trip_id, result)
    saved = service._get_trip(trip_id)
    assert saved is not None
    assert saved.status == "insufficient_data"
    assert saved.days[0].estimated_daily_cost is None
    assert saved.days[0].activities[0].estimated_cost is None
    assert saved.itinerary["days"][0]["daily_cost"]["amount"] is None

    assert service._delete_trip(trip_id)
    assert service._persist_result(trip_id, result) is False


def test_postgresql_concurrent_workers_claim_each_job_once(isolated_test_schema):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy.orm import sessionmaker
    from app.services.trip_job_service import TripJobService
    session, schema = isolated_test_schema
    # Independent connections, isolated schema, actual PostgreSQL row locks.
    engine = create_engine(session.get_bind().engine.url, pool_pre_ping=True,
        execution_options={'schema_translate_map': {None: schema}})
    factory = sessionmaker(engine, expire_on_commit=False)
    config = Settings(_env_file=None, TRIP_WORKER_ENABLED=False)
    service = TripPlanningService(factory, TaskStatusService(settings=config), settings=config)
    ids = [uuid.uuid4() for _ in range(4)]
    request = TravelRequest(origin='Origin', destination='Destination', start_date='2026-11-01',
        end_date='2026-11-02', travelers_count=2, total_budget='20000',currency='INR')
    try:
        for ident in ids:
            service._create_trip(ident, request)
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: TripJobService(factory,config).claim(), range(4)))
        assert {claim.trip_id for claim in claims if claim} == set(ids)
        assert len(claims) == len({claim.token for claim in claims}) == 4
        assert TripJobService(factory,config).claim() is None
    finally:
        for ident in ids:
            service._delete_trip(ident)
        engine.dispose()


def test_postgresql_expired_lease_recovery_fences_old_worker(isolated_test_schema):
    from datetime import timedelta
    from sqlalchemy.orm import sessionmaker
    from app.services.trip_job_service import TripJobService, job_for
    session, schema = isolated_test_schema
    engine = create_engine(session.get_bind().engine.url, pool_pre_ping=True,
        execution_options={'schema_translate_map': {None:schema}})
    factory = sessionmaker(engine,expire_on_commit=False)
    config = Settings(_env_file=None, TRIP_WORKER_ENABLED=False)
    service = TripPlanningService(factory, TaskStatusService(settings=config), settings=config)
    ident = uuid.uuid4()
    request = TravelRequest(origin='Origin',destination='Destination',start_date='2026-11-01',
        end_date='2026-11-02',travelers_count=2,total_budget='20000',currency='INR')
    try:
        service._create_trip(ident,request)
        queue = TripJobService(factory,config)
        first = queue.claim()
        with factory() as db, db.begin():
            job = job_for(db,ident)
            job.output_data={**job.output_data,'lease_expires_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}
        second = TripJobService(factory,config).claim()
        assert second.token != first.token
        assert not service._persist_failure(ident,'failed','old worker',first.token)
        assert not service._persist_result(ident,{'execution_status':'FAILED'},first.token)
        assert queue.renew(second)
    finally:
        service._delete_trip(ident)
        engine.dispose()
