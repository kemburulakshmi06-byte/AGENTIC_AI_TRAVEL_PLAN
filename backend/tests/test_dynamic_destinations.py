"""Destination names and coordinates in this file are isolated test fixtures only."""
import asyncio
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.schemas.external import GeocodingResult, PlaceResult, RouteResult
from app.schemas.trip_api import TripPlanRequest
from app.tools.base import TravelAPIUnavailableError
from test_cost_preferences_flow import IsolatedProvider, run_real_agents


class DynamicProvider(IsolatedProvider):
    def __init__(self, locations, cities=None, categories=None, transfer_seconds=1800):
        self.locations = locations
        self.cities = cities or {}
        self.categories = categories or {}
        self.transfer_seconds = transfer_seconds
        self.geocoding_calls = []
        self.place_calls = []
        self.route_calls = []

    async def geocode_destination(self, destination, **kwargs):
        self.geocoding_calls.append(destination)
        return self.locations.get(destination)

    async def find_destination_cities(self, location, **kwargs):
        return [self.locations[name] for name in self.cities.get(location.name, [])]

    async def find_nearby_places(self, latitude, longitude, **kwargs):
        self.place_calls.append((latitude, longitude))
        location = next(value for value in self.locations.values() if value.latitude == latitude and value.longitude == longitude)
        return [PlaceResult(
            place_id=f"{location.name}-{index}", name=f"{location.name} fixture attraction {index}",
            latitude=latitude + index * 0.0001, longitude=longitude,
            categories=self.categories.get(location.name, ["history", "entertainment.museum"]),
        ) for index in range(16)]

    async def calculate_route(self, origin, destination, *, profile="driving"):
        self.route_calls.append((origin, destination, profile))
        if self.transfer_seconds is None:
            raise TravelAPIUnavailableError("openrouteservice", "Fixture routing outage.")
        return RouteResult(profile=profile, distance_meters=1000, duration_seconds=self.transfer_seconds, geometry=[])


def location(name, country, latitude, longitude, kind="city"):
    return GeocodingResult(
        name=name, city=name if kind == "city" else None,
        state=name if kind == "state" else None, country=country,
        formatted_address=f"{name}, {country}", latitude=latitude, longitude=longitude,
        result_type=kind, place_id=f"fixture-{name}",
    )


def request(origin, destination, days=4, travelers=2, currency="INR", **overrides):
    values = dict(
        origin=origin, destination=destination, start_date=date(2027, 3, 1),
        end_date=date(2027, 3, 1) + timedelta(days=days - 1),
        travelers_count=travelers, currency=currency, total_budget="200000.00",
        interests=["history"], cost_preferences={
            "accommodation_nightly_rate_per_room": "100.25", "room_count": 2,
            "intercity_round_trip_fare_per_traveler": "500.50", "transport_mode": "driving",
            "local_transport_allowance_per_day": "20.25",
            "food_allowance_per_traveler_per_day": "30.50",
            "activities_allowance_per_traveler_per_trip": "40.75", "contingency_percentage": "5.00",
        },
    )
    values.update(overrides)
    return TripPlanRequest(**values)


