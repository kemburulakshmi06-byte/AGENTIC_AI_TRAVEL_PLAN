"""All rates and Gemini output are fictional fixtures; no live pricing calls."""
import asyncio
import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.schemas.agent_schemas import NormalizedTravelRequirements
from app.schemas.ai_cost_estimation import (
    AICostEstimationRequest, AICostEstimationResponse, AICostEstimationResult, AIPlanningCostEstimate,
    CATEGORY_UNITS,
)
from app.schemas.budget import BudgetLineItem
from app.schemas.flight_pricing import CachedFlightReference, FlightPricingScope, PassengerPricingBasis
from app.services.ai_cost_estimation_service import AICostEstimationService
from app.services.cost_estimation_service import CostEstimationService
from app.services.gemini_service import GeminiAPIError, GeminiInvalidJSONError, GeminiTimeoutError
from test_gemini_service import install_fake_client

CATEGORIES = list(CATEGORY_UNITS)
RATES = {"intercity_transport": "1000", "accommodation": "250", "food": "75",
         "local_transport": "40", "activities": "150"}


def settings(**overrides):
    return Settings(_env_file=None, **({"AI_COST_ESTIMATION_ENABLED": True,
        "GEMINI_API_KEY": SecretStr("fictional-test-key"), "GEMINI_MODEL": "fictional-test-model",
        "GEMINI_MAX_RETRIES": 0} | overrides))


def request(**overrides):
    return AICostEstimationRequest.model_validate({
        "origin": "Sample Origin", "destination": "Sample Destination",
        "start_date": "2027-05-01", "end_date": "2027-05-05",
        "travelers_count": 3, "room_count": 2, "travel_style": "balanced",
        "transport_mode": "flight", "currency": "AED", "missing_categories": CATEGORIES,
    } | overrides)


def response(req=None, categories=None, ranges=False):
    req = req or request()
    rows = []
    for category in req.missing_categories if categories is None else categories:
        row = {"category": category, "unit": CATEGORY_UNITS[category], "currency": req.currency,
               "typical": RATES[category]}
        if ranges:
            row.update(minimum=str(Decimal(RATES[category]) * Decimal("0.9")),
                       maximum=str(Decimal(RATES[category]) * Decimal("1.1")))
        rows.append(row)
    return {"scope": req.model_dump(mode="json", exclude={"missing_categories"}), "estimates": rows}


class FakeGemini:
    def __init__(self, payload=None, error=None, delay=0):
        self.payload = response() if payload is None else payload
        self.error = error
        self.delay = delay
        self.calls = []
        self.cancelled = False

    async def generate_structured(self, prompt, response_model, **kwargs):
        self.calls.append({"prompt": json.loads(prompt), "response_model": response_model, **kwargs})
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            return response_model.model_validate(self.payload)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def estimate(payload=None, *, req=None, config=None, existing=()):
    gemini = FakeGemini(payload)
    result = asyncio.run(AICostEstimationService(config or settings(), gemini_service=gemini).estimate_missing(
        req or request(), existing_costs=existing))
    return result, gemini


def test_valid_all_categories_calculate_decimal_amounts_and_non_live_provenance():
    req = request()
    result, gemini = estimate(response(req, ranges=True), req=req)
    assert {category: value.line_item.amount for category, value in result.estimates.items()} == {
        "intercity_transport": Decimal("3000.00"), "accommodation": Decimal("2000.00"),
        "food": Decimal("1125.00"), "local_transport": Decimal("200.00"), "activities": Decimal("450.00")}
    assert not result.unresolved_categories and not result.warnings
    assert gemini.calls[0]["response_model"] is AICostEstimationResponse
    for value in result.estimates.values():
        line, provenance = value.line_item, value.provenance
        assert line.classification == "estimated" and line.evidence_kind == "planning_estimate"
        assert line.source == "configured_assumption" and line.confidence == "low"
        assert not line.price_sources and not line.source_references and line.flight_reference is None
        assert line.currency_conversion is None
        assert provenance.method == "gemini_planning_assumption" and provenance.confidence == "low"
        assert provenance.live_availability_checked is False
        assert provenance.generated_at.utcoffset() is not None and provenance.generated_at.date() == datetime.now(UTC).date()
        assert line.minimum_amount == line.amount * Decimal("0.9")
        assert line.maximum_amount == line.amount * Decimal("1.1")
    assert AICostEstimationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("category", CATEGORIES)
