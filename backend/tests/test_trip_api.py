from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from app.core.config import Settings
from app.main import create_app
from app.schemas.agent_schemas import TravelRequest
from app.schemas.trip_api import TripDetailsResponse, TripSummaryResponse


class FakeTripService:
    def __init__(self) -> None:
        self.trip_id = uuid4()
        self.created_request: Any = None
        self.started: list[tuple[UUID, Any]] = []
        self.deleted: list[UUID] = []
        self.closed = False
        now = datetime.now(timezone.utc)
        self.details = TripDetailsResponse(
            id=self.trip_id,
            title="Example City trip",
            status="completed",
            request={
                "origin": "Delhi",
                "destination": "Jaipur",
                "start_date": date(2026, 11, 1),
                "end_date": date(2026, 11, 2),
                "travelers_count": 2,
                "total_budget": Decimal("25000"),
                "currency": "INR",
                "travel_style": "culture",
                "interests": ["history"],
                "constraints": {},
            },
            total_estimated_cost=Decimal("22000"),
            currency="INR",
            created_at=now,
            updated_at=now,
            itinerary={"days": []},
            budget={"status": "WITHIN_BUDGET"},
            optimization_review={
                "approved": True,
                "review_status": "APPROVED",
                "suggestions": [
                    {
                        "category": "routing",
                        "recommendation": (
                            "Group nearby activities to reduce unnecessary travel."
                        ),
                    }
                ],
            },
        )
        self.summary = TripSummaryResponse(
            id=self.trip_id,
            title="Example City trip",
            status="completed",
            origin="Delhi",
            destination="Jaipur",
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 2),
            travelers_count=2,
            total_budget=Decimal("25000"),
            currency="INR",
            total_estimated_cost=Decimal("22000"),
            created_at=now,
            updated_at=now,
        )

    async def create_trip(self, request: Any) -> UUID:
        self.created_request = request
        return self.trip_id

    async def run_trip(self, trip_id: UUID, request: Any) -> None:
        self.started.append((trip_id, request))

    async def get_trip_status(self, trip_id: UUID) -> dict[str, Any]:
        if trip_id != self.trip_id:
            from app.services.trip_planning_service import TripNotFoundError

            raise TripNotFoundError
        now = self.details.updated_at
        return {
            "trip_id": trip_id,
            "task_id": trip_id,
            "status": "running",
            "current_stage": "itinerary",
            "progress_percentage": 70,
            "message": "Planning is in progress.",
            "error_message": None,
            "created_at": self.details.created_at,
            "updated_at": now,
        }

    async def get_trip(self, trip_id: UUID) -> TripDetailsResponse:
        if trip_id != self.trip_id:
            from app.services.trip_planning_service import TripNotFoundError

            raise TripNotFoundError
        return self.details

    async def list_trips(self, limit: int, offset: int):
        return ([self.summary][offset : offset + limit], 1)

    async def delete_trip(self, trip_id: UUID) -> bool:
        if trip_id != self.trip_id:
            return False
        self.deleted.append(trip_id)
        return True

    async def aclose(self) -> None:
        self.closed = True


def create_test_client(
    service: FakeTripService,
    *,
    admin_enabled: bool = False,
    history_enabled: bool = False,
) -> TestClient:
    application = create_app(
        Settings(
            _env_file=None,
            TRIP_ADMIN_API_ENABLED=admin_enabled,
            TRIP_HISTORY_API_ENABLED=history_enabled,
            TRIP_EXECUTION_MODE="background",
        )
    )
    application.state.trip_planning_service_factory = lambda: service
    return TestClient(application)


def valid_payload() -> dict[str, Any]:
    return {
        "origin": "Delhi",
        "destination": "Jaipur",
        "start_date": "2026-11-01",
        "end_date": "2026-11-02",
        "travelers_count": 2,
        "total_budget": "25000.00",
        "currency": "INR",
        "travel_style": "culture",
        "interests": ["history"],
        "constraints": {},
    }


def test_post_plan_returns_202_and_schedules_background_work() -> None:
    service = FakeTripService()
    with create_test_client(service) as client:
        response = client.post("/api/v1/trips/plan", json=valid_payload())

    assert response.status_code == 202
    body = response.json()
    assert body["trip_id"] == str(service.trip_id)
    assert body["task_id"] == str(service.trip_id)
    assert body["status"] == "queued"
    assert service.created_request.destination == "Jaipur"
    assert len(service.started) == 1
    assert service.started[0][0] == service.trip_id
    assert service.closed


