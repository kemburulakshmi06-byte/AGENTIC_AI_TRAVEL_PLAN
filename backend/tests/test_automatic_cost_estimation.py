"""HTTP/LLM fixtures are test-only; no production database or private requests."""
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from app.agents.budget_agent import BudgetAgent
from app.agents.accommodation_transport_agent import AccommodationTransportAgent
from app.agents.destination_research_agent import DestinationResearchAgent
from app.agents.itinerary_agent import ItineraryAgent
from app.agents.optimization_agent import OptimizationAgent
from app.agents.requirements_agent import RequirementsAgent
from app.core.config import Settings
from app.database.models import AgentRun, ItineraryActivity, ItineraryDay, TravelPreference, Trip
from app.schemas.agent_schemas import AgentContext, NormalizedTravelRequirements
from app.schemas.budget import BudgetBreakdown, BudgetLineItem
from app.schemas.trip_api import TripPlanRequest
from app.services.cost_estimation_service import CostEstimationService, ReferenceFXTool, ECBReferenceFXTool
from app.services.trip_planning_service import TripPlanningService
from app.tools.base import TravelAPIError
from app.tools.travel_pricing import AmadeusPricingTool
from app.workflows.travel_graph import TravelAgentSet, plan_trip
from test_cost_preferences_flow import IsolatedProvider
from test_dynamic_destinations import DynamicProvider, location, request as dynamic_request
from test_external_api_tools import FakeCache


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 11)


@pytest.fixture(autouse=True)
def stable_reference_date(monkeypatch):
    monkeypatch.setattr("app.services.cost_estimation_service.date", FixedDate)


class FXFixture:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def conversion(self, base, quote):
        self.calls.append((base, quote))
        if self.fail:
            raise TravelAPIError("frankfurter", "PRIVATE", category="timeout")
        # Explicitly fictional deterministic conversion used only in isolated tests.
        return (Decimal("1") if base == quote else Decimal("0.60"), date(2026, 10, 9),
                None if base == quote else f"https://api.frankfurter.dev/v2/rate/{base}/{quote}")


def req(**overrides):
    return TripPlanRequest(**(dict(origin="Hyderabad", destination="Tokyo", start_date="2026-10-15",
        end_date="2026-10-19", travelers_count=2, currency="INR", total_budget="500000",
        travel_style="balanced", interests=["Culture"]) | overrides))


def agent_context(request=None, research=None):
    request = request or req()
    values = request.model_dump(exclude={"user_message"})
    values["travel_duration_days"] = (request.end_date - request.start_date).days + 1
    return AgentContext(request=request, normalized_requirements=NormalizedTravelRequirements.model_validate(values),
        destination_research=research or {"destination_name": "Tokyo, Japan", "country_code": "jp", "resolution_status": "verified"})


def run_budget(request=None, *, fx=None, settings=None, research=None, data=None):
    settings = settings or Settings(_env_file=None)
    service = CostEstimationService(settings, fx=fx or FXFixture(), reference_data=data)
    return asyncio.run(BudgetAgent(settings=settings, cost_estimation_service=service).run(agent_context(request, research)))


def test_basic_details_produce_complete_decimal_reference_budget_without_manual_costs():
    fx = FXFixture()
    result = run_budget(fx=fx)
    budget = result.output
    amounts = {item.category: item.amount for item in budget.line_items}
    assert amounts == {"accommodation": Decimal("48000.00"), "intercity_transport": Decimal("257064.00"),
                       "local_transport": Decimal("15180.00"), "food": Decimal("19560.00"),
                       "activities": Decimal("12000.00"), "contingency": Decimal("35180.40")}
    assert budget.estimated_total == Decimal("386984.40")
    assert budget.remaining_budget == Decimal("113015.60")
    assert budget.minimum_estimated_total == Decimal("297655.60")
    assert budget.maximum_estimated_total == Decimal("572589.60")
    assert budget.pricing_status == "COMPLETE_ESTIMATE"
    assert budget.booking_readiness == "unverified"
    assert budget.estimate_confidence == "low"
    assert result.status == "completed"
    assert not budget.missing_price_inputs
    assert all(item.evidence_kind == "planning_estimate" for item in budget.line_items)
    assert all(item.price_sources and item.assumptions for item in budget.line_items)
    assert fx.calls.count(("JPY", "INR")) == 1
    assert BudgetBreakdown.model_validate_json(budget.model_dump_json()) == budget


