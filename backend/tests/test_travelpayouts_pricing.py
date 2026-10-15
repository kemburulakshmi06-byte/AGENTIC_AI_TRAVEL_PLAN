"""Mocked cached fares and explicit fictional basis evidence; no live quotes."""
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.budget_agent import BudgetAgent
from app.agents.optimization_agent import OptimizationAgent
from app.core.config import Settings
from app.main import create_app
from app.schemas.agent_schemas import AgentContext, NormalizedTravelRequirements, TravelRequest
from app.schemas.budget import BudgetLineItem
from app.schemas.destination_research import SourceReference
from app.schemas.flight_pricing import PassengerPricingBasis
from app.services.cost_estimation_service import CostEstimationService
from app.tools.base import TravelAPIError
from app.tools.travelpayouts import FARES_URL, TravelpayoutsPricingTool
from app.workflows.travel_graph import _budget_safety_status
from test_external_api_tools import FakeCache

TOKEN = "fixture-only-travelpayouts-token"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("Unexpected real network call"))


def settings(**changes):
    return Settings(_env_file=None, **(dict(TRAVELPAYOUTS_ENABLED=True, TRAVELPAYOUTS_API_TOKEN=TOKEN,
        AMADEUS_API_KEY=None, AMADEUS_API_SECRET=None, GEMINI_API_KEY=None, EXTERNAL_API_MAX_RETRIES=0) | changes))


def request(**changes):
    return TravelRequest(**(dict(origin="Hyderabad", destination="Dubai", start_date="2026-11-10",
        end_date="2026-11-14", travelers_count=2, currency="AED", total_budget="8000", travel_style="balanced") | changes))


def fare(**changes):
    return dict(origin="HYD", destination="DXB", departure_at="2026-11-10T09:00:00+05:30",
                return_at="2026-11-14T10:00:00+04:00", price=500, **changes)


def basis(kind="per_adult", passengers=1):
    return PassengerPricingBasis(kind=kind, priced_passengers=passengers,
        evidence="Explicit fictional provider-contract evidence for this test; not a real API guarantee.")


def build_tool(rows=None, *, currency="aed", pricing_basis=True, config=None, status=200,
               body=None, failure=None, places=None):
    calls = []
    def handler(req):
        calls.append(req)
        if "places2" in req.url.path:
            if places is not None:
                return httpx.Response(200, json=places)
            origin = req.url.params["term"] in {"Hyderabad", "HYD"}
            return httpx.Response(200, json=[dict(type="city", name="Hyderabad" if origin else "Dubai",
                code="HYD" if origin else "DXB", country_code="IN" if origin else "AE",
                country_name="India" if origin else "United Arab Emirates")])
        assert str(req.url).startswith(FARES_URL)
        assert req.headers["X-Access-Token"] == TOKEN
        assert "token" not in req.url.params and TOKEN not in str(req.url)
        assert req.url.params["one_way"] == "false"
        assert req.url.params["departure_at"] == "2026-11-10"
        assert req.url.params["return_at"] == "2026-11-14"
        if failure:
            raise failure
        return httpx.Response(status, json=body if body is not None else {
            "success": True, "currency": currency, "data": [fare()] if rows is None else rows})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = TravelpayoutsPricingTool(settings=config or settings(), client=client, cache=FakeCache(),
        passenger_basis=basis() if pricing_basis is True else pricing_basis if isinstance(pricing_basis, PassengerPricingBasis) else None)
    return tool, client, calls


def test_applicable_cached_fare_has_provenance_and_scales_once():
    async def run():
        tool, client, calls = build_tool()
        async with client:
            item = await tool.flight_cost(request(), "Dubai, UAE", country_code="ae")
        assert item.amount == Decimal("1000")
        assert item.classification == "estimated" and item.source == "reference_data"
        assert item.evidence_kind == "planning_estimate"
        assert item.flight_reference.applicability == "applicable"
        assert item.flight_reference.live_availability_checked is False
        assert item.source_references[0].provider == "travelpayouts"
        assert len(calls) == 3
        assert TOKEN not in item.model_dump_json()
    asyncio.run(run())