def test_invalid_trip_request_returns_clear_422() -> None:
    with create_test_client(FakeTripService()) as client:
        response = client.post(
            "/api/v1/trips/plan",
            json={**valid_payload(), "travelers_count": 0},
        )
    assert response.status_code == 422
    assert "detail" in response.json()


def test_invalid_date_range_returns_422() -> None:
    with create_test_client(FakeTripService()) as client:
        response = client.post(
            "/api/v1/trips/plan",
            json={
                **valid_payload(),
                "start_date": "2026-11-04",
                "end_date": "2026-11-01",
            },
        )
    assert response.status_code == 422


def test_status_polling_returns_current_execution_fields() -> None:
    service = FakeTripService()
    with create_test_client(service) as client:
        response = client.get(f"/api/v1/trips/{service.trip_id}/status")

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["current_stage"] == "itinerary"
    assert response.json()["progress_percentage"] == 70


def test_completed_trip_details_include_persisted_structures() -> None:
    service = FakeTripService()
    with create_test_client(service) as client:
        response = client.get(f"/api/v1/trips/{service.trip_id}")

    assert response.status_code == 200
    assert response.json()["budget"]["status"] == "WITHIN_BUDGET"
    assert response.json()["optimization_review"]["approved"] is True
    assert response.json()["optimization_review"]["suggestions"][0][
        "recommendation"
    ] == "Group nearby activities to reduce unnecessary travel."
    assert response.json()["request"]["destination"] == "Jaipur"


def test_unknown_trip_returns_404_for_details_and_status() -> None:
    unknown = uuid4()
    service = FakeTripService()
    with create_test_client(service) as client:
        details = client.get(f"/api/v1/trips/{unknown}")
        status = client.get(f"/api/v1/trips/{unknown}/status")
    assert details.status_code == 404
    assert status.status_code == 404
    assert details.json()["detail"]["code"] == "trip_not_found"


def test_trip_list_is_disabled_by_default_and_paginates_when_enabled() -> None:
    service = FakeTripService()
    with create_test_client(service) as client:
        disabled = client.get("/api/v1/trips")
    assert disabled.status_code == 404

    with create_test_client(service, admin_enabled=True) as client:
        listed = client.get("/api/v1/trips?limit=5&offset=0")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["limit"] == 5
    assert len(listed.json()["items"]) == 1


def test_history_can_be_enabled_without_enabling_deletion() -> None:
    service = FakeTripService()
    with create_test_client(service, history_enabled=True) as client:
        response = client.get("/api/v1/trips?limit=8&offset=0")
        deleted = client.delete(f"/api/v1/trips/{service.trip_id}")
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == str(service.trip_id)
    assert response.json()["total"] == 1
    assert response.json()["limit"] == 8
    assert deleted.status_code == 404
    assert service.deleted == []


def test_empty_history_returns_an_empty_page() -> None:
    service = FakeTripService()

    async def empty_history(limit: int, offset: int):
        return [], 0

    service.list_trips = empty_history
    with create_test_client(service, history_enabled=True) as client:
        response = client.get("/api/v1/trips?limit=8&offset=0")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 8, "offset": 0}
    assert service.closed


@pytest.mark.parametrize(
    ("limit", "offset", "expected_indices"),
    [(2, 0, [0, 1]), (2, 2, [2]), (2, 3, []), (100, 0, [0, 1, 2])],
)
def test_history_pagination_preserves_total_and_record_order(
    limit: int, offset: int, expected_indices: list[int]
) -> None:
    service = FakeTripService()
    records = [
        service.summary.model_copy(update={"id": uuid4(), "title": f"Trip {index}"})
        for index in range(3)
    ]
    calls = []

    async def paginated_history(page_limit: int, page_offset: int):
        calls.append((page_limit, page_offset))
        return records[page_offset : page_offset + page_limit], len(records)

    service.list_trips = paginated_history
    with create_test_client(service, history_enabled=True) as client:
        response = client.get(f"/api/v1/trips?limit={limit}&offset={offset}")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == limit
    assert body["offset"] == offset
    assert [item["id"] for item in body["items"]] == [
        str(records[index].id) for index in expected_indices
    ]
    assert calls == [(limit, offset)]
    assert service.closed