def test_example_budget_overrun_is_preserved_not_changed_into_success():
    budget = run_budget(req(total_budget="250000")).output
    assert budget.status == "OVER_BUDGET"
    assert budget.remaining_budget == Decimal("-136984.40")
    assert budget.pricing_status == "COMPLETE_ESTIMATE"


@pytest.mark.parametrize("count,rooms", [(1, 1), (3, 2), (5, 3)])
def test_travelers_nights_and_inclusive_days_are_dynamic(count, rooms):
    budget = run_budget(req(travelers_count=count, end_date="2026-10-17", total_budget="2000000")).output
    amounts = {item.category: item.amount for item in budget.line_items}
    assert amounts["accommodation"] == Decimal("20000") * rooms * 2 * Decimal("0.60")
    assert amounts["food"] == Decimal("3260") * count * 3 * Decimal("0.60")
    assert amounts["intercity_transport"] == Decimal("128532") * count


def test_supported_references_precede_fallback_allowances_and_preserve_room_count():
    budget = run_budget(req(cost_preferences={"room_count": 2,
        "food_allowance_per_traveler_per_day": "20.01", "contingency_percentage": "7.25"})).output
    amounts = {item.category: item for item in budget.line_items}
    assert amounts["accommodation"].amount == Decimal("96000")
    assert amounts["food"].amount == Decimal("19560.00")
    assert amounts["food"].source == "reference_data"
    assert amounts["contingency"].amount == Decimal("28985.79")


def test_fx_failure_keeps_foreign_categories_unknown_and_partial_fare_priced():
    budget = run_budget(fx=FXFixture(fail=True)).output
    assert budget.estimated_total is None and budget.minimum_estimated_total is None
    assert budget.pricing_status == "INCOMPLETE"
    assert budget.known_cost_subtotal == Decimal("282770.40")
    assert all(item.amount is None for item in budget.line_items if item.category not in {"intercity_transport", "contingency"})
    reserve = next(item for item in budget.line_items if item.category == "contingency")
    assert reserve.amount == Decimal("25706.40")
    assert reserve.source == "configured_assumption"
    assert any("unknown expenses are not treated as zero" in item for item in reserve.assumptions)
    assert any("JPY/INR" in action for action in budget.missing_price_actions)
    assert "PRIVATE" not in str(budget)


def test_disabled_fx_keeps_same_currency_fare_as_a_useful_partial_budget():
    budget = run_budget(settings=Settings(_env_file=None, REFERENCE_FX_ENABLED=False)).output
    assert budget.known_cost_subtotal == Decimal("282770.40")
    assert budget.estimated_total is None and budget.remaining_budget is None
    assert budget.status == "INCOMPLETE_ESTIMATE"
    assert budget.pricing_status == "INCOMPLETE"
    assert next(item for item in budget.line_items if item.category == "contingency").amount == Decimal("25706.40")
    assert any("enable reference FX" in action for action in budget.missing_price_actions)


def test_exact_dated_hyderabad_dubai_reference_is_scoped_and_non_live(monkeypatch):
    import app.services.cost_estimation_service as module

    class ObservationDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 12)

    monkeypatch.setattr(module, "date", ObservationDate)
    request = req(
        destination="Dubai, UAE",
        start_date="2026-10-20",
        end_date="2026-10-24",
        total_budget="150000",
    )
    research = {
        "destination_name": "Dubai, UAE",
        "country_code": "ae",
        "resolution_status": "verified",
    }
    budget = run_budget(request, research=research).output
    transport = next(
        item for item in budget.line_items if item.category == "intercity_transport"
    )
    assert transport.amount == Decimal("62241.66")
    assert transport.currency == "INR"
    assert transport.source == "reference_data"
    assert transport.classification == "estimated"
    assert transport.evidence_kind == "planning_estimate"
    assert transport.confidence == "low"
    assert transport.minimum_amount is None and transport.maximum_amount is None
    assert len(transport.price_sources) == 3
    assert "2026-10-20 to 2026-10-24" in (transport.pricing_basis or "")
    assert any("not a live quote" in item for item in transport.assumptions)

    changed_dates = run_budget(
        request.model_copy(update={"end_date": date(2026, 10, 25)}),
        research=research,
    ).output
    changed_transport = next(
        item for item in changed_dates.line_items
        if item.category == "intercity_transport"
    )
    assert changed_transport.amount is None
    assert any(
        action.startswith("intercity_transport:")
        and "requested departure and return dates" in action
        for action in changed_dates.missing_price_actions
    )