@pytest.mark.parametrize("origin,destination,country,kind,base,days,travelers,currency", [
    ("Hyderabad", "Tokyo", "Japan", "city", "Tokyo", 3, 2, "INR"),
    ("Bengaluru", "Goa", "India", "state", "Panaji", 4, 3, "INR"),
    ("Delhi", "Paris", "France", "city", "Paris", 5, 1, "EUR"),
    ("Mumbai", "Dubai", "United Arab Emirates", "city", "Dubai", 6, 4, "AED"),
    ("Chennai", "Kerala", "India", "state", "Kochi", 7, 2, "INR"),
])
def test_requested_scenarios_use_dynamic_locations_dates_currency_and_costs(origin, destination, country, kind, base, days, travelers, currency):
    locations = {origin: location(origin, "India", 12, 77), destination: location(destination, country, 30, 120, kind)}
    cities = {}
    if kind != "city":
        locations[base] = location(base, country, 31, 121)
        locations["Alternative fixture town"] = location("Alternative fixture town", country, 32, 122)
        cities[destination] = [base, "Alternative fixture town"]
    provider = DynamicProvider(locations, cities, {"Alternative fixture town": ["nature"]})
    trip_request = request(origin, destination, days, travelers, currency)
    state = asyncio.run(run_real_agents(trip_request, provider))
    assert state["execution_status"] == "COMPLETED"
    assert state["normalized_requirements"].destination == destination
    assert state["normalized_requirements"].origin == origin
    assert state["normalized_requirements"].travel_duration_days == days
    research = state["destination_research"]
    assert base in research.destination_name
    assert (research.latitude, research.longitude) == (locations[base].latitude, locations[base].longitude)
    assert destination in provider.geocoding_calls
    assert origin in provider.geocoding_calls
    assert len(state["trip_itinerary"].days) == days
    assert state["trip_itinerary"].end_date == trip_request.end_date
    amounts = {item.category: item.amount for item in state["budget_breakdown"].line_items}
    assert amounts["accommodation"] == Decimal("100.25") * 2 * (days - 1)
    assert amounts["intercity_transport"] == Decimal("500.50") * travelers
    assert amounts["food"] == Decimal("30.50") * travelers * days
    assert state["budget_breakdown"].currency == currency
    assert state["budget_breakdown"].pricing_status == "COMPLETE_ESTIMATE"
    assert all(attraction.name.startswith(base) for attraction in research.attractions)


@pytest.mark.parametrize("kind", ["country", "state"])
def test_ambiguous_country_or_region_requests_require_a_choice(kind):
    locations = {"Requested area": location("Requested area", "Requested area", 20, 100, kind),
                 "Candidate A": location("Candidate A", "Requested area", 21, 101),
                 "Candidate B": location("Candidate B", "Requested area", 22, 102)}
    provider = DynamicProvider(locations, {"Requested area": ["Candidate A", "Candidate B"]})
    state = asyncio.run(run_real_agents(request("Origin", "Requested area"), provider))
    assert state["execution_status"] == "INSUFFICIENT_DATA"
    assert state["destination_research"].clarification_questions
    assert len(state["destination_research"].location_candidates) == 2
    assert "trip_itinerary" not in state


def test_country_does_not_treat_a_unique_interest_match_as_a_national_recommendation():
    locations = {"Requested country": location("Requested country", "Requested country", 20, 100, "country"),
                 "History base": location("History base", "Requested country", 21, 101),
                 "Nature base": location("Nature base", "Requested country", 22, 102)}
    provider = DynamicProvider(locations, {"Requested country": ["History base", "Nature base"]}, {"Nature base": ["nature"]})
    state = asyncio.run(run_real_agents(request("Origin", "Requested country"), provider))
    assert state["execution_status"] == "INSUFFICIENT_DATA"
    assert state["destination_research"].resolution_status == "clarification_required"
    assert not provider.place_calls
    assert "unranked" in " ".join(state["warnings"])
    assert "trip_itinerary" not in state


def test_reviewer_rejects_an_omitted_intercity_transfer():
    from app.agents.optimization_agent import OptimizationAgent
    from app.core.config import Settings
    from app.schemas.agent_schemas import AgentContext

    async def run():
        trip_request = request("Origin", "City A > City B", days=6)
        state = await run_real_agents(trip_request, multi_city_provider())
        itinerary = state["trip_itinerary"].model_copy(deep=True)
        for day in itinerary.days:
            day.activities = [activity for activity in day.activities if activity.intercity_transfer is None]
        result = await OptimizationAgent(settings=Settings(_env_file=None)).run(AgentContext(
            request=trip_request, normalized_requirements=state["normalized_requirements"],
            destination_research=state["destination_research"].model_dump(mode="json"),
            accommodation_transport_plan=state["accommodation_transport_plan"].model_dump(mode="json"),
            budget_breakdown=state["budget_breakdown"].model_dump(mode="json"),
            trip_itinerary=itinerary.model_dump(mode="json"),
        ))
        assert result.output.approved is False
        assert any(issue.code == "missing_intercity_transfer" for issue in result.output.issues)

    asyncio.run(run())