@pytest.mark.parametrize(
    "query",
    [
        "limit=0", "limit=-1", "limit=101", "limit=abc", "limit=1.5",
        "offset=-1", "offset=100001", "offset=abc", "offset=1.5",
    ],
)
def test_history_rejects_invalid_query_parameters_without_querying_storage(query: str) -> None:
    service = FakeTripService()
    calls = []

    async def record_history_call(limit: int, offset: int):
        calls.append((limit, offset))
        return [], 0

    service.list_trips = record_history_call
    with create_test_client(service, history_enabled=True) as client:
        response = client.get(f"/api/v1/trips?{query}")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][0] == "query"
    assert calls == []


def test_history_uses_default_pagination_when_query_parameters_are_omitted() -> None:
    with create_test_client(FakeTripService(), history_enabled=True) as client:
        response = client.get("/api/v1/trips")
    assert response.status_code == 200
    assert response.json()["limit"] == 20
    assert response.json()["offset"] == 0
    assert response.json()["total"] == 1


def test_history_storage_failure_is_not_reported_as_empty_history() -> None:
    from app.services.trip_planning_service import TripPersistenceError

    service = FakeTripService()

    async def unavailable_history(limit: int, offset: int):
        raise TripPersistenceError("DATABASE_URL=private")

    service.list_trips = unavailable_history
    with create_test_client(service, history_enabled=True) as client:
        response = client.get("/api/v1/trips")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "trip_list_unavailable"
    assert "private" not in response.text
    assert service.closed


def test_submission_preserves_cost_preferences() -> None:
    service = FakeTripService()
    preferences = {
        "accommodation_nightly_rate_per_room": "100.00", "room_count": 2,
        "transport_mode": "flight", "intercity_round_trip_fare_per_traveler": "5000.00",
        "local_transport_allowance_per_day": "100.50",
        "food_allowance_per_traveler_per_day": "200.25",
        "activities_allowance_per_traveler_per_trip": "750.00",
        "contingency_percentage": "10.00",
    }
    with create_test_client(service) as client:
        response = client.post(
            "/api/v1/trips/plan",
            json={**valid_payload(), "cost_preferences": preferences},
        )
    assert response.status_code == 202
    assert service.created_request.cost_preferences.accommodation_nightly_rate_per_room == Decimal("100.00")
    assert service.started[0][1].cost_preferences.accommodation_nightly_rate_per_room == Decimal("100.00")
    assert service.created_request.cost_preferences.model_dump(mode="json", exclude_none=True) == preferences
    assert service.started[0][1].cost_preferences.model_dump(mode="json", exclude_none=True) == preferences


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "insufficient_data", "revision_required"])
def test_status_polling_preserves_terminal_outcomes(terminal_status: str) -> None:
    from app.services.trip_planning_service import TripPlanningService

    class NoRedis:
        async def get_task_status(self, trip_id):
            return None

        async def aclose(self):
            pass

    service = TripPlanningService(
        session_factory=lambda: None,
        task_status=NoRedis(),
        settings=Settings(_env_file=None),
    )
    now = datetime.now(timezone.utc)
    reason = "Verified destination coordinates are unavailable." if terminal_status == "insufficient_data" else None
    service._get_status_source = lambda trip_id: {
        "status": terminal_status, "created_at": now, "updated_at": now,
        "current_stage": "destination_research", "error": reason,
    }
    with create_test_client(service) as client:
        response = client.get(f"/api/v1/trips/{uuid4()}/status")
    assert response.status_code == 200
    assert response.json()["status"] == terminal_status
    assert response.json()["current_stage"] == "destination_research"
    assert response.json()["progress_percentage"] == 100
    assert response.json()["error_message"] == reason
    if terminal_status == "revision_required":
        assert response.json()["message"] == "The plan requires revisions before approval."


def test_persisted_status_recovers_last_agent_from_unordered_runs() -> None:
    from contextlib import nullcontext
    from datetime import timedelta
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app.services.trip_planning_service import TripPlanningService

    now = datetime.now(timezone.utc)
    trip = SimpleNamespace(
        status="insufficient_data", created_at=now, updated_at=now,
        agent_runs=[
            SimpleNamespace(id=uuid4(), agent_name="destination_research", started_at=now + timedelta(seconds=1), error_message=None),
            SimpleNamespace(id=uuid4(), agent_name="workflow", started_at=now, error_message="No verified destination coordinates."),
            SimpleNamespace(id=uuid4(), agent_name="requirements", started_at=now, error_message=None),
        ],
    )
    session = Mock()
    session.execute.return_value.scalar_one_or_none.return_value = trip
    service = TripPlanningService(
        session_factory=lambda: nullcontext(session), task_status=Mock(),
        settings=Settings(_env_file=None),
    )
    value = service._get_status_source(uuid4())
    assert value["current_stage"] == "destination_research"
    assert value["error"] == "No verified destination coordinates."