def test_party_total_is_not_multiplied_again():
    async def run():
        tool, client, _ = build_tool(pricing_basis=basis("party_total", 2))
        async with client:
            assert (await tool.flight_cost(request(), "Dubai", country_code="ae")).amount == Decimal("500")
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"departure_at": "2026-10-10"}, {"return_at": "2026-10-14"},
    {"origin": "DEL"}, {"destination": "AUH"}, {"return_at": None},
    {"currency": "usd"}, {"price": "NaN"}, {"price": -1}, {"price": 0},
    {"price": "invalid"}, {"departure_at": "invalid"},
    {"expires_at": "2020-01-01T00:00:00Z"}, {"found_at": "2020-01-01T00:00:00Z"},
])
def test_unrelated_or_invalid_references_never_price_trip(change):
    async def run():
        tool, client, _ = build_tool(rows=[fare() | change])
        async with client:
            assert await tool.flight_cost(request(), "Dubai", country_code="ae") is None
    asyncio.run(run())


@pytest.mark.parametrize("currency,pricing_basis", [(None, True), ("aed", False), ("aed", basis("party_total", 1))])
def test_unknown_currency_or_passenger_basis_is_not_inferred(currency, pricing_basis):
    async def run():
        tool, client, _ = build_tool(currency=currency, pricing_basis=pricing_basis)
        async with client:
            assert await tool.flight_cost(request(), "Dubai", country_code="ae") is None
        assert len(tool.last_references) == 1
        assert tool.last_references[0].applicability != "applicable"
    asyncio.run(run())


def test_empty_exact_date_result_does_not_trigger_unfiltered_search():
    async def run():
        tool, client, calls = build_tool(rows=[])
        async with client:
            assert await tool.flight_cost(request(), "Dubai", country_code="ae") is None
        assert len([req for req in calls if "prices_for_dates" in req.url.path]) == 1
    asyncio.run(run())


@pytest.mark.parametrize("token", [None, "", "replace_with_token"])
def test_missing_token_fails_safely_before_any_request(token):
    async def run():
        tool, client, calls = build_tool(config=settings(TRAVELPAYOUTS_API_TOKEN=token))
        async with client:
            with pytest.raises(TravelAPIError) as caught:
                await tool.flight_cost(request(), "Dubai", country_code="ae")
        assert caught.value.category == "not_configured"
        assert not calls
    asyncio.run(run())


@pytest.mark.parametrize("status,category", [(401, "authentication"), (403, "authentication"),
    (429, "rate_limit"), (500, "http_error"), (400, "http_error")])
def test_http_failures_are_sanitized(status, category, caplog):
    async def run():
        tool, client, _ = build_tool(status=status, body={"error": TOKEN})
        async with client:
            with pytest.raises(TravelAPIError) as caught:
                await tool.flight_cost(request(), "Dubai", country_code="ae")
        assert caught.value.category == category
        assert TOKEN not in str(caught.value)
        assert TOKEN not in caplog.text
    asyncio.run(run())


def test_timeout_retries_are_bounded(monkeypatch):
    async def immediate_delay(*args, **kwargs):
        return None
    monkeypatch.setattr("app.tools.base.asyncio.sleep", immediate_delay)
    async def run():
        tool, client, calls = build_tool(config=settings(EXTERNAL_API_MAX_RETRIES=2),
            failure=httpx.ReadTimeout(TOKEN))
        async with client:
            with pytest.raises(TravelAPIError) as caught:
                await tool.flight_cost(request(), "Dubai", country_code="ae")
        assert caught.value.category == "timeout" and TOKEN not in str(caught.value)
        assert len([req for req in calls if "prices_for_dates" in req.url.path]) == 3
    asyncio.run(run())


@pytest.mark.parametrize("body", [{"success": False, "error": TOKEN}, {"success": True, "data": {}}, {"data": []}])
def test_malformed_response_does_not_expose_provider_body(body):
    async def run():
        tool, client, _ = build_tool(body=body)
        async with client:
            with pytest.raises(TravelAPIError) as caught:
                await tool.flight_cost(request(), "Dubai", country_code="ae")
        assert TOKEN not in str(caught.value)
    asyncio.run(run())


def estimator(config, tool, *, amadeus=None):
    return CostEstimationService(config, pricing=amadeus, travelpayouts_pricing=tool,
                                reference_data={"style_aliases": {"balanced": "balanced"}, "rates": []})


