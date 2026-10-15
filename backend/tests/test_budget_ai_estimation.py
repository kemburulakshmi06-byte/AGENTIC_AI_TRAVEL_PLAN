"""Budget Agent integration with fictional unit rates and isolated providers."""
import asyncio
import copy
import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agents.budget_agent import BudgetAgent
from app.schemas.agent_schemas import AgentContext, NormalizedTravelRequirements, TravelRequest
from app.schemas.ai_cost_estimation import AICostEstimationResponse
from app.schemas.budget import BudgetBreakdown, BudgetLineItem, BudgetNarrative
from app.schemas.destination_research import SourceReference
from app.services.ai_cost_estimation_service import AICostEstimationService
from app.services.cost_estimation_service import CostEstimationService
from app.services.gemini_service import GeminiAPIError, GeminiInvalidJSONError
from test_ai_cost_estimation import RATES, settings
from test_budget_agent import accommodation_plan


class PricingService(CostEstimationService):
    def __init__(self, config, items=None):
        super().__init__(config)
        self.items = items or {}
        self.provider_calls = []
        self.ai_calls = []

    async def estimate_missing(self, requirements, research_data, plan, categories):
        self.provider_calls.append(list(categories))
        return copy.deepcopy(self.items), [], [f"{category}: provide a supported price or allowance." for category in categories]

    async def estimate_ai_assumptions(self, request, **kwargs):
        self.ai_calls.append(request)
        return await super().estimate_ai_assumptions(request, **kwargs)


class GeminiFixture:
    def __init__(self, categories=None, error=None, mutate=None, cost_delay=0, narrative_delay=0, ranges=False):
        self.categories = categories
        self.error = error
        self.mutate = mutate
        self.cost_delay = cost_delay
        self.narrative_delay = narrative_delay
        self.ranges = ranges
        self.calls = []

    @property
    def cost_prompts(self):
        return [payload for model, payload in self.calls if model is AICostEstimationResponse]

    async def generate_structured(self, prompt, response_model, **kwargs):
        payload = json.loads(prompt)
        self.calls.append((response_model, payload))
        if response_model is BudgetNarrative:
            if self.narrative_delay:
                await asyncio.sleep(self.narrative_delay)
            return BudgetNarrative(summary="Reconfirm planning allowances before booking.")
        assert response_model is AICostEstimationResponse
        if self.cost_delay:
            await asyncio.sleep(self.cost_delay)
        if self.error:
            raise self.error
        scope = {key: value for key, value in payload.items() if key not in {"missing_categories", "category_units"}}
        estimates = []
        for category in payload["missing_categories"] if self.categories is None else self.categories:
            item = {"category": category, "unit": payload["category_units"].get(category, "traveler_trip"),
                    "currency": payload["currency"], "typical": RATES[category]}
            if self.ranges:
                item.update(minimum=str(Decimal(RATES[category]) * Decimal("0.9")), maximum=str(Decimal(RATES[category]) * Decimal("1.1")))
            estimates.append(item)
        raw = {"scope": scope, "estimates": estimates}
        if self.mutate:
            self.mutate(raw)
        return response_model.model_validate(raw)


def context(*, preferences=None, budget="10000", all_missing=False, **changes):
    if not all_missing and preferences is None:
        preferences = {"food_allowance_per_traveler_per_day": "75", "activities_allowance_per_traveler_per_trip": "150"}
    values = dict(origin="Sample Origin", destination="Sample Destination", start_date=date(2027, 5, 1),
                  end_date=date(2027, 5, 5), travelers_count=3, total_budget=Decimal(budget), currency="AED",
                  travel_style="balanced", travel_duration_days=5, interests=[], constraints={}, cost_preferences=preferences)
    values.update(changes)
    requirements = NormalizedTravelRequirements.model_validate(values)
    plan = accommodation_plan(currency="AED", intercity=None, accommodation=None if all_missing else "2000",
                              local=None if all_missing else "200")
    plan["number_of_nights"] = 4
    plan["accommodation"].update(nights=4, room_count=2)
    return AgentContext(normalized_requirements=requirements, accommodation_transport_plan=plan,
                        destination_research={"destination_name": "Resolved Destination", "country_code": "ae", "resolution_status": "verified"})


def run(ctx=None, *, gemini=None, config=None, items=None):
    config = config or settings()
    gemini = gemini or GeminiFixture()
    service = PricingService(config, items)
    result = asyncio.run(BudgetAgent(settings=config, gemini_service=gemini, cost_estimation_service=service).run(ctx or context()))
    return result.output, gemini, service