def test_wrong_units_are_rejected(category):
    req = request(missing_categories=[category])
    payload = response(req)
    payload["estimates"][0]["unit"] = "party_day" if category != "local_transport" else "traveler_day"
    result, _ = estimate(payload, req=req)
    assert not result.estimates and result.unresolved_categories == [category]


def test_wrong_currency_remains_unresolved():
    payload = response()
    payload["estimates"][0]["currency"] = "USD"
    result, _ = estimate(payload)
    assert set(result.estimates) == set(CATEGORIES[1:])
    assert result.unresolved_categories == ["intercity_transport"]
    assert any("currency" in warning for warning in result.warnings)


@pytest.mark.parametrize("value", ["-1", "0", "NaN", "Infinity", "-Infinity", True, "1e25", "75.12345"])
def test_invalid_numeric_rates_fail_closed(value):
    payload = response()
    payload["estimates"][0]["typical"] = value
    result, _ = estimate(payload)
    assert not result.estimates and result.unresolved_categories == CATEGORIES


@pytest.mark.parametrize("field,value", [("typical", "75001"), ("minimum", "1"), ("maximum", "75001")])
def test_configured_bounds_reject_excessive_or_implausibly_low_unit_rates(field, value):
    payload = response(ranges=True)
    payload["estimates"][0][field] = value
    if field == "typical":
        payload["estimates"][0].update(minimum="75000", maximum="80000")
    result, _ = estimate(payload)
    assert "intercity_transport" not in result.estimates
    assert result.unresolved_categories == ["intercity_transport"]


@pytest.mark.parametrize("minimum,maximum", [("1001", "1100"), ("900", "999"), (None, "1100"), ("900", None), ("NaN", "1100")])
def test_invalid_or_incomplete_scenario_ranges_are_rejected(minimum, maximum):
    payload = response()
    payload["estimates"][0].update(minimum=minimum, maximum=maximum)
    result, _ = estimate(payload)
    assert not result.estimates


def test_duplicate_response_categories_reject_entire_response():
    payload = response(request(missing_categories=["food"]))
    payload["estimates"].append(copy.deepcopy(payload["estimates"][0]))
    result, _ = estimate(payload, req=request(missing_categories=["food"]))
    assert result.unresolved_categories == ["food"] and not result.estimates


def test_unsolicited_categories_reject_entire_response():
    req = request(missing_categories=["food"])
    result, gemini = estimate(response(categories=["food", "activities"]), req=req)
    assert not result.estimates and result.unresolved_categories == ["food"]
    assert gemini.calls[0]["prompt"]["missing_categories"] == ["food"]


@pytest.mark.parametrize("field,value", [
    ("provider", "fabricated-provider"), ("source_url", "https://example.org/fake-price"),
    ("source_references", [{"title": "Invented", "url": "https://example.org/fake"}]),
    ("price_sources", [{"name": "Invented", "observed_on": "2030-01-01"}]),
    ("observed_on", "2030-01-01"), ("retrieved_at", "2030-01-01T00:00:00Z"),
    ("classification", "verified"), ("evidence_kind", "verified_price"),
    ("live_availability_checked", True), ("booking_readiness", "ready"),
    ("assumptions", ["Confirmed available at the provider's live price."]),
    ("total", "100"), ("estimated_savings", "100"),
])
def test_fabricated_metadata_and_claims_are_not_accepted_output_fields(field, value):
    payload = response()
    payload["estimates"][0][field] = value
    result, _ = estimate(payload)
    assert not result.estimates and result.unresolved_categories == CATEGORIES
    assert "fabricated-provider" not in str(result)


@pytest.mark.parametrize("field,value", [
    ("origin", "Other Origin"), ("destination", "Other Destination"),
    ("start_date", "2027-05-02"), ("end_date", "2027-05-06"),
    ("travelers_count", 4), ("room_count", 3), ("travel_style", "luxury"),
    ("transport_mode", "bus"), ("currency", "USD"),
])
def test_scope_mismatch_rejects_response(field, value):
    payload = response()
    payload["scope"][field] = value
    result, _ = estimate(payload)
    assert not result.estimates and result.unresolved_categories == CATEGORIES