@pytest.mark.parametrize("origin,destination,country", [("Bengaluru", "Goa", "in"), ("Delhi", "Paris", "fr"),
    ("Mumbai", "Dubai", "ae"), ("Chennai", "Kochi", "in")])
def test_unsupported_reference_coverage_does_not_invent_costs(origin, destination, country):
    budget = run_budget(req(origin=origin, destination=destination), research={"destination_name": destination,
        "country_code": country, "resolution_status": "verified"}).output
    assert budget.estimated_total is None
    assert all(item.amount is None for item in budget.line_items)
    assert len(budget.missing_price_actions) == 6
    assert next(item for item in budget.line_items if item.category == "contingency").amount is None
    # Fixed date is October 11; the new historical rows are observed October 12.
    # Future observations must remain unavailable until their observation date.
    assert all("no coverage" in action or "no currently valid reference" in action for action in budget.missing_price_actions[:5])


@pytest.mark.parametrize("changes,category", [({"origin": "Bengaluru"}, "intercity_transport"),
    ({"origin": "Hyderabad, United States"}, "intercity_transport"),
    ({"cost_preferences": {"transport_mode": "train"}}, "intercity_transport"),
    ({"constraints": {"intercity_routing_mode": "driving"}}, "intercity_transport"),
    ({"travel_style": "luxury"}, "food"), ({"interests": ["Theme parks"]}, "activities"),
    ({"constraints": {"must_visit_attraction_ids": ["required-id"]}}, "activities")])
def test_references_are_not_applied_outside_supported_scope(changes, category):
    budget = run_budget(req(**changes)).output
    assert next(item for item in budget.line_items if item.category == category).amount is None
    assert budget.estimated_total is None


def test_expired_and_ambiguous_reference_tables_fail_closed():
    data = json.loads(Settings(_env_file=None).COST_REFERENCE_FILE.read_text(encoding="utf-8"))
    for row in data["rates"]:
        row["valid_until"] = "2026-10-10"
        row["observed_on"] = "2026-10-01"
    assert run_budget(data=data).output.estimated_total is None
    data = json.loads(Settings(_env_file=None).COST_REFERENCE_FILE.read_text(encoding="utf-8"))
    data["rates"].append(data["rates"][1].copy())
    budget = run_budget(data=data).output
    assert next(item for item in budget.line_items if item.category == "accommodation").amount is None


def test_configured_tables_support_another_region_without_application_code_changes():
    template = json.loads(Settings(_env_file=None).COST_REFERENCE_FILE.read_text(encoding="utf-8"))["rates"][1]
    rows = []
    for category in ("accommodation", "intercity_transport", "local_transport", "food", "activities"):
        row = template | {"id": "test-only-" + category, "category": category, "country_code": "fr", "currency": "EUR",
            "minimum": "1.005", "typical": "2.005", "maximum": "3.005", "styles": ["balanced"],
            "unit": "room_night" if category == "accommodation" else "person_trip", "occupancy": 2 if category == "accommodation" else None,
            "source_name": "Isolated test reference; not production prices", "source_url": "https://example.org/test-only"}
        if category == "intercity_transport":
            row |= {"origins": ["Delhi"], "destination_cities": ["Paris"]}
        rows.append(row)
    budget = run_budget(req(origin="Delhi", destination="Paris", currency="EUR"), research={"destination_name": "Paris", "country_code": "fr"},
        data={"version": 1, "style_aliases": {"balanced": "balanced"}, "rates": rows}).output
    assert budget.pricing_status == "COMPLETE_ESTIMATE"
    assert budget.estimated_total == Decimal("26.47")  # Each category is rounded before reserve, with Decimal half-up.


@pytest.mark.parametrize("payload", [
    {"base": "JPY", "quote": "INR", "rate": "0.625123456789", "date": "2026-10-09"},
    {"base": "JPY", "quote": "INR", "rate": "0", "date": "2026-10-09"},
    {"base": "USD", "quote": "INR", "rate": "0.6", "date": "2026-10-09"},
    {"base": "JPY", "quote": "INR", "rate": "0.6", "date": "2026-01-01"},
])
def test_reference_fx_validates_pair_positive_rate_and_age(payload):
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
        tool = ReferenceFXTool(Settings(_env_file=None), client=client, cache=FakeCache())
        if payload["rate"] == "0.625123456789":
            rate, dated, _ = await tool.conversion("JPY", "INR")
            assert rate == Decimal("0.625123456789") and dated == date(2026, 10, 9)
        else:
            with pytest.raises(TravelAPIError):
                await tool.conversion("JPY", "INR")
        await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["network", "timeout", "invalid_schema"])