def test_missing_flight_filled_after_provider_merge_before_contingency():
    budget, gemini, service = run()
    amounts = {line.category: line.amount for line in budget.line_items}
    assert amounts == {"accommodation": Decimal("2000"), "local_transport": Decimal("200"),
                       "food": Decimal("1125"), "activities": Decimal("450"),
                       "intercity_transport": Decimal("3000"), "contingency": Decimal("677.50")}
    assert budget.estimated_total == Decimal("7452.50") and budget.remaining_budget == Decimal("2547.50")
    assert budget.pricing_status == "COMPLETE_ESTIMATE" and budget.status == "WITHIN_BUDGET"
    assert budget.estimate_confidence == "low" and budget.booking_readiness == "unverified"
    assert not budget.unpriced_components and not budget.missing_price_actions and not budget.missing_price_inputs
    assert len(service.ai_calls) == 1 and service.provider_calls
    assert gemini.cost_prompts[0]["missing_categories"] == ["intercity_transport"]
    assert gemini.cost_prompts[0]["destination"] == "Resolved Destination"
    flight = next(line for line in budget.line_items if line.category == "intercity_transport")
    assert flight.ai_provenance["quantity"] == 3
    assert flight.ai_provenance["unit_rate"]["unit"] == "traveler_round_trip"
    assert BudgetBreakdown.model_validate_json(budget.model_dump_json()) == budget


def test_multiple_missing_categories_and_ranges_reconcile_with_contingency():
    budget, gemini, _ = run(context(all_missing=True), gemini=GeminiFixture(ranges=True))
    assert set(gemini.cost_prompts[0]["missing_categories"]) == set(RATES)
    assert budget.estimated_total == Decimal("7452.50")
    assert budget.minimum_estimated_total == Decimal("6707.25")
    assert budget.maximum_estimated_total == Decimal("8197.75")
    assert sum(line.amount for line in budget.line_items) == budget.known_cost_subtotal
    assert all(total.complete for total in budget.category_totals if total.included)
    assert len([line for line in budget.line_items if line.ai_provenance]) == 5


def test_user_contingency_percentage_and_room_count_are_preserved():
    ctx = context(all_missing=True, preferences={"room_count": 3, "contingency_percentage": "7.25"})
    budget, gemini, _ = run(ctx)
    assert gemini.cost_prompts[0]["room_count"] == 3
    assert next(line.amount for line in budget.line_items if line.category == "accommodation") == Decimal("3000")
    assert next(line.amount for line in budget.line_items if line.category == "contingency") == Decimal("563.69")
    assert budget.estimated_total == Decimal("8338.69")


def test_user_fixed_contingency_is_preserved():
    budget, _, _ = run(context(constraints={"budget_assumptions": {"contingency": {"amount": "123", "currency": "AED"}}}))
    assert next(line.amount for line in budget.line_items if line.category == "contingency") == Decimal("123")
    assert budget.estimated_total == Decimal("6898.00")


@pytest.mark.parametrize("source,classification", [("verified_provider", "verified"), ("reference_data", "estimated")])
def test_provider_and_reference_prices_precede_ai_and_are_never_overwritten(source, classification):
    item = BudgetLineItem(category="intercity_transport", description="Fictional provider/reference price", amount="600",
        currency="AED", classification=classification, source=source,
        source_references=[SourceReference(provider="amadeus", title="Fictional test evidence", url="https://example.org/fare")] if classification == "verified" else [])
    budget, gemini, service = run(items={"intercity_transport": item})
    flight = next(line for line in budget.line_items if line.category == "intercity_transport")
    assert flight.amount == Decimal("600") and flight.source == source and flight.ai_provenance is None
    assert not gemini.cost_prompts and not service.ai_calls


def test_user_flight_allowance_is_preserved_without_ai_call():
    budget, gemini, service = run(context(preferences={"intercity_round_trip_fare_per_traveler": "200",
        "food_allowance_per_traveler_per_day": "75", "activities_allowance_per_traveler_per_trip": "150"}))
    flight = next(line for line in budget.line_items if line.category == "intercity_transport")
    assert flight.amount == Decimal("600") and flight.source == "user_assumption"
    assert not gemini.cost_prompts and not service.ai_calls


def test_ai_fills_other_categories_without_changing_existing_fare_or_user_meal_allowance():
    fare = BudgetLineItem(category="intercity_transport", description="Fictional reference fare", amount="600",
        currency="AED", classification="estimated", source="reference_data")
    budget, gemini, _ = run(context(all_missing=True, preferences={"food_allowance_per_traveler_per_day": "100"}),
                            items={"intercity_transport": fare})
    assert set(gemini.cost_prompts[0]["missing_categories"]) == {"accommodation", "local_transport", "activities"}
    assert next(line.amount for line in budget.line_items if line.category == "food") == Decimal("1500")
    assert next(line.amount for line in budget.line_items if line.category == "intercity_transport") == Decimal("600")
    assert budget.estimated_total == Decimal("5225.00")
    assert fare.ai_provenance is None


