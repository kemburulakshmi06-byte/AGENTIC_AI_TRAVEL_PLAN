"""AI safety/revision fixtures are fictional; every external service is mocked."""
import asyncio
import copy
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agents.budget_agent import BudgetAgent
from app.agents.itinerary_agent import ItineraryAgent
from app.agents.optimization_agent import OptimizationAgent
from app.agents.requirements_agent import RequirementsAgent
from app.schemas.accommodation_transport import AccommodationTransportPlan
from app.schemas.agent_schemas import TravelRequest
from app.schemas.budget import BudgetBreakdown, BudgetLineItem
from app.schemas.destination_research import DestinationResearch, SourceReference
from app.schemas.optimization import OptimizationNarrative
from app.services.ai_cost_safety import ai_budget_issue
from app.services.budget_revision_service import reconciled_snapshot, revised_entries
from app.workflows.travel_graph import TravelAgentSet, _budget_safety_status, plan_trip
from test_ai_cost_estimation import settings
from test_budget_ai_estimation import context, GeminiFixture, PricingService
from test_itinerary_agent import FakeRouting
from test_optimization_agent import FakeGemini
from test_travel_graph import FakeAgent, research


def prepared_context(**kwargs):
    ctx = context(all_missing=True, **kwargs)
    ctx.request = TravelRequest.model_validate(ctx.normalized_requirements.model_dump(
        exclude={"travel_duration_days", "missing_required_fields", "conflicting_requirements"}))
    destination = research().model_copy(update={"destination_name": "Resolved Destination", "country_code": "ae", "resolution_status": "verified"})
    ctx.destination_research = destination.model_dump(mode="json")
    return ctx


async def priced_context(ctx=None, *, gemini=None, config=None):
    ctx = ctx or prepared_context()
    config = config or settings()
    gemini = gemini or GeminiFixture(ranges=True)
    pricing = PricingService(config)
    agent = BudgetAgent(settings=config, gemini_service=gemini, cost_estimation_service=pricing)
    budget = (await agent.run(ctx)).output
    ctx.budget_breakdown = budget.model_dump(mode="json")
    itinerary = (await ItineraryAgent(settings=settings(GEMINI_API_KEY=None), routing=FakeRouting()).run(ctx)).output
    ctx.trip_itinerary = itinerary.model_dump(mode="json")
    return ctx, budget, agent, gemini, pricing


def state_for(ctx, budget):
    return {"request": ctx.request, "normalized_requirements": ctx.normalized_requirements,
            "budget_breakdown": budget, "destination_research": DestinationResearch.model_validate(ctx.destination_research),
            "accommodation_transport_plan": ctx.accommodation_transport_plan}


@pytest.mark.parametrize("amount,status,approved", [("10000", "APPROVED", True), ("1000", "REVISION_REQUIRED", False)])
def test_reviewer_and_workflow_accept_only_affordable_complete_ai_planning_estimates(amount, status, approved):
    async def run():
        ctx, budget, _, _, _ = await priced_context(prepared_context(budget=amount))
        result = (await OptimizationAgent(settings=settings(GEMINI_API_KEY=None)).run(ctx)).output
        assert result.review_status == status and result.approved is approved
        assert any("AI-generated low-confidence" in warning for warning in result.warnings)
        safe, warning = _budget_safety_status(state_for(ctx, budget))
        assert safe == ("RUNNING" if approved else "REVISION_REQUIRED")
        assert (warning is None) is approved
        assert budget.pricing_status == "COMPLETE_ESTIMATE" and budget.booking_readiness == "unverified"
        assert budget.estimate_confidence == "low"
    asyncio.run(run())


def test_partial_ai_estimates_remain_insufficient_with_existing_budget_present():
    async def run():
        ctx, budget, _, _, _ = await priced_context(gemini=GeminiFixture(categories=["food", "activities"]))
        review = (await OptimizationAgent(settings=settings(GEMINI_API_KEY=None)).run(ctx)).output
        assert review.review_status == "INSUFFICIENT_DATA" and not review.approved
        assert budget.estimated_total is None and budget.pricing_status == "INCOMPLETE"
        assert _budget_safety_status(state_for(ctx, budget))[0] == "INSUFFICIENT_DATA"
        assert not any(issue.code == "missing_or_invalid_budget" for issue in review.issues)
    asyncio.run(run())