def test_ecb_public_feed_fallback_uses_dated_decimal_cross_rates(failure):
    async def run():
        calls = []
        def handler(request):
            calls.append(request.url.host)
            if request.url.host == "api.frankfurter.dev":
                if failure == "network":
                    raise httpx.ConnectError("PRIVATE", request=request)
                if failure == "timeout":
                    raise httpx.ReadTimeout("PRIVATE", request=request)
                return httpx.Response(200, json={})
            assert str(request.url) == ECBReferenceFXTool.url
            return httpx.Response(200, text='<Envelope><Cube><Cube time="2026-10-09"><Cube currency="JPY" rate="177.34"/><Cube currency="INR" rate="99.20"/></Cube></Cube></Envelope>')
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        tool = ReferenceFXTool(Settings(_env_file=None, EXTERNAL_API_MAX_RETRIES=0), client=client, cache=FakeCache())
        rate, dated, url = await tool.conversion("JPY", "INR")
        assert rate == Decimal("99.20") / Decimal("177.34")
        assert dated == date(2026, 10, 9) and url == ECBReferenceFXTool.url
        assert calls == ["api.frankfurter.dev", "www.ecb.europa.eu"]
        await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("body", ['<Envelope><Cube time="2026-01-01"><Cube currency="JPY" rate="177"/><Cube currency="INR" rate="99"/></Cube></Envelope>',
    '<Envelope><Cube time="2026-10-09"><Cube currency="JPY" rate="0"/></Cube></Envelope>',
    '<!DOCTYPE x [<!ENTITY secret "PRIVATE">]><Envelope/>', 'not XML'])
def test_ecb_rejects_stale_invalid_and_entity_feeds(body):
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))
        tool = ECBReferenceFXTool(Settings(_env_file=None), client=client, cache=FakeCache())
        with pytest.raises(TravelAPIError):
            await tool.conversion("JPY", "INR")
        await client.aclose()
    asyncio.run(run())


def test_frankfurter_numeric_json_preserves_precision_without_a_float_round_trip():
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            text='{"base":"JPY","quote":"INR","date":"2026-10-09","rate":0.625123456789123456789}')))
        tool = ReferenceFXTool(Settings(_env_file=None), client=client, cache=FakeCache())
        rate, _, _ = await tool.conversion("JPY", "INR")
        assert rate == Decimal("0.625123456789123456789")
        await client.aclose()
    asyncio.run(run())


class ResearchProvider(IsolatedProvider):
    async def geocode_destination(self, name, **kwargs):
        result = await super().geocode_destination(name, **kwargs)
        return result.model_copy(update={"country_code": "jp" if name == "Tokyo" else "in"})


async def all_agents(request):
    settings, provider = Settings(_env_file=None), ResearchProvider()
    return await plan_trip(request, agents=TravelAgentSet(
        requirements=RequirementsAgent(settings=settings),
        destination_research=DestinationResearchAgent(settings=settings, geoapify=provider, weather=provider, tavily=provider, routing=provider),
        accommodation_transport=AccommodationTransportAgent(settings=settings, geoapify=provider, routing=provider),
        budget=BudgetAgent(settings=settings, cost_estimation_service=CostEstimationService(settings, fx=FXFixture())),
        itinerary=ItineraryAgent(settings=settings, routing=provider), optimization_review=OptimizationAgent(settings=settings)))


@pytest.mark.parametrize("trip_request,expected", [(req(), "COMPLETED"), (req(total_budget="250000"), "REVISION_REQUIRED"),
    (req(origin="Unsupported origin"), "INSUFFICIENT_DATA"),
    (req(constraints={"accessibility": "step-free access required"}), "REVISION_REQUIRED")])
def test_complete_partial_overrun_and_critical_failures_through_six_agents(trip_request, expected):
    state = asyncio.run(all_agents(trip_request))
    assert state["execution_status"] == expected
    assert state["optimization_review"].approved == (expected == "COMPLETED")
    assert state["budget_breakdown"].booking_readiness == "unverified"


