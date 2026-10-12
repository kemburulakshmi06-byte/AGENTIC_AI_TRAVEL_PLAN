"""Real API/service/graph with isolated ORM objects and fictional provider fixtures.

This is not a PostgreSQL integration test or evidence of current travel prices.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.agents.accommodation_transport_agent import AccommodationTransportAgent
from app.agents.budget_agent import BudgetAgent
from app.agents.destination_research_agent import DestinationResearchAgent
from app.agents.itinerary_agent import ItineraryAgent
from app.agents.optimization_agent import OptimizationAgent
from app.agents.requirements_agent import RequirementsAgent
from app.core.config import Settings
from app.database.models import AgentRun, ItineraryActivity, ItineraryDay, TravelPreference, Trip
from app.services.cost_estimation_service import CostEstimationService
from app.services.trip_planning_service import TripPlanningService
from app.schemas.trip_api import TripPlanRequest
from app.tools.base import TravelAPIError
from app.tools.geoapify import _destination_match_score
from app.workflows.travel_graph import TravelAgentSet, plan_trip
from test_automatic_cost_estimation import FXFixture, stable_reference_date
from test_dynamic_destinations import DynamicProvider, location
from test_trip_api import create_test_client


ALLOWANCES = {
    "accommodation_nightly_rate_per_room": "2000", "room_count": 1,
    "intercity_round_trip_fare_per_traveler": "1000",
    "local_transport_allowance_per_day": "200",
    "food_allowance_per_traveler_per_day": "300",
    "activities_allowance_per_traveler_per_trip": "500", "contingency_percentage": "10",
}


def payload(origin="Hyderabad, India", destination="Tokyo, Japan", **changes):
    return {"origin": origin, "destination": destination, "start_date": "2026-10-13",
            "end_date": "2026-10-17", "travelers_count": 2, "total_budget": "20000",
            "currency": "INR", "travel_style": "balanced", "interests": ["Culture"], **changes}


class ORMStore:
    """Capture real mapped objects, without opening any database connection."""
    def __init__(self):
        self.records = []
        self.trip = None
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.session.add.side_effect = self.add
        self.session.execute.side_effect = self.execute

    def add(self, record):
        record.id = record.id or uuid4()
        record.created_at = record.updated_at = datetime.now(timezone.utc)
        self.records.append(record)
        if isinstance(record, Trip):
            self.trip = record
            record.preference = next(item for item in self.records if isinstance(item, TravelPreference))

    def execute(self, statement):
        if self.trip is not None:
            self.trip.agent_runs = [item for item in self.records if isinstance(item, AgentRun)]
            self.trip.itinerary_days = [item for item in self.records if isinstance(item, ItineraryDay)]
            for day in self.trip.itinerary_days:
                day.activities = [item for item in self.records if isinstance(item, ItineraryActivity) and item.itinerary_day_id == day.id]
        response = MagicMock()
        response.scalar_one_or_none.return_value = self.trip
        return response


def isolated_service(request, *, pricing_failure=False, worker_failure=False, settings_override=None):
    store = ORMStore()
    settings = settings_override or Settings(_env_file=None, TRIP_EXECUTION_MODE="background", AMADEUS_API_KEY="test-only" if pricing_failure else None,
                        AMADEUS_API_SECRET="test-only" if pricing_failure else None)
    country = "jp" if "Tokyo" in request["destination"] else "ae" if "Dubai" in request["destination"] else "in"
    # Only test fixtures use these synthetic locations/places/routes.
    city = request["destination"].split(",")[0]
    country_name, latitude, longitude = {"jp": ("Japan", 35, 139), "ae": ("United Arab Emirates", 25, 55), "in": ("India", 12, 77)}[country]
    target = location(city, country_name, latitude, longitude).model_copy(update={"country_code": country})
    origin = location(request["origin"].split(",")[0], "India", 17, 78,
                      "country" if request["origin"].lower() == "india" else "city").model_copy(update={"country_code": "in"})
    provider = DynamicProvider({request["origin"]: origin, request["destination"]: target})

    class FailedPricing:
        async def flight_cost(self, *args, **kwargs):
            raise TravelAPIError("amadeus", "PRIVATE PROVIDER BODY", category="timeout")

    async def workflow(trip_request, **kwargs):
        if worker_failure:
            raise RuntimeError("PRIVATE WORKER DETAILS")
        return await plan_trip(trip_request, agents=TravelAgentSet(
            requirements=RequirementsAgent(settings=settings),
            destination_research=DestinationResearchAgent(settings=settings, geoapify=provider, weather=provider, tavily=provider, routing=provider),
            accommodation_transport=AccommodationTransportAgent(settings=settings, geoapify=provider, routing=provider),
            budget=BudgetAgent(settings=settings, cost_estimation_service=CostEstimationService(settings, fx=FXFixture(), pricing=FailedPricing() if pricing_failure else None)),
            itinerary=ItineraryAgent(settings=settings, routing=provider),
            optimization_review=OptimizationAgent(settings=settings)), **kwargs)

    cache = MagicMock()
    cache.create_task_status = AsyncMock()
    cache.update_task_status = AsyncMock()
    cache.get_task_status = AsyncMock(return_value=None)
    cache.aclose = AsyncMock()
    service = TripPlanningService(lambda: store.session, cache, workflow=workflow, settings=settings)
    return service, store


def api_flow(request, **options):
    service, store = isolated_service(request, **options)
    with create_test_client(service) as client:
        submitted = client.post("/api/v1/trips/plan", json=request)
        assert submitted.status_code == 202
        assert submitted.json()["status"] == "queued"
        trip_id = submitted.json()["trip_id"]
        status = client.get(f"/api/v1/trips/{trip_id}/status")
        details = client.get(f"/api/v1/trips/{trip_id}")
        assert status.status_code == details.status_code == 200
    return status.json(), details.json(), store


@pytest.mark.parametrize("case,trip_payload,expected", [
    ("Reported", payload(origin="Hyderabad", destination="Bengaluru"), "insufficient_data"),
    ("A", payload(), "revision_required"),
    ("B", payload(destination="Dubai, UAE"), "insufficient_data"),
    ("Goa", payload(destination="Goa, India"), "insufficient_data"),
    ("C", payload(origin="India"), "insufficient_data"),
    ("D", payload(origin="India", destination="Dubai", currency="AED"), "insufficient_data"),
    ("E", payload(destination="Bengaluru", cost_preferences=ALLOWANCES), "completed"),
    ("F", payload(destination="Dubai, UAE", cost_preferences=ALLOWANCES), "completed"),
    ("G", payload(origin="Unsupported origin", destination="Unsupported city"), "insufficient_data"),
    ("J", payload(total_budget="1000"), "revision_required"),
])
def test_requested_scenarios_through_real_api_background_service_and_six_agents(case, trip_payload, expected):
    status, details, store = api_flow(trip_payload)
    assert status["status"] == details["status"] == expected
    assert len(details["days"]) == 5 and details["itinerary"]["days"]
    assert details["accommodation_transport"]["accommodation"]["room_count"] == 1
    assert details["budget"]["booking_readiness"] == "unverified"
    assert {item.agent_name for item in store.records if isinstance(item, AgentRun)} >= {
        "requirements", "destination_research", "accommodation_transport", "budget", "itinerary", "optimization_review", "workflow"}
    if case in {"E", "F"}:
        # Domestic support here uses explicit user allowances, not invented India rates.
        assert details["total_estimated_cost"] == "16500.00"
        assert details["budget"]["remaining_budget"] == "3500.00"
        assert details["request"]["cost_preferences"]["room_count"] == 1
    elif case in {"A", "J"}:
        assert details["budget"]["status"] == "OVER_BUDGET"
        assert details["optimization_review"]["approved"] is False
    else:
        assert details["total_estimated_cost"] is None
        assert details["budget"]["pricing_status"] == "INCOMPLETE"
        contingency = next(item for item in details["budget"]["line_items"] if item["category"] == "contingency")
        if case == "C":
            assert contingency["amount"] == "9474.00"
            assert contingency["source"] == "configured_assumption"
        else:
            assert contingency["amount"] is None
        if case in {"C", "D"}:
            assert details["accommodation_transport"]["origin_resolution_status"] == "broad"
            assert any("choose a departure city" in action for action in details["budget"]["missing_price_actions"])


def test_pricing_api_timeout_uses_supported_reference_without_exposing_private_body():
    status, details, _ = api_flow(payload(total_budget="500000"), pricing_failure=True)
    assert status["status"] == "completed"
    assert details["budget"]["pricing_status"] == "COMPLETE_ESTIMATE"
    assert next(item for item in details["budget"]["line_items"] if item["category"] == "intercity_transport")["source"] == "reference_data"
    assert "PRIVATE PROVIDER BODY" not in json.dumps(details)


def test_background_exception_returns_failed_through_status_and_details():
    status, details, _ = api_flow(payload(), worker_failure=True)
    assert status["status"] == details["status"] == "failed"
    assert details["budget"] is None and details["days"] == []
    assert status["error_message"] and "PRIVATE WORKER DETAILS" not in json.dumps(details)


@pytest.mark.parametrize("query,city,country,code,accepted", [
    ("Dubai, UAE", "Dubai", "United Arab Emirates", "ae", True),
    ("Tokyo, JP", "Tokyo", "Japan", "jp", True),
    ("Paris, FR", "Paris", "France", "fr", True),
    ("London, GB", "London", "United Kingdom", "gb", True),
    ("Dubai, UAE", "Dubai", "India", "in", False),
    ("Cape Town, SA", "Cape Town", "South Africa", "za", False),
    ("Tokyo, AE", "Tokyo", "Japan", "jp", False),
])
def test_provider_country_identifiers_match_without_accepting_wrong_countries(query, city, country, code, accepted):
    result = location(city, country, 20, 30).model_copy(update={"country_code": code})
    assert (_destination_match_score(query, result) is not None) is accepted


def test_country_origin_never_becomes_an_arbitrary_city_price_even_if_a_provider_is_configured():
    status, details, _ = api_flow(payload(origin="India"), pricing_failure=True)
    assert status["status"] == "insufficient_data"
    assert not any("Amadeus pricing was unavailable" in warning for warning in details["warnings"])
    assert any("choose a departure city" in action for action in details["budget"]["missing_price_actions"])


def test_restarted_service_reports_expired_queued_work_as_failed_via_status_api():
    service, store = isolated_service(payload())
    trip_id = uuid4()
    service._create_trip(trip_id, TripPlanRequest(**payload()))
    store.trip.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    # A fresh service has no surviving in-process background task or Redis status.
    restarted = TripPlanningService(lambda: store.session, service._task_status,
                                    settings=Settings(_env_file=None))
    with create_test_client(restarted) as client:
        status = client.get(f"/api/v1/trips/{trip_id}/status")
        details = client.get(f"/api/v1/trips/{trip_id}")
    assert status.status_code == details.status_code == 200
    assert status.json()["status"] == details.json()["status"] == "failed"
    assert "background worker stopped" in status.json()["error_message"]
    assert details.json()["days"] == [] and details.json()["total_estimated_cost"] is None