@pytest.mark.parametrize("claim", ["The prices are verified.", "The trip is booking-ready.", "These are high-confidence estimates."])
@pytest.mark.parametrize("in_suggestion", [False, True])
def test_reviewer_prose_cannot_promote_ai_costs_to_verified_booking_prices(claim, in_suggestion):
    async def run():
        ctx, budget, _, _, _ = await priced_context()
        narrative = OptimizationNarrative(summary="Group nearby activities." if in_suggestion else claim,
            suggestions=[claim] if in_suggestion else [])
        result = (await OptimizationAgent(settings=settings(GEMINI_API_KEY=None), gemini_service=FakeGemini(narrative)).run(ctx)).output
        assert result.approved and result.review_status == "APPROVED"
        assert result.qualitative_summary is None
        assert any("unsupported AI price verification" in warning for warning in result.warnings)
        assert budget.booking_readiness == "unverified" and budget.estimate_confidence == "low"
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("origin", "Wrong Origin"), ("destination", "Wrong Destination"),
    ("travel_style", "luxury"), ("transport_mode", "bus"), ("start_date", "2027-05-02"),
    ("end_date", "2027-05-06"), ("travelers_count", 4), ("room_count", 3)])
def test_request_scope_mismatches_block_approval_even_if_line_arithmetic_is_consistent(field, value):
    async def run():
        ctx, budget, _, _, _ = await priced_context()
        line = next(item for item in budget.line_items if item.category == "intercity_transport")
        line.ai_provenance["scope"][field] = value
        ctx.budget_breakdown = budget.model_dump(mode="json")
        review = (await OptimizationAgent(settings=settings(GEMINI_API_KEY=None)).run(ctx)).output
        assert not review.approved and review.review_status == "INSUFFICIENT_DATA"
        assert _budget_safety_status(state_for(ctx, budget))[0] == "INSUFFICIENT_DATA"
    asyncio.run(run())


@pytest.mark.parametrize("corruption", ["missing_provenance", "unit", "high_confidence", "live_kind", "provider_source", "provider_citation", "observed_price", "optional", "availability", "future_timestamp", "rate", "range"])
def test_invalid_ai_evidence_is_rejected_at_reviewer_and_workflow_boundaries(corruption):
    async def run():
        ctx, budget, _, _, _ = await priced_context()
        line = next(item for item in budget.line_items if item.category == "intercity_transport")
        if corruption == "missing_provenance": line.ai_provenance = None
        if corruption == "unit": line.ai_provenance["unit_rate"]["unit"] = "party_day"
        if corruption == "high_confidence": line.confidence = "high"
        if corruption == "live_kind": line.evidence_kind = "verified_price"
        if corruption == "provider_source": line.source = "verified_provider"
        if corruption == "provider_citation": line.source_references = [SourceReference(title="Invented", url="https://example.org/invented")]
        if corruption == "observed_price": line.ai_provenance["observed_on"] = "2030-01-01"
        if corruption == "optional": line.optional = True
        if corruption == "availability": line.ai_provenance["live_availability_checked"] = True
        if corruption == "future_timestamp": line.ai_provenance["generated_at"] = "2099-01-01T00:00:00Z"
        if corruption == "rate": line.ai_provenance["unit_rate"]["typical"] = "999"
        if corruption == "range": line.minimum_amount += Decimal("1")
        ctx.budget_breakdown = budget.model_dump(mode="json")
        review = (await OptimizationAgent(settings=settings(GEMINI_API_KEY=None)).run(ctx)).output
        assert not review.approved and review.review_status == "INSUFFICIENT_DATA"
        assert _budget_safety_status(state_for(ctx, budget))[0] == "INSUFFICIENT_DATA"
        assert reconciled_snapshot(ctx) is None
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("booking_readiness", "ready"), ("pricing_status", "LIVE_PRICE_VERIFIED"), ("estimate_confidence", "high"),
    ("minimum_estimated_total", Decimal("6708.25")), ("maximum_estimated_total", Decimal("8198.75"))])