@pytest.mark.parametrize("origin", ["Hyderabad", "Unsupported origin"])
def test_estimation_metadata_survives_existing_orm_result_persistence(origin):
    request = req(origin=origin)
    state = asyncio.run(all_agents(request))
    captured, session = [], MagicMock()
    session.__enter__.return_value = session
    def capture(record):
        record.id = uuid4()
        captured.append(record)
    session.add.side_effect = capture
    service = TripPlanningService(session_factory=lambda: session, task_status=MagicMock(), settings=Settings(_env_file=None))
    trip_id = uuid4()
    service._create_trip(trip_id, request)
    trip = next(item for item in captured if isinstance(item, Trip))
    trip.id, trip.preference = trip_id, next(item for item in captured if isinstance(item, TravelPreference))
    trip.created_at = trip.updated_at = datetime.now(timezone.utc)
    session.execute.return_value.scalar_one_or_none.return_value = trip
    assert service._persist_result(trip_id, state)
    trip.agent_runs = [item for item in captured if isinstance(item, AgentRun)]
    trip.itinerary_days = [item for item in captured if isinstance(item, ItineraryDay)]
    for day in trip.itinerary_days:
        day.activities = [item for item in captured if isinstance(item, ItineraryActivity) and item.itinerary_day_id == day.id]
    saved = service._get_trip(trip_id)
    assert saved.budget["minimum_estimated_total"] == (str(state["budget_breakdown"].minimum_estimated_total) if state["budget_breakdown"].minimum_estimated_total is not None else None)
    assert saved.budget["line_items"][0]["currency_conversion"]["rate"] == "0.60"
    assert saved.budget["booking_readiness"] == "unverified"
    assert saved.days and saved.itinerary["days"]
    assert saved.accommodation_transport == state["accommodation_transport_plan"].model_dump(mode="json")
    assert saved.accommodation_transport["accommodation"]["room_count"] == 1
    if saved.total_estimated_cost is None:
        assert saved.status == "insufficient_data"
        assert saved.budget["pricing_status"] == "INCOMPLETE"
        assert all(day.estimated_daily_cost is None for day in saved.days)
    else:
        assert sum(day.estimated_daily_cost for day in saved.days) == saved.total_estimated_cost


def test_spain_country_requires_user_city_choice_even_with_one_small_town_candidate():
    provider = DynamicProvider({"Spain": location("Spain", "Spain", 40, -3, "country"),
        "Small fixture town": location("Small fixture town", "Spain", 41, -4)}, {"Spain": ["Small fixture town"]})
    from test_cost_preferences_flow import run_real_agents
    state = asyncio.run(run_real_agents(dynamic_request("Origin", "Spain"), provider))
    assert state["destination_research"].resolution_status == "clarification_required"
    assert not provider.place_calls and "trip_itinerary" not in state


def test_country_with_an_explicit_supported_city_can_plan_without_an_arbitrary_base():
    provider = DynamicProvider({"Spain": location("Spain", "Spain", 40, -3, "country"),
        "Madrid": location("Madrid", "Spain", 40.4, -3.7)})
    from test_cost_preferences_flow import run_real_agents
    state = asyncio.run(run_real_agents(dynamic_request("Origin", "Spain", constraints={"destination_cities": ["Madrid"]}), provider))
    assert state["execution_status"] == "COMPLETED"
    assert "Madrid" in state["destination_research"].destination_name
    assert state["normalized_requirements"].destination == "Spain"


def flight_handler(request):
    if "oauth2" in request.url.path:
        assert b"grant_type=client_credentials" in request.content
        return httpx.Response(200, json={"access_token": "test-only-token"})
    if "locations" in request.url.path:
        city = request.url.params["keyword"]
        code = "AAA" if city == "Origin City" else "BBB"
        return httpx.Response(200, json={"data": [{"subType": "CITY", "name": city.upper(), "iataCode": code}]})
    assert request.url.params["adults"] == "2"
    assert request.url.params["currencyCode"] == "INR"
    assert request.url.params["departureDate"] == "2026-10-15"
    return httpx.Response(200, json={"data": [{"travelerPricings": [{}, {}], "price": {"currency": "INR", "grandTotal": "12345.67"},
        "itineraries": [{"segments": [{"departure": {"iataCode": "AAA", "at": "2026-10-15T09:00:00"}, "arrival": {"iataCode": "BBB"}}]},
                        {"segments": [{"departure": {"iataCode": "BBB", "at": "2026-10-19T09:00:00"}, "arrival": {"iataCode": "AAA"}}]}]}]})


