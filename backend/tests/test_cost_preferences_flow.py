"""Run all six real agents with isolated provider fixtures and ORM serialization."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from app.agents.accommodation_transport_agent import AccommodationTransportAgent
from app.agents.budget_agent import BudgetAgent
from app.agents.destination_research_agent import DestinationResearchAgent
from app.agents.itinerary_agent import ItineraryAgent
from app.agents.optimization_agent import OptimizationAgent
from app.agents.requirements_agent import RequirementsAgent
from app.core.config import Settings
from app.database.models import AgentRun, ItineraryActivity, ItineraryDay, TravelPreference, Trip
from app.schemas.external import GeocodingResult, PlaceResult, RouteResult, TravelSearchResult, TravelSearchSource
from app.schemas.trip_api import TripPlanRequest
from app.services.trip_planning_service import TripPlanningService
from app.tools.open_meteo import UnsupportedForecastDateError
from app.workflows.travel_graph import TravelAgentSet, plan_trip


def japan_request(**preference_overrides):
    preferences = {
        "accommodation_nightly_rate_per_room": "5000.00", "room_count": 1,
        "transport_mode": "flight", "intercity_round_trip_fare_per_traveler": "40000.00",
        "local_transport_allowance_per_day": "1000.00",
        "food_allowance_per_traveler_per_day": "1000.00",
        "activities_allowance_per_traveler_per_trip": "5000.00",
        "contingency_percentage": "10.00",
    }
    preferences.update(preference_overrides)
    return TripPlanRequest(
        origin="Hyderabad", destination="Japan", start_date="2026-10-15",
        end_date="2026-10-27", travelers_count=2, total_budget="200000.00",
        currency="INR", cost_preferences=preferences,
    )


class IsolatedProvider:
    async def geocode_destination(self, destination, **kwargs):
        return GeocodingResult(
            latitude=35.6, longitude=139.7,
            formatted_address=f"{destination}, test location", country="Japan",
            city=destination, result_type="city",
        )

    async def find_nearby_places(self, *args, **kwargs):
        return [PlaceResult(
            place_id=f"fixture-{index}", name=f"Test museum {index}",
            latitude=35.6 + index * 0.001, longitude=139.7,
            categories=["entertainment.museum"],
        ) for index in range(13)]

    async def calculate_route(self, *args, **kwargs):
        return RouteResult(profile="driving", distance_meters=1000, duration_seconds=600, geometry=[])

    async def get_weather(self, *args, **kwargs):
        raise UnsupportedForecastDateError("No forecast in this isolated test.")

    async def search_travel_information(self, query, **kwargs):
        return TravelSearchResult(query=query, sources=[TravelSearchSource(
            title="Test guide", url="https://example.com/test-guide", summary="Test source only.",
        )])

    async def aclose(self):
        pass


async def run_real_agents(request, provider=None):
    settings = Settings(_env_file=None)
    provider = provider or IsolatedProvider()
    agents = TravelAgentSet(
        requirements=RequirementsAgent(settings=settings),
        destination_research=DestinationResearchAgent(settings=settings, geoapify=provider, weather=provider, tavily=provider, routing=provider),
        accommodation_transport=AccommodationTransportAgent(settings=settings, geoapify=provider, routing=provider),
        budget=BudgetAgent(settings=settings),
        itinerary=ItineraryAgent(settings=settings, routing=provider),
        optimization_review=OptimizationAgent(settings=settings),
    )
    return await plan_trip(request, agents=agents)


def test_japan_estimates_propagate_through_all_six_real_agents():
    request = japan_request()
    state = asyncio.run(run_real_agents(request))
    assert state["normalized_requirements"].cost_preferences == request.cost_preferences
    assert state["normalized_requirements"].travel_duration_days == 13
    plan = state["accommodation_transport_plan"]
    assert plan.number_of_nights == 12
    assert plan.accommodation.room_count == 1
    assert plan.accommodation_cost.amount == Decimal("60000.00")
    assert plan.local_transport_cost.amount == Decimal("13000.00")
    assert plan.intercity_transport_cost.amount == Decimal("80000.00")
    assert not plan.unpriced_components
    assert plan.transport_options[0].mode == "flight"
    assert plan.transport_options[0].estimated_cost.amount == Decimal("80000.00")
    assert plan.accommodation.live_availability_checked is False
    budget = state["budget_breakdown"]
    assert {item.category: item.amount for item in budget.line_items} == {
        "accommodation": Decimal("60000.00"), "intercity_transport": Decimal("80000.00"),
        "local_transport": Decimal("13000.00"), "food": Decimal("26000.00"),
        "activities": Decimal("10000.00"), "contingency": Decimal("18900.00"),
    }
    assert budget.estimated_total == Decimal("207900.00")
    assert budget.remaining_budget == Decimal("-7900.00")
    assert budget.status == "OVER_BUDGET"
    assert budget.pricing_status == "COMPLETE_ESTIMATE"
    assert not budget.missing_price_inputs
    assert all(item.source == "user_assumption" for item in budget.line_items)
    assert state["optimization_review"].approved is False
    assert state["execution_status"] == "REVISION_REQUIRED"


def test_complete_user_estimates_can_pass_affordability_review():
    state = asyncio.run(run_real_agents(japan_request(intercity_round_trip_fare_per_traveler="30000.00")))
    assert state["budget_breakdown"].estimated_total == Decimal("185900.00")
    assert state["budget_breakdown"].remaining_budget == Decimal("14100.00")
    assert state["budget_breakdown"].pricing_status == "COMPLETE_ESTIMATE"
    assert state["optimization_review"].approved is True
    assert state["execution_status"] == "COMPLETED"


def test_complete_budget_does_not_override_critical_accessibility_failure():
    request = japan_request(intercity_round_trip_fare_per_traveler="30000.00")
    request.constraints = {"accessibility": "step-free access required"}
    state = asyncio.run(run_real_agents(request))
    assert state["budget_breakdown"].status == "WITHIN_BUDGET"
    assert state["budget_breakdown"].pricing_status == "COMPLETE_ESTIMATE"
    assert state["optimization_review"].approved is False
    assert any(issue.code == "accessibility_unverified" for issue in state["optimization_review"].issues)
    assert state["execution_status"] != "COMPLETED"


def test_room_count_fallback_is_shared_by_accommodation_and_budget_agents():
    state = asyncio.run(run_real_agents(japan_request(room_count=None)))
    assert state["accommodation_transport_plan"].accommodation.room_count == 1
    assert state["accommodation_transport_plan"].accommodation_cost.amount == Decimal("60000.00")
    assert next(item for item in state["budget_breakdown"].line_items if item.category == "accommodation").amount == Decimal("60000.00")


def test_budget_evidence_label_cannot_turn_user_estimates_into_live_prices():
    from app.schemas.budget import BudgetBreakdown
    state = asyncio.run(run_real_agents(japan_request()))
    payload = state["budget_breakdown"].model_dump(mode="json")
    payload["pricing_status"] = "LIVE_PRICE_VERIFIED"
    restored = BudgetBreakdown.model_validate(payload)
    assert restored.pricing_status == "COMPLETE_ESTIMATE"


def test_missing_food_stays_unknown_and_names_the_required_input():
    state = asyncio.run(run_real_agents(japan_request(food_allowance_per_traveler_per_day=None)))
    budget = state["budget_breakdown"]
    assert budget.estimated_total is None
    assert budget.pricing_status == "INCOMPLETE"
    assert budget.missing_price_inputs == ["food_allowance_per_traveler_per_day"]
    assert next(item for item in budget.line_items if item.category == "food").amount is None
    reserve = next(item for item in budget.line_items if item.category == "contingency")
    assert reserve.amount == Decimal("16300.00")
    assert reserve.source == "user_assumption"
    assert budget.known_cost_subtotal == Decimal("179300.00")
    assert state["optimization_review"].approved is False


def test_blank_preferences_do_not_generate_prices():
    request = japan_request()
    request.cost_preferences = None
    state = asyncio.run(run_real_agents(request))
    budget = state["budget_breakdown"]
    assert budget.estimated_total is None
    assert budget.remaining_budget is None
    assert budget.pricing_status == "INCOMPLETE"
    assert set(budget.missing_price_inputs) == {
        "accommodation_nightly_rate_per_room", "intercity_round_trip_fare_per_traveler",
        "local_transport_allowance_per_day", "food_allowance_per_traveler_per_day",
        "activities_allowance_per_traveler_per_trip",
    }
    assert all(item.amount is None and item.source == "unknown" for item in budget.line_items)


def test_decimal_rates_and_contingency_round_to_paise():
    state = asyncio.run(run_real_agents(japan_request(
        accommodation_nightly_rate_per_room="100.01", room_count=2,
        intercity_round_trip_fare_per_traveler="200.02",
        local_transport_allowance_per_day="10.03",
        food_allowance_per_traveler_per_day="20.04",
        activities_allowance_per_traveler_per_trip="30.05", contingency_percentage="7.25",
    )))
    budget = state["budget_breakdown"]
    # 2400.24 + 400.04 + 130.39 + 521.04 + 60.10 = 3511.81; reserve 254.61.
    assert budget.estimated_total == Decimal("3766.42")
    assert budget.remaining_budget == Decimal("196233.58")
    assert state["accommodation_transport_plan"].accommodation.room_count == 2


def test_cost_preferences_and_budget_survive_orm_persistence_and_retrieval():
    request = japan_request(intercity_round_trip_fare_per_traveler="30000.00")
    state = asyncio.run(run_real_agents(request))
    captured = []
    session = MagicMock()
    session.__enter__.return_value = session

    def capture(record):
        record.id = uuid4()
        captured.append(record)

    session.add.side_effect = capture
    service = TripPlanningService(
        session_factory=lambda: session, task_status=MagicMock(), settings=Settings(_env_file=None),
    )
    trip_id = uuid4()
    service._create_trip(trip_id, request)
    preference = next(item for item in captured if isinstance(item, TravelPreference))
    trip = next(item for item in captured if isinstance(item, Trip))
    trip.id = trip_id
    trip.preference = preference
    trip.created_at = trip.updated_at = datetime.now(timezone.utc)
    session.execute.return_value.scalar_one_or_none.return_value = trip
    assert service._persist_result(trip_id, state)
    trip.agent_runs = [item for item in captured if isinstance(item, AgentRun)]
    trip.itinerary_days = [item for item in captured if isinstance(item, ItineraryDay)]
    for day in trip.itinerary_days:
        day.activities = [item for item in captured if isinstance(item, ItineraryActivity) and item.itinerary_day_id == day.id]
    saved = service._get_trip(trip_id)
    assert saved.request["constraints"]["cost_preferences"]["room_count"] == 1
    assert saved.total_estimated_cost == Decimal("185900.00")
    assert saved.budget["pricing_status"] == "COMPLETE_ESTIMATE"
    assert saved.budget["remaining_budget"] == "14100.00"
    assert saved.budget["line_items"][0]["source"] == "user_assumption"
    assert len(saved.days) == 13
    assert sum(day.estimated_daily_cost for day in saved.days) == Decimal("185900.00")