@pytest.mark.parametrize("error", [GeminiAPIError("PRIVATE provider body"), GeminiInvalidJSONError("PRIVATE invalid JSON")])
def test_gemini_failures_retain_existing_costs_and_incomplete_total(error):
    budget, _, _ = run(gemini=GeminiFixture(error=error))
    assert budget.estimated_total is None and budget.pricing_status == "INCOMPLETE"
    assert budget.known_cost_subtotal == Decimal("4152.50")
    assert next(line.amount for line in budget.line_items if line.category == "accommodation") == Decimal("2000")
    assert any("intercity_transport" in component for component in budget.unpriced_components)
    assert "PRIVATE" not in str(budget)


@pytest.mark.parametrize("mutation", [
    lambda raw: raw["estimates"][0].update(typical="-1"),
    lambda raw: raw["estimates"][0].update(unit="party_day"),
    lambda raw: raw["estimates"][0].update(source_url="https://example.org/invented"),
    lambda raw: raw["scope"].update(travelers_count=99),
])
def test_invalid_gemini_responses_do_not_complete_or_erase_budget(mutation):
    budget, _, _ = run(gemini=GeminiFixture(mutate=mutation))
    assert budget.estimated_total is None and budget.pricing_status == "INCOMPLETE"
    assert budget.known_cost_subtotal == Decimal("4152.50")
    assert not any(line.ai_provenance for line in budget.line_items)


def test_ai_timeout_retains_costs_and_does_not_fail_agent():
    budget, gemini, _ = run(config=settings(AI_COST_ESTIMATION_TIMEOUT_SECONDS=0.01), gemini=GeminiFixture(cost_delay=1))
    assert len(gemini.cost_prompts) == 1
    assert budget.estimated_total is None and budget.status == "INCOMPLETE_ESTIMATE"
    assert budget.known_cost_subtotal == Decimal("4152.50")
    assert any("deadline" in warning for warning in budget.warnings)


def test_slow_commentary_cannot_erase_successful_estimate():
    budget, _, _ = run(config=settings(TRIP_AGENT_TIMEOUT_SECONDS=0.09), gemini=GeminiFixture(narrative_delay=1))
    assert budget.estimated_total == Decimal("7452.50")
    assert any("commentary exceeded" in warning for warning in budget.warnings)


def test_ai_disabled_keeps_missing_flight_and_never_invokes_foundation():
    budget, gemini, service = run(config=settings(AI_COST_ESTIMATION_ENABLED=False))
    assert budget.estimated_total is None and budget.pricing_status == "INCOMPLETE"
    assert not gemini.cost_prompts and not service.ai_calls


def test_ai_prompts_and_costs_are_budget_independent_and_overrun_is_preserved():
    outputs = [run(context(budget=amount)) for amount in ("1000", "10000")]
    assert outputs[0][0].estimated_total == outputs[1][0].estimated_total == Decimal("7452.50")
    assert outputs[0][0].status == "OVER_BUDGET" and outputs[0][0].over_budget_amount == Decimal("6452.50")
    assert outputs[0][1].cost_prompts == outputs[1][1].cost_prompts
    for _, gemini, _ in outputs:
        for _, payload in gemini.calls:
            text = json.dumps(payload)
            assert all(key not in text for key in ("total_budget", "remaining_budget", "known_cost_subtotal", "affordability_target", "planning_allowances_and_sources"))


def test_partial_ai_response_keeps_missing_mandatory_categories_unknown():
    budget, _, _ = run(context(all_missing=True), gemini=GeminiFixture(categories=["food", "activities"]))
    assert budget.estimated_total is None and budget.remaining_budget is None
    assert budget.pricing_status == "INCOMPLETE" and budget.estimate_confidence == "low"
    assert budget.known_cost_subtotal == Decimal("1732.50")
    assert len([line for line in budget.line_items if line.amount is None]) == 3
    assert set(budget.missing_price_inputs) == {"accommodation_nightly_rate_per_room", "local_transport_allowance_per_day", "intercity_round_trip_fare_per_traveler"}
    assert all(not action.startswith(("food:", "activities:")) for action in budget.missing_price_actions)


def test_ai_evidence_never_gets_provider_sources_or_live_classification():
    budget, _, _ = run(context(all_missing=True))
    for line in budget.line_items:
        if line.ai_provenance:
            assert line.evidence_kind == "planning_estimate" and line.classification == "estimated"
            assert line.confidence == "low"
            assert not line.price_sources and not line.source_references
            assert line.flight_reference is None and line.currency_conversion is None
            assert line.ai_provenance["live_availability_checked"] is False
            raw = line.model_dump(mode="json")
            raw["classification"] = "verified"
            with pytest.raises(ValidationError):
                BudgetLineItem.model_validate(raw)
    assert budget.pricing_status != "LIVE_PRICE_VERIFIED"