@pytest.mark.parametrize("environment", ["test", "production"])
def test_authorized_provider_search_is_dynamic_and_sandbox_never_verifies_prices(environment):
    async def run():
        settings = Settings(_env_file=None, AMADEUS_API_KEY="test-only", AMADEUS_API_SECRET="test-only", AMADEUS_ENVIRONMENT=environment)
        client = httpx.AsyncClient(transport=httpx.MockTransport(flight_handler))
        tool = AmadeusPricingTool(settings, client=client, cache=FakeCache())
        item = await tool.flight_cost(req(origin="Origin City", destination="Destination City"), "Destination City")
        if environment == "production":
            assert item.amount == Decimal("12345.67") and item.evidence_kind == "verified_price"
            assert item.price_sources[0].name.startswith("Amadeus production")
        else:
            assert item is None
        await client.aclose()
    asyncio.run(run())


def test_configured_provider_quote_precedes_reference_fares_and_failure_falls_back():
    async def run():
        settings = Settings(_env_file=None, AMADEUS_API_KEY="test-only", AMADEUS_API_SECRET="test-only", AMADEUS_ENVIRONMENT="production")
        class PricingFixture:
            fail = False
            calls = 0
            async def flight_cost(self, requirements, destination, *, country_code=None):
                self.calls += 1
                assert destination == "Tokyo, Japan" and requirements.origin == "Hyderabad"
                assert country_code == "jp"
                if self.fail:
                    raise TravelAPIError("amadeus", "PRIVATE KEY", category="authentication")
                return BudgetLineItem(category="intercity_transport", description="Test provider quote", amount="12345.67", currency="INR",
                    classification="verified", source="verified_provider", source_references=[{
                        "provider": "amadeus", "title": "Test quote only", "url": "https://example.org/test-only"}])
        pricing = PricingFixture()
        service = CostEstimationService(settings, fx=FXFixture(), pricing=pricing)
        agent = BudgetAgent(settings=settings, cost_estimation_service=service)
        result = await agent.run(agent_context())
        fare = next(item for item in result.output.line_items if item.category == "intercity_transport")
        assert fare.amount == Decimal("12345.67") and fare.evidence_kind == "verified_price"
        pricing.fail = True
        result = await agent.run(agent_context())
        fare = next(item for item in result.output.line_items if item.category == "intercity_transport")
        assert fare.amount == Decimal("257064.00") and fare.evidence_kind == "planning_estimate"
        assert "PRIVATE KEY" not in str(result)
        assert pricing.calls == 2
    asyncio.run(run())


def test_amadeus_accepts_provider_airport_membership_and_rejects_unrelated_routes():
    async def run():
        wrong = False
        def handler(request):
            response = flight_handler(request)
            payload = response.json()
            if "locations" in request.url.path and request.url.params["keyword"] == "Destination City":
                payload["data"].append({"subType": "AIRPORT", "iataCode": "CCC", "address": {"cityCode": "BBB"}})
            if "flight-offers" in request.url.path:
                airport = "ZZZ" if wrong else "CCC"
                payload["data"][0]["itineraries"][0]["segments"][0]["arrival"]["iataCode"] = airport
                payload["data"][0]["itineraries"][1]["segments"][0]["departure"]["iataCode"] = airport
            return httpx.Response(200, json=payload)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        tool = AmadeusPricingTool(Settings(_env_file=None, AMADEUS_API_KEY="test-only", AMADEUS_API_SECRET="test-only", AMADEUS_ENVIRONMENT="production"),
                                  client=client, cache=FakeCache())
        request = req(origin="Origin City", destination="Destination City")
        assert (await tool.flight_cost(request, "Destination City")).amount == Decimal("12345.67")
        wrong = True
        assert await tool.flight_cost(request, "Destination City") is None
        await client.aclose()
    asyncio.run(run())


def test_reference_line_cannot_promote_itself_to_live_evidence():
    item = BudgetLineItem(category="food", description="Planning allowance", currency="INR", amount="100",
        source="reference_data", classification="estimated", evidence_kind="verified_price")
    assert item.evidence_kind == "planning_estimate"


def test_unknown_line_cannot_contain_fake_zero_range():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        BudgetLineItem(category="food", description="Unknown food", currency="INR", amount=None,
            source="unknown", classification="unknown", minimum_amount="0", maximum_amount="0")


def test_provider_city_in_a_different_country_is_not_a_valid_flight_destination():
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(flight_handler))
        tool = AmadeusPricingTool(Settings(_env_file=None, AMADEUS_API_KEY="test-only", AMADEUS_API_SECRET="test-only", AMADEUS_ENVIRONMENT="production"),
                                  client=client, cache=FakeCache())
        assert await tool.flight_cost(req(origin="Origin City", destination="Destination City"), "Destination City", country_code="fr") is None
        await client.aclose()
    asyncio.run(run())