def multi_city_provider(transfer_seconds=1800):
    return DynamicProvider({
        "Origin": location("Origin", "Country", 10, 10),
        "City A": location("City A", "Country", 11, 11),
        "City B": location("City B", "Country", 12, 12),
    }, transfer_seconds=transfer_seconds)


def test_multi_city_trip_reserves_provider_transfer_time_and_groups_attractions():
    provider = multi_city_provider()
    state = asyncio.run(run_real_agents(request("Origin", "City A > City B", days=6), provider))
    assert state["execution_status"] == "COMPLETED"
    segments = state["destination_research"].city_segments
    assert len(segments) == 2
    assert segments[1].transfer_from_previous.duration_minutes == Decimal("30")
    for segment in segments:
        allowed = {item.place_id for item in segment.attractions}
        for day in state["trip_itinerary"].days:
            if segment.start_date <= day.date <= segment.end_date:
                assert all(activity.place_id in allowed for activity in day.activities if activity.activity_type == "attraction")
    transfer_day = next(day for day in state["trip_itinerary"].days if day.date == segments[1].start_date)
    transfer = next(activity for activity in transfer_day.activities if activity.intercity_transfer)
    assert transfer.duration_minutes == 30
    assert transfer.intercity_transfer.provider == "openrouteservice"
    assert all(activity.start_time >= transfer.end_time for activity in transfer_day.activities if activity.activity_type == "attraction")


@pytest.mark.parametrize("transfer_seconds", [None, 9 * 3600])
def test_unavailable_or_impossible_multi_city_transfers_do_not_generate_a_plan(transfer_seconds):
    state = asyncio.run(run_real_agents(request("Origin", "City A > City B", days=6), multi_city_provider(transfer_seconds)))
    assert state["execution_status"] == "INSUFFICIENT_DATA"
    assert state["destination_research"].clarification_questions
    assert "trip_itinerary" not in state


def test_unsupported_flight_transfer_requests_are_not_replaced_with_driving():
    trip_request = request("Origin", "City A > City B", days=6)
    trip_request.cost_preferences.transport_mode = "flight"
    provider = multi_city_provider()
    state = asyncio.run(run_real_agents(trip_request, provider))
    assert state["execution_status"] == "INSUFFICIENT_DATA"
    assert not provider.route_calls


def test_explicit_city_outside_requested_country_requires_clarification():
    provider = multi_city_provider()
    provider.locations["Requested country"] = location("Requested country", "Requested country", 20, 20, "country")
    trip_request = request("Origin", "Requested country", days=6, constraints={"destination_cities": ["City A", "City B"]})
    state = asyncio.run(run_real_agents(trip_request, provider))
    assert state["execution_status"] == "INSUFFICIENT_DATA"
    assert "within the requested" in " ".join(state["warnings"])
    assert not provider.route_calls


def test_return_to_an_earlier_city_preserves_order_and_both_transfers():
    state = asyncio.run(run_real_agents(request("Origin", "City A > City B > City A", days=8), multi_city_provider()))
    assert state["execution_status"] == "COMPLETED"
    segments = state["destination_research"].city_segments
    assert [segment.location_name for segment in segments] == ["City A, Country", "City B, Country", "City A, Country"]
    assert len([activity for day in state["trip_itinerary"].days for activity in day.activities if activity.intercity_transfer]) == 2


@pytest.mark.parametrize("failure,expected_code", [("gap", "invalid_city_dates"), ("wrong_city", "incorrect_city_order"), ("cross_city", "cross_city_activity")])
def test_reviewer_rejects_inconsistent_multi_city_research(failure, expected_code):
    from app.agents.optimization_agent import _validate_city_segments

    state = asyncio.run(run_real_agents(request("Origin", "City A > City B", days=6), multi_city_provider()))
    research = state["destination_research"].model_copy(deep=True)
    if failure == "gap":
        research.city_segments[1].start_date += timedelta(days=1)
    elif failure == "wrong_city":
        research.city_segments[1].location_name = "Unrequested city"
    else:
        research.city_segments[0].attractions = research.city_segments[1].attractions
    issues = []
    _validate_city_segments(state["trip_itinerary"], research, state["normalized_requirements"], issues)
    assert any(issue.code == expected_code and issue.blocks_approval for issue in issues)