def test_budget_independence_whitelists_requirements_and_never_sends_existing_prices():
    async def run():
        requirements = NormalizedTravelRequirements.model_validate({**request().model_dump(exclude={"missing_categories", "room_count", "transport_mode"}),
            "total_budget": "10000", "constraints": {"remaining_budget": "500", "overrun": "100", "affordability_target": "cheap"}})
        gemini = FakeGemini()
        service = AICostEstimationService(settings(), gemini_service=gemini)
        outputs = []
        for budget in ("10000", "100000"):
            scope = AICostEstimationRequest.from_requirements(requirements.model_copy(update={"total_budget": Decimal(budget)}),
                room_count=2, missing_categories=CATEGORIES)
            outputs.append(await service.estimate_missing(scope))
        assert gemini.calls[0]["prompt"] == gemini.calls[1]["prompt"]
        assert {k:v.line_item.amount for k,v in outputs[0].estimates.items()} == {k:v.line_item.amount for k,v in outputs[1].estimates.items()}
        text = json.dumps(gemini.calls[0]["prompt"])
        assert all(word not in text for word in ("total_budget", "remaining_budget", "overrun", "affordability_target", "constraints"))
    asyncio.run(run())


@pytest.mark.parametrize("error", [GeminiAPIError("PRIVATE secret provider body"), GeminiInvalidJSONError("PRIVATE invalid response"), GeminiTimeoutError("PRIVATE timeout")])
def test_provider_failure_is_sanitized_and_keeps_unresolved_categories(error, caplog):
    gemini = FakeGemini(error=error)
    result = asyncio.run(AICostEstimationService(settings(), gemini_service=gemini).estimate_missing(request()))
    assert not result.estimates and result.unresolved_categories == CATEGORIES
    assert result.warnings
    assert "PRIVATE" not in str(result) and "PRIVATE" not in caplog.text


def test_bounded_deadline_cancels_gemini_and_preserves_unresolved_costs():
    async def run():
        gemini = FakeGemini(delay=1)
        result = await AICostEstimationService(settings(AI_COST_ESTIMATION_TIMEOUT_SECONDS=0.01), gemini_service=gemini).estimate_missing(request())
        assert gemini.cancelled
        assert result.unresolved_categories == CATEGORIES and not result.estimates
        assert any("deadline" in warning for warning in result.warnings)
    asyncio.run(run())


@pytest.mark.parametrize("agent,remaining", [(0.03, None), (60, 0.03)])
def test_deadline_is_capped_to_one_third_of_agent_and_remaining_time(agent, remaining):
    async def run():
        gemini = FakeGemini(delay=1)
        result = await AICostEstimationService(settings(TRIP_AGENT_TIMEOUT_SECONDS=agent), gemini_service=gemini).estimate_missing(
            request(), remaining_agent_seconds=remaining)
        assert gemini.cancelled and not result.estimates
    asyncio.run(run())