def test_no_second_estimation_for_already_priced_categories():
    async def exercise():
        ctx = context()
        ctx.normalized_requirements.cost_preferences.contingency_percentage = Decimal("10")
        gemini = GeminiFixture()
        service = PricingService(settings())
        agent = BudgetAgent(settings=settings(), gemini_service=gemini, cost_estimation_service=service)
        first = await agent.run(ctx)
        flight = next(line for line in first.output.line_items if line.category == "intercity_transport")
        # An applicable supplied allowance now resolves the only missing cost.
        ctx.normalized_requirements.cost_preferences.intercity_round_trip_fare_per_traveler = Decimal("1000")
        second = await agent.run(ctx)
        assert len(service.ai_calls) == 1 and len(gemini.cost_prompts) == 1
        assert second.output.estimated_total == first.output.estimated_total
        assert flight.amount == Decimal("3000")
    asyncio.run(exercise())


def test_existing_snapshot_recalculation_retains_ai_reserve_without_requesting_new_assumptions():
    async def exercise():
        ctx = context(all_missing=True)
        ctx.request = TravelRequest.model_validate(ctx.normalized_requirements.model_dump(
            exclude={"travel_duration_days", "missing_required_fields", "conflicting_requirements"}))
        gemini = GeminiFixture(ranges=True)
        service = PricingService(settings())
        agent = BudgetAgent(settings=settings(), gemini_service=gemini, cost_estimation_service=service)
        initial = await agent.run(ctx)
        ctx.budget_breakdown = initial.output.model_dump(mode="json")
        ctx.budget_revision = {"replacement_items": {}, "removed_other_indexes": []}
        retained = await agent.run(ctx)
        assert len(service.ai_calls) == 1 and len(gemini.cost_prompts) == 1
        assert retained.output.estimated_total == initial.output.estimated_total == Decimal("7452.50")
        assert retained.output.minimum_estimated_total == initial.output.minimum_estimated_total
        assert retained.output.maximum_estimated_total == initial.output.maximum_estimated_total
        assert retained.output.estimate_confidence == "low"
        assert all(not line.price_sources for line in retained.output.line_items if line.ai_provenance)
    asyncio.run(exercise())


def test_cancellation_during_ai_estimation_propagates():
    async def exercise():
        gemini = GeminiFixture(cost_delay=1)
        agent = BudgetAgent(settings=settings(), gemini_service=gemini, cost_estimation_service=PricingService(settings()))
        task = asyncio.create_task(agent.run(context()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())


@pytest.mark.parametrize("changes", [{"origin": None}, {"travel_duration_days": 99}, {"travelers_count": None}])
def test_invalid_scope_does_not_invoke_ai_or_fail_existing_calculations(changes):
    budget, gemini, service = run(context(**changes))
    assert not gemini.cost_prompts and not service.ai_calls
    assert budget.estimated_total is None and budget.pricing_status == "INCOMPLETE"


def test_foreign_currency_known_amount_is_not_overwritten_by_ai():
    ctx = context()
    ctx.accommodation_transport_plan["intercity_transport_cost"].update(amount="100", currency="USD", classification="estimate")
    budget, gemini, service = run(ctx)
    flight = next(line for line in budget.line_items if line.category == "intercity_transport")
    assert flight.amount == Decimal("100") and flight.currency == "USD"
    assert budget.estimated_total is None and not gemini.cost_prompts and not service.ai_calls


def test_specific_paid_activity_and_broad_origin_do_not_receive_generic_allowances():
    ctx = context(all_missing=True, constraints={"must_visit_attraction_ids": ["verified-required-id"]})
    ctx.accommodation_transport_plan["origin_resolution_status"] = "broad"
    budget, gemini, _ = run(ctx)
    assert set(gemini.cost_prompts[0]["missing_categories"]) == {"accommodation", "food", "local_transport"}
    assert budget.estimated_total is None and budget.pricing_status == "INCOMPLETE"


def test_room_inference_and_zero_nights_are_python_calculations():
    ctx = context(all_missing=True)
    ctx.accommodation_transport_plan["accommodation"]["room_count"] = None
    budget, gemini, _ = run(ctx)
    assert gemini.cost_prompts[0]["room_count"] == 2
    stay = next(line for line in budget.line_items if line.category == "accommodation")
    assert any("Python assumed 2" in assumption for assumption in stay.assumptions)
    ctx = context(all_missing=True, end_date=date(2027, 5, 1), travel_duration_days=1)
    budget, gemini, _ = run(ctx)
    assert "accommodation" not in gemini.cost_prompts[0]["missing_categories"]
    assert next(line.amount for line in budget.line_items if line.category == "accommodation") == Decimal("0")