def test_valid_amadeus_quote_keeps_priority():
    class Amadeus:
        async def flight_cost(self, *args, **kwargs):
            return BudgetLineItem(category="intercity_transport", description="Fixture live quote", amount="1500",
                currency="AED", source="verified_provider", classification="verified",
                source_references=[SourceReference(provider="amadeus", title="Fixture quote", url="https://example.com/quote")])
    async def run():
        cfg = settings(AMADEUS_API_KEY="fixture", AMADEUS_API_SECRET="fixture")
        tool, client, calls = build_tool(config=cfg)
        async with client:
            result, _, _ = await estimator(cfg, tool, amadeus=Amadeus()).estimate_missing(
                request(), {"destination_name": "Dubai", "country_code": "ae"}, None, ["intercity_transport"])
        assert result["intercity_transport"].amount == Decimal("1500")
        assert result["intercity_transport"].evidence_kind == "verified_price"
        assert calls == []
    asyncio.run(run())


@pytest.mark.parametrize("with_allowance", [False, True])
def test_budget_missing_flight_or_explicit_allowance(with_allowance):
    async def run():
        req = request(constraints={"budget_assumptions": {"food": {"amount": "500"}, "activities": {"amount": "100"}}},
            cost_preferences=dict(accommodation_nightly_rate_per_room="200", room_count=1,
                                  local_transport_allowance_per_day="20",
                                  **({"intercity_round_trip_fare_per_traveler": "700"} if with_allowance else {})))
        normalized = NormalizedTravelRequirements(**req.model_dump(exclude={"user_message"}), travel_duration_days=5)
        tool, client, _ = build_tool(pricing_basis=False)
        service = estimator(settings(), tool)
        ctx = AgentContext(request=req, normalized_requirements=normalized,
                           destination_research={"destination_name": "Dubai", "country_code": "ae"})
        async with client:
            budget = (await BudgetAgent(settings=settings(), cost_estimation_service=service).run(ctx)).output
        assert len(budget.flight_references) == 1
        ctx.budget_breakdown = budget.model_dump(mode="json")
        status, _ = _budget_safety_status({"request": req, "normalized_requirements": normalized, "budget_breakdown": budget})
        reviewer = (await OptimizationAgent(settings=settings()).run(ctx)).output
        if with_allowance:
            assert budget.pricing_status == "COMPLETE_ESTIMATE" and status == "RUNNING"
            flight = next(item for item in budget.line_items if item.category == "intercity_transport")
            assert flight.source == "user_assumption" and flight.amount == Decimal("1400")
        else:
            assert budget.estimated_total is None and budget.pricing_status == "INCOMPLETE"
            assert status == reviewer.review_status == "INSUFFICIENT_DATA"
            assert any("passenger" in warning for warning in budget.warnings)
        assert TOKEN not in budget.model_dump_json()
    asyncio.run(run())


def test_scope_rechecked_by_workflow_and_reviewer():
    async def run():
        tool, client, _ = build_tool()
        async with client:
            item = await tool.flight_cost(request(), "Dubai", country_code="ae")
        from test_optimization_agent import budget, agent_context
        existing = budget()
        item.currency = "INR"
        existing.line_items.append(item)
        ctx = agent_context(budget_result=existing)
        result = (await OptimizationAgent(settings=settings()).run(ctx)).output
        assert not result.approved
        assert any(issue.code == "missing_applicable_flight_price" for issue in result.issues)
        assert _budget_safety_status({"request": ctx.request, "normalized_requirements": ctx.normalized_requirements,
                                     "budget_breakdown": existing})[0] == "INSUFFICIENT_DATA"
    asyncio.run(run())


def test_optional_configuration_and_status_redaction():
    cfg = settings(TRAVELPAYOUTS_API_TOKEN=None)
    with TestClient(create_app(cfg)) as client:
        assert client.get("/health").status_code == 200
    cfg = settings()
    with TestClient(create_app(cfg)) as client:
        assert TOKEN not in client.get("/api/v1/status").text
    assert TOKEN not in repr(cfg)


def test_ambiguous_or_wrong_country_locations_are_not_guessed():
    async def run():
        places = [dict(type="city", name="Dubai", code="DXB", country_code="AE"),
                  dict(type="city", name="Dubai", code="DWC", country_code="AE")]
        tool, client, calls = build_tool(places=places)
        async with client:
            assert await tool.resolve_iata("Dubai", country_code="ae") is None
            assert await tool.resolve_iata("Dubai", country_code="fr") is None
        assert not any("prices_for_dates" in req.url.path for req in calls)
    asyncio.run(run())