def test_invalid_budget_level_ai_claims_and_scenarios_are_rejected(field, value):
    async def run():
        ctx, budget, _, _, _ = await priced_context()
        bad = budget.model_copy(update={field: value})
        ctx.budget_breakdown = bad.model_dump(mode="json")
        review = (await OptimizationAgent(settings=settings(GEMINI_API_KEY=None)).run(ctx)).output
        assert not review.approved
        assert _budget_safety_status(state_for(ctx, bad))[0] == "INSUFFICIENT_DATA"
    asyncio.run(run())


def test_normalized_scope_cannot_change_explicit_user_request_to_approve_ai_prices():
    async def run():
        ctx, budget, _, _, _ = await priced_context()
        ctx.request.travelers_count = 99
        assert _budget_safety_status(state_for(ctx, budget))[0] == "INSUFFICIENT_DATA"
        review = (await OptimizationAgent(settings=settings(GEMINI_API_KEY=None)).run(ctx)).output
        assert not review.approved
    asyncio.run(run())


def test_existing_requirements_text_normalization_does_not_invalidate_ai_costs():
    async def run():
        ctx = prepared_context()
        ctx.request = ctx.request.model_copy(update={"origin": "Sample   Origin", "destination": "Sample   Destination", "travel_style": "Balanced"})
        ctx.normalized_requirements = (await RequirementsAgent(settings=settings(GEMINI_API_KEY=None)).run(ctx)).output
        ctx, budget, _, _, _ = await priced_context(ctx)
        assert ai_budget_issue(budget, ctx.normalized_requirements, ctx.destination_research, ctx.accommodation_transport_plan, ctx.request) is None
        assert (await OptimizationAgent(settings=settings(GEMINI_API_KEY=None)).run(ctx)).output.approved
    asyncio.run(run())


def test_normalization_cannot_drop_the_explicit_transport_mode_constraint():
    async def run():
        ctx, budget, _, _, _ = await priced_context()
        ctx.request.constraints["intercity_routing_mode"] = "bus"
        assert ai_budget_issue(budget, ctx.normalized_requirements, ctx.destination_research, ctx.accommodation_transport_plan, ctx.request) == "invalid_ai_trip_scope"
        assert _budget_safety_status(state_for(ctx, budget))[0] == "INSUFFICIENT_DATA"
    asyncio.run(run())


@pytest.mark.parametrize("budget_amount", ["10000", "1000"])
@pytest.mark.parametrize("field", ["estimated_total", "remaining_budget", "over_budget_amount"])
def test_inconsistent_totals_block_ai_planning_approval(budget_amount, field):
    async def run():
        ctx, budget, _, _, _ = await priced_context(prepared_context(budget=budget_amount))
        setattr(budget, field, getattr(budget, field) + Decimal("1"))
        ctx.budget_breakdown = budget.model_dump(mode="json")
        review = (await OptimizationAgent(settings=settings(GEMINI_API_KEY=None)).run(ctx)).output
        assert not review.approved
        assert _budget_safety_status(state_for(ctx, budget))[0] == "INSUFFICIENT_DATA"
        assert reconciled_snapshot(ctx) is None
    asyncio.run(run())