def test_delete_is_restricted_by_default_and_succeeds_when_enabled() -> None:
    service = FakeTripService()
    with create_test_client(service) as client:
        disabled = client.delete(f"/api/v1/trips/{service.trip_id}")
    assert disabled.status_code == 404
    assert service.deleted == []

    with create_test_client(service, admin_enabled=True) as client:
        deleted = client.delete(f"/api/v1/trips/{service.trip_id}")
    assert deleted.status_code == 204
    assert service.deleted == [service.trip_id]


def test_delete_unknown_trip_returns_404() -> None:
    service = FakeTripService()
    with create_test_client(service, admin_enabled=True) as client:
        response = client.delete(f"/api/v1/trips/{uuid4()}")
    assert response.status_code == 404


def test_original_health_and_status_endpoints_remain_functional() -> None:
    with create_test_client(FakeTripService()) as client:
        health = client.get("/health")
        status = client.get("/api/v1/status")
    assert health.status_code == 200
    assert health.json()["status"] == "healthy"
    assert status.status_code == 200
    assert status.json()["status"] == "healthy"


def test_trip_endpoints_are_registered_in_openapi() -> None:
    with create_test_client(FakeTripService()) as client:
        paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/trips/plan" in paths
    assert "/api/v1/trips/{trip_id}/status" in paths
    assert "/api/v1/trips/{trip_id}" in paths
    assert "/api/v1/trips" in paths


def test_workflow_failure_responses_do_not_expose_secrets() -> None:
    service = FakeTripService()

    async def failing_run(trip_id: UUID, request: Any) -> None:
        from app.services.trip_planning_service import TripPersistenceError

        raise TripPersistenceError("API_KEY=do-not-leak")

    service.run_trip = failing_run
    with create_test_client(service) as client:
        response = client.post("/api/v1/trips/plan", json=valid_payload())
    assert response.status_code == 202
    assert "do-not-leak" not in response.text


def test_create_storage_failure_is_sanitized() -> None:
    service = FakeTripService()

    async def fail_create(request: Any) -> UUID:
        from app.services.trip_planning_service import TripPersistenceError

        raise TripPersistenceError("DATABASE_URL=private-neon-url")

    service.create_trip = fail_create
    with create_test_client(service) as client:
        response = client.post("/api/v1/trips/plan", json=valid_payload())
    assert response.status_code == 503
    assert "private-neon-url" not in response.text


def test_redis_unavailable_status_falls_back_to_postgres_state() -> None:
    from app.services.trip_planning_service import TripPlanningService

    class UnavailableStatus:
        async def get_task_status(self, _trip_id: UUID) -> None:
            raise RuntimeError("UPSTASH_REDIS_REST_TOKEN=private-token")

    service = TripPlanningService(
        session_factory=lambda: None,
        task_status=UnavailableStatus(),  # type: ignore[arg-type]
    )
    trip_id = uuid4()
    now = datetime.now(timezone.utc)
    service._get_status_source = lambda _trip_id: {
        "status": "insufficient_data",
        "created_at": now,
        "updated_at": now,
        "error": None,
    }
    response = asyncio.run(service.get_trip_status(trip_id))
    assert response["status"] == "insufficient_data"
    assert response["progress_percentage"] == 100
    assert response["message"] == "Planning ended because required information is incomplete."


def test_trip_service_records_workflow_exception_without_secrets() -> None:
    from app.services.trip_planning_service import TripPlanningService

    class StatusRecorder:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []

        async def update_task_status(self, _trip_id: UUID, **values: Any) -> None:
            self.updates.append(values)

    async def failing_workflow(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("GEMINI_API_KEY=private-secret")

    async def run() -> None:
        status = StatusRecorder()
        service = TripPlanningService(
            session_factory=lambda: None,
            task_status=status,  # type: ignore[arg-type]
            workflow=failing_workflow,
            settings=Settings(_env_file=None),
        )
        persisted: list[tuple[Any, ...]] = []
        service._mark_running = lambda _trip_id: True
        service._persist_failure = (
            lambda *values: persisted.append(values) or True
        )
        trip_id = uuid4()
        await service.run_trip(
            trip_id,
            TravelRequest.model_validate(valid_payload()),
        )
        assert persisted
        assert status.updates[-1]["status"] == "failed"
        assert "private-secret" not in str(status.updates)

    asyncio.run(run())