def test_cached_estimate_flows_through_budget_and_snapshot_without_live_promotion():
    async def run():
        req = request(cost_preferences=dict(accommodation_nightly_rate_per_room="200", room_count=1,
            local_transport_allowance_per_day="20", food_allowance_per_traveler_per_day="50",
            activities_allowance_per_traveler_per_trip="100"))
        normalized = NormalizedTravelRequirements(**req.model_dump(exclude={"user_message"}), travel_duration_days=5)
        ctx = AgentContext(request=req, normalized_requirements=normalized,
            destination_research={"destination_name": "Dubai", "country_code": "ae"})
        tool, client, calls = build_tool()
        agent = BudgetAgent(settings=settings(), cost_estimation_service=estimator(settings(), tool))
        async with client:
            budget = (await agent.run(ctx)).output
            assert budget.pricing_status == "COMPLETE_ESTIMATE"
            assert budget.booking_readiness == "unverified"
            ctx.budget_breakdown = budget.model_dump(mode="json")
            ctx.budget_revision = {"replacement_items": {}, "removed_other_indexes": []}
            restored = (await agent.run(ctx)).output
        assert len(calls) == 3
        line = next(item for item in restored.line_items if item.category == "intercity_transport")
        assert line.flight_reference == budget.flight_references[0]
        assert restored.estimated_total == budget.estimated_total
        assert _budget_safety_status({"request": req, "normalized_requirements": normalized, "budget_breakdown": restored})[0] == "RUNNING"
        ctx.budget_breakdown = restored.model_dump(mode="json")
        line.flight_reference.return_date = req.end_date - timedelta(days=1)
        # Totals and category reconciliation are unchanged: scope alone blocks approval.
        assert _budget_safety_status({"request": req, "normalized_requirements": normalized, "budget_breakdown": restored})[0] == "INSUFFICIENT_DATA"
        ctx.budget_breakdown = restored.model_dump(mode="json")
        review = (await OptimizationAgent(settings=settings()).run(ctx)).output
        assert any(issue.code == "missing_applicable_flight_price" for issue in review.issues)
    asyncio.run(run())


def test_cached_reference_cannot_be_classified_as_verified():
    from pydantic import ValidationError
    async def run():
        tool, client, _ = build_tool()
        async with client:
            item = await tool.flight_cost(request(), "Dubai", country_code="ae")
        payload = item.model_dump(mode="json") | {"classification": "verified", "source": "verified_provider"}
        with pytest.raises(ValidationError):
            BudgetLineItem.model_validate(payload)
    asyncio.run(run())


def test_disabled_integration_does_not_make_requests():
    async def run():
        tool, client, calls = build_tool(config=settings(TRAVELPAYOUTS_ENABLED=False))
        async with client:
            assert await tool.flight_cost(request(), "Dubai", country_code="ae") is None
        assert calls == []
    asyncio.run(run())


def test_undocumented_passenger_fields_are_not_trusted():
    async def run():
        tool, client, _ = build_tool(pricing_basis=False,
            rows=[fare() | {"adults": 2, "passenger_basis": "party_total", "link": "https://example.com/?token=" + TOKEN}])
        async with client:
            assert await tool.flight_cost(request(), "Dubai", country_code="ae") is None
        assert tool.last_references[0].passenger_basis.kind == "unknown"
        assert TOKEN not in tool.last_references[0].model_dump_json()
    asyncio.run(run())


def test_rate_limit_retry_is_bounded_and_does_not_expose_token(monkeypatch):
    async def immediate(*a, **k):
        return None
    monkeypatch.setattr("app.tools.base.asyncio.sleep", immediate)
    async def run():
        tool, client, calls = build_tool(config=settings(EXTERNAL_API_MAX_RETRIES=1), status=429, body={"error": TOKEN})
        async with client:
            with pytest.raises(TravelAPIError) as caught:
                await tool.flight_cost(request(), "Dubai", country_code="ae")
        assert caught.value.category == "rate_limit"
        assert len([req for req in calls if "prices_for_dates" in req.url.path]) == 2
        assert TOKEN not in str(caught.value)
    asyncio.run(run())


def test_freshness_metadata_is_preserved_and_rechecked():
    async def run():
        now = datetime.now(UTC)
        tool, client, _ = build_tool(rows=[fare() | {
            "found_at": (now - timedelta(hours=1)).isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat()}])
        async with client:
            item = await tool.flight_cost(request(), "Dubai", country_code="ae")
        reference = item.flight_reference
        assert reference.found_at == now - timedelta(hours=1)
        assert reference.expires_at == now + timedelta(hours=1)
        assert reference.rejection_reason(request(), now=now + timedelta(hours=2)) == "stale_reference"
    asyncio.run(run())