def test_quantity_recalculation_preserves_ai_rates_ranges_and_generation_provenance():
    async def run():
        ctx, original, agent, gemini, pricing = await priced_context()
        saved = {item.category: copy.deepcopy(item.ai_provenance) for item in original.line_items if item.ai_provenance}
        ctx.normalized_requirements.travelers_count = ctx.request.travelers_count = 5
        from app.schemas.agent_schemas import TripCostPreferences
        ctx.normalized_requirements.cost_preferences = TripCostPreferences(room_count=3)
        ctx.request.cost_preferences = TripCostPreferences(room_count=3)
        ctx.budget_revision = {"replacement_items": {}, "removed_other_indexes": []}
        updated = (await agent.run(ctx)).output
        assert updated.estimated_total == Decimal("11907.50") and updated.status == "OVER_BUDGET"
        assert updated.minimum_estimated_total == Decimal("10716.75")
        assert updated.maximum_estimated_total == Decimal("13098.25")
        assert len(gemini.cost_prompts) == len(pricing.ai_calls) == len(pricing.provider_calls) == 1
        for item in updated.line_items:
            if item.ai_provenance:
                assert item.ai_provenance["unit_rate"] == saved[item.category]["unit_rate"]
                assert item.ai_provenance["generated_at"] == saved[item.category]["generated_at"]
                assert item.ai_provenance["generation_scope"] == saved[item.category]["scope"]
                assert item.confidence == "low" and not item.price_sources
        assert ai_budget_issue(updated, ctx.normalized_requirements, ctx.destination_research, ctx.accommodation_transport_plan, ctx.request) is None
        assert original.line_items[0].ai_provenance == saved[original.line_items[0].category]
    asyncio.run(run())


@pytest.mark.parametrize("change,expected", [("origin", {"intercity_transport"}), ("mode", {"intercity_transport", "local_transport"}),
    ("destination", set(["accommodation", "intercity_transport", "local_transport", "food", "activities"])),
    ("dates", set(["accommodation", "intercity_transport", "local_transport", "food", "activities"])),
    ("style", set(["accommodation", "intercity_transport", "local_transport", "food", "activities"]))])
def test_only_genuinely_changed_rate_scopes_trigger_fresh_pricing(change, expected):
    async def run():
        ctx, _, agent, gemini, pricing = await priced_context()
        if change == "origin": ctx.request.origin = ctx.normalized_requirements.origin = "Updated Origin"
        if change == "mode":
            ctx.request.constraints["intercity_routing_mode"] = "bus"
            ctx.normalized_requirements.constraints["intercity_routing_mode"] = "bus"
        if change == "destination":
            ctx.request.destination = ctx.normalized_requirements.destination = "Updated Destination"
            ctx.destination_research["destination_name"] = "Updated Destination"
        if change == "dates":
            ctx.request.start_date += timedelta(days=1)
            ctx.request.end_date += timedelta(days=1)
            ctx.normalized_requirements.start_date = ctx.request.start_date
            ctx.normalized_requirements.end_date = ctx.request.end_date
        if change == "style": ctx.request.travel_style = ctx.normalized_requirements.travel_style = "comfort"
        ctx.budget_revision = {"replacement_items": {}, "removed_other_indexes": []}
        updated = (await agent.run(ctx)).output
        assert updated.estimated_total == Decimal("7452.50")
        assert set(gemini.cost_prompts[-1]["missing_categories"]) == expected
        assert set(pricing.provider_calls[-1]) == expected
        assert len(gemini.cost_prompts) == 2
        assert any("scope changed" in warning for warning in updated.warnings)
        ctx.budget_breakdown = updated.model_dump(mode="json")
        again = (await agent.run(ctx)).output
        assert again.estimated_total == updated.estimated_total
        assert len(gemini.cost_prompts) == 2 and len(pricing.provider_calls) == 2
    asyncio.run(run())


def test_changed_scope_with_ai_disabled_stays_unknown_without_reusing_old_fares():
    async def run():
        ctx, _, _, _, _ = await priced_context()
        ctx.request.origin = ctx.normalized_requirements.origin = "Updated Origin"
        ctx.budget_revision = {"replacement_items": {}, "removed_other_indexes": []}
        gemini = GeminiFixture()
        config = settings(AI_COST_ESTIMATION_ENABLED=False)
        pricing = PricingService(config)
        updated = (await BudgetAgent(settings=config, gemini_service=gemini, cost_estimation_service=pricing).run(ctx)).output
        assert updated.estimated_total is None and updated.pricing_status == "INCOMPLETE"
        assert not gemini.cost_prompts
        assert next(item.amount for item in updated.line_items if item.category == "intercity_transport") is None
    asyncio.run(run())