def test_caller_cancellation_is_not_swallowed():
    async def run():
        gemini = FakeGemini(delay=1)
        task = asyncio.create_task(AICostEstimationService(settings(), gemini_service=gemini).estimate_missing(request()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_partial_response_retains_valid_categories_and_reports_omissions():
    result, _ = estimate(response(categories=["food", "activities"]))
    assert set(result.estimates) == {"food", "activities"}
    assert result.unresolved_categories == ["intercity_transport", "accommodation", "local_transport"]
    assert any("partial" in warning for warning in result.warnings)


def test_empty_response_is_not_complete_or_free():
    result, _ = estimate(response(categories=[]))
    assert not result.estimates and result.unresolved_categories == CATEGORIES
    assert any("omitted" in warning for warning in result.warnings)


@pytest.mark.parametrize("source,classification", [("verified_provider", "verified"), ("reference_data", "estimated"), ("user_assumption", "estimated")])
@pytest.mark.parametrize("category", CATEGORIES)
def test_existing_prices_and_allowances_are_never_replaced_or_sent_to_gemini(source, classification, category):
    existing = BudgetLineItem(category=category, description="Fictional existing cost", amount="100", currency="AED",
        classification=classification, source=source)
    original = existing.model_dump_json()
    other = "activities" if category != "activities" else "food"
    req = request(missing_categories=[category, other])
    result, gemini = estimate(response(req, categories=[other]), req=req, existing=[existing])
    assert set(result.estimates) == {other} and not result.unresolved_categories
    assert gemini.calls[0]["prompt"]["missing_categories"] == [other]
    assert "existing cost" not in json.dumps(gemini.calls[0]["prompt"])
    assert existing.model_dump_json() == original
    # A provider that insists on returning a protected category is rejected too.
    result, _ = estimate(response(req), req=req, existing=[existing])
    assert not result.estimates and result.unresolved_categories == [other]


def test_explicit_zero_allowance_and_empty_request_do_not_call_gemini():
    existing = BudgetLineItem(category="food", description="Explicit zero", amount="0", currency="AED",
        classification="estimated", source="user_assumption")
    for req, costs in [(request(missing_categories=["food"]), [existing]), (request(missing_categories=[]), [])]:
        result, gemini = estimate(req=req, existing=costs)
        assert not gemini.calls and not result.estimates and not result.unresolved_categories


def test_disabled_feature_and_unsupported_currency_make_no_calls():
    for config, req in [(settings(AI_COST_ESTIMATION_ENABLED=False), request()), (settings(), request(currency="JPY"))]:
        result, gemini = estimate(config=config, req=req)
        assert not gemini.calls and not result.estimates and result.unresolved_categories == CATEGORIES
        assert result.warnings


def test_configurable_currency_bounds_are_not_fx_conversion():
    config = settings(AI_COST_ESTIMATION_RATE_BOUNDS={"JPY": {"food": {"minimum": "10", "maximum": "50000"}}})
    req = request(currency="JPY", missing_categories=["food"])
    result, _ = estimate(response(req), req=req, config=config)
    assert result.estimates["food"].line_item.amount == Decimal("1125.00")
    assert result.estimates["food"].line_item.currency_conversion is None


@pytest.mark.parametrize("change", [{"travelers_count": True}, {"travelers_count": 0}, {"room_count": 0},
    {"end_date": "2027-04-30"}, {"end_date": "2028-05-01"}, {"missing_categories": ["food", "food"]},
    {"missing_categories": ["contingency"]}, {"total_budget": "10000"}])
def test_invalid_requests_and_budget_fields_are_rejected_by_schema(change):
    with pytest.raises(ValidationError):
        request(**change)


def test_zero_nights_cannot_request_ai_accommodation():
    with pytest.raises(ValidationError):
        request(end_date="2027-05-01")
    req = request(end_date="2027-05-01", missing_categories=["food"])
    result, _ = estimate(response(req), req=req)
    assert result.estimates["food"].line_item.amount == Decimal("225.00")


def test_decimal_rounding_is_deterministic():
    req = request(missing_categories=["food"])
    payload = response(req)
    payload["estimates"][0]["typical"] = "75.001"
    result, _ = estimate(payload, req=req)
    assert result.estimates["food"].line_item.amount == Decimal("1125.02")


@pytest.mark.parametrize("body", ["not JSON", '{"scope":{},"estimates":[]}', None])
def test_existing_gemini_json_validation_and_client_closure_are_reused(monkeypatch, body):
    client, models, _ = install_fake_client(monkeypatch, [SimpleNamespace(text=body)])
    result = asyncio.run(AICostEstimationService(settings()).estimate_missing(request()))
    assert not result.estimates and result.unresolved_categories == CATEGORIES
    assert client.aio.closed
    assert models.calls[0]["config"].response_json_schema == AICostEstimationResponse.model_json_schema()


def test_existing_gemini_client_handles_valid_output_without_retries(monkeypatch):
    client, models, init_calls = install_fake_client(monkeypatch, [SimpleNamespace(text=json.dumps(response()))])
    result = asyncio.run(AICostEstimationService(settings()).estimate_missing(request()))
    assert set(result.estimates) == set(CATEGORIES)
    assert len(models.calls) == 1 and client.aio.closed
    assert init_calls[0]["http_options"].timeout <= 10000


def test_unconfigured_gemini_returns_warning_without_initializing_client(monkeypatch):
    _, _, init_calls = install_fake_client(monkeypatch, [])
    result = asyncio.run(AICostEstimationService(settings(GEMINI_API_KEY=None)).estimate_missing(request()))
    assert not init_calls and not result.estimates
    assert any("configured" in warning for warning in result.warnings)


def test_cost_service_explicit_adapter_does_not_invoke_existing_providers():
    class ForbiddenProvider:
        async def flight_cost(self, *args, **kwargs):
            raise AssertionError("Existing providers must not be invoked by the standalone AI adapter")
    service = CostEstimationService(settings(), pricing=ForbiddenProvider(), travelpayouts_pricing=ForbiddenProvider())
    result = asyncio.run(service.estimate_ai_assumptions(request(), gemini_service=FakeGemini()))
    assert set(result.estimates) == set(CATEGORIES)


@pytest.mark.parametrize("change", [{"amount": "1"}, {"confidence": "high"},
    {"classification": "verified"}, {"source": "verified_provider"},
    {"price_sources": [{"name": "Invented", "observed_on": "2030-01-01", "currency": "AED"}]}])
def test_output_wrapper_rejects_amount_tampering_and_live_evidence_promotion(change):
    result, _ = estimate()
    raw = result.estimates["food"].model_dump(mode="json")
    raw["line_item"].update(change)
    with pytest.raises(ValidationError):
        AIPlanningCostEstimate.model_validate(raw)


def test_invalid_constructed_request_returns_warning_without_gemini():
    req = request().model_copy(update={"room_count": 0})
    result, gemini = estimate(req=req)
    assert not gemini.calls and not result.estimates and result.unresolved_categories == CATEGORIES
    assert any("invalid" in warning for warning in result.warnings)


@pytest.mark.parametrize("remaining", [0, -1, float("nan"), float("inf")])
def test_invalid_or_exhausted_remaining_time_makes_no_call(remaining):
    gemini = FakeGemini()
    result = asyncio.run(AICostEstimationService(settings(), gemini_service=gemini).estimate_missing(
        request(), remaining_agent_seconds=remaining))
    assert not gemini.calls and result.unresolved_categories == CATEGORIES


@pytest.mark.parametrize("bounds", [
    {"aed": {"food": {"minimum": "1", "maximum": "100"}}},
    {"AED": {"food": {"minimum": "100", "maximum": "1"}}},
    {"AED": {"food": {"minimum": "NaN", "maximum": "100"}}},
    {"AED": {"other": {"minimum": "1", "maximum": "100"}}},
])
def test_invalid_bound_configuration_is_rejected(bounds):
    with pytest.raises(ValidationError):
        settings(AI_COST_ESTIMATION_RATE_BOUNDS=bounds)


@pytest.mark.parametrize("applicable", [True, False])
def test_cached_reference_is_protected_only_when_applicable_without_reusing_other_dates(applicable):
    req = request(origin="Hyderabad", destination="Dubai", start_date="2030-11-10", end_date="2030-11-14",
                  travelers_count=2, room_count=1, missing_categories=["intercity_transport"])
    reference = CachedFlightReference(
        origin_iata="HYD", destination_iata="DXB", currency="AED", price="500",
        departure_date=req.start_date if applicable else "2030-10-20", return_date=req.end_date,
        retrieved_at=datetime.now(UTC), passenger_basis=PassengerPricingBasis(kind="per_adult", priced_passengers=1, evidence="Fictional fixture"),
        requested_scope=FlightPricingScope(origin_iata="HYD", destination_iata="DXB", origin_location=req.origin,
            destination_location=req.destination, departure_date=req.start_date, return_date=req.end_date,
            currency=req.currency, travelers=req.travelers_count))
    existing = BudgetLineItem(category="intercity_transport", description="Fictional cached fare", amount="1000",
        currency="AED", classification="estimated", source="reference_data", flight_reference=reference)
    original = existing.model_dump_json()
    result, gemini = estimate(response(req), req=req, existing=[existing])
    if applicable:
        assert not gemini.calls and not result.estimates and not result.unresolved_categories
    else:
        assert len(gemini.calls) == 1
        assert result.estimates["intercity_transport"].line_item.amount == Decimal("2000.00")
        assert result.estimates["intercity_transport"].line_item.flight_reference is None
        assert "500" not in json.dumps(gemini.calls[0]["prompt"])
    assert existing.model_dump_json() == original


def test_complete_reference_budget_does_not_call_ai_foundation(monkeypatch):
    from test_automatic_cost_estimation import FixedDate, req, run_budget
    monkeypatch.setattr("app.services.cost_estimation_service.date", FixedDate)
    async def forbidden(*args, **kwargs):
        raise AssertionError("An already-priced budget must not invoke AI estimation")
    monkeypatch.setattr(AICostEstimationService, "estimate_missing", forbidden)
    budget = run_budget(req(), settings=settings(GEMINI_API_KEY=None)).output
    assert budget.estimated_total == Decimal("386984.40")
    assert all(line.source in {"reference_data", "configured_assumption"} for line in budget.line_items)