@pytest.mark.parametrize("forged", ["reference", "cheaper_ai", "range_minimum"])
def test_revision_directives_cannot_replace_or_lower_ai_rates(forged):
    async def run():
        ctx, budget, _, _, _ = await priced_context()
        line = next(item for item in budget.line_items if item.category == "accommodation")
        replacement = line.model_dump(mode="json")
        if forged == "reference":
            replacement.update(ai_provenance=None, source="reference_data", price_sources=[{"name": "Invented", "url": "https://example.org/fake", "observed_on": "2027-01-01", "currency": "AED"}])
        if forged == "cheaper_ai": replacement["amount"] = "100"
        if forged == "range_minimum": replacement["amount"] = replacement["minimum_amount"]
        ctx.budget_revision = {"replacement_items": {"accommodation": replacement}, "removed_other_indexes": []}
        from app.agents.budget_agent import _BudgetEntry
        with pytest.raises(ValueError):
            revised_entries(ctx, [_BudgetEntry(line, True)])
    asyncio.run(run())


@pytest.mark.parametrize("budget_amount,expected", [("10000", "COMPLETED"), ("1000", "REVISION_REQUIRED")])
def test_real_six_agent_graph_uses_ai_estimates_without_forcing_completion(budget_amount, expected):
    async def run():
        ctx = prepared_context(budget=budget_amount)
        gemini = GeminiFixture(ranges=True)
        config = settings()
        pricing = PricingService(config)
        agents = TravelAgentSet(
            requirements=FakeAgent("requirements", ctx.normalized_requirements),
            destination_research=FakeAgent("destination_research", DestinationResearch.model_validate(ctx.destination_research)),
            accommodation_transport=FakeAgent("accommodation_transport", lambda _: AccommodationTransportPlan.model_validate(ctx.accommodation_transport_plan)),
            budget=BudgetAgent(settings=config, gemini_service=gemini, cost_estimation_service=pricing),
            itinerary=ItineraryAgent(settings=settings(GEMINI_API_KEY=None), routing=FakeRouting()),
            optimization_review=OptimizationAgent(settings=settings(GEMINI_API_KEY=None)))
        result = await plan_trip(ctx.request, agents=agents, max_revisions=2)
        assert result["execution_status"] == expected
        assert len(gemini.cost_prompts) == 1
        assert result["revision_count"] == 0
        assert {record.agent_name for record in result["agent_history"]} == {"requirements", "destination_research", "accommodation_transport", "budget", "itinerary", "optimization_review"}
        assert result["budget_breakdown"].booking_readiness == "unverified"
    asyncio.run(run())


def test_optional_cost_revision_preserves_all_ai_provenance_and_never_regenerates_rates():
    async def run():
        ctx = prepared_context(constraints={"budget_assumptions": {"other_expenses": [{"description": "Optional extra", "amount": "5000", "optional": True}]}})
        gemini = GeminiFixture(ranges=True)
        pricing = PricingService(settings())
        agents = TravelAgentSet(
            requirements=FakeAgent("requirements", ctx.normalized_requirements),
            destination_research=FakeAgent("destination_research", DestinationResearch.model_validate(ctx.destination_research)),
            accommodation_transport=FakeAgent("accommodation_transport", AccommodationTransportPlan.model_validate(ctx.accommodation_transport_plan)),
            budget=BudgetAgent(settings=settings(), gemini_service=gemini, cost_estimation_service=pricing),
            itinerary=ItineraryAgent(settings=settings(GEMINI_API_KEY=None), routing=FakeRouting()),
            optimization_review=OptimizationAgent(settings=settings(GEMINI_API_KEY=None)))
        result = await plan_trip(ctx.request, agents=agents, max_revisions=2)
        assert result["execution_status"] == "COMPLETED" and result["revision_count"] == 1
        assert result["budget_breakdown"].estimated_total == Decimal("7452.50")
        histories = [record.output_data for record in result["agent_history"] if record.agent_name == "budget"]
        assert len(histories) == 2
        for before, after in zip(histories[0]["line_items"], histories[1]["line_items"]):
            if before["ai_provenance"]:
                assert before["ai_provenance"] == after["ai_provenance"]
                assert before["amount"] == after["amount"]
        assert len(pricing.ai_calls) == len(gemini.cost_prompts) == len(pricing.provider_calls) == 1
    asyncio.run(run())
