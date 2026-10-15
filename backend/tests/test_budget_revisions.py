"""Fictional sourced fixtures; no providers, database or Redis are contacted."""
import asyncio
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.agents.budget_agent import BudgetAgent
from app.agents.optimization_agent import OptimizationAgent
from app.core.config import Settings
from app.schemas.agent_schemas import AgentContext, NormalizedTravelRequirements
from app.schemas.budget import BudgetBreakdown
from app.schemas.optimization import OptimizationNarrative
from app.services.budget_revision_service import reconciled_snapshot
from app.services.cost_estimation_service import CostEstimationService
from app.workflows.travel_graph import plan_trip, _budget_safety_status
from test_travel_graph import (
    FakeAgent, accommodation, agent_set, itinerary, request, research, review,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def blocked(*args, **kwargs):
        raise AssertionError("Unexpected network call")
    monkeypatch.setattr(socket.socket, "connect", blocked)


def context(*, optional=True, flight_missing=False, reserve=False, constraints=None):
    req = request()
    assumptions = {
        "food": {"amount": "6000"}, "activities": {"amount": "2000"},
        "other_expenses": [{"description": "Optional extra", "amount": "10000",
                             "optional": optional}],
    }
    if reserve:
        assumptions["contingency"] = {"percentage": "10"}
        req.total_budget = Decimal("27500")
    req.constraints = {"budget_assumptions": assumptions, **(constraints or {})}
    normalized = NormalizedTravelRequirements(**req.model_dump(exclude={"user_message"}), travel_duration_days=2)
    plan = accommodation()
    if flight_missing:
        plan.intercity_transport_cost.amount = None
        plan.intercity_transport_cost.classification = "unknown"
    destination = research()
    destination.country_code = "ae"
    return AgentContext(request=req, normalized_requirements=normalized,
        destination_research=destination.model_dump(mode="json"),
        accommodation_transport_plan=plan.model_dump(mode="json"))


class CountingEstimator(CostEstimationService):
    calls = 0

    async def estimate_missing(self, *args, **kwargs):
        self.calls += 1
        return await super().estimate_missing(*args, **kwargs)


def build_budget(*, automatic=False, data=None):
    settings = Settings(_env_file=None, GEMINI_API_KEY=None, AMADEUS_API_KEY=None,
                        AUTOMATIC_COST_ESTIMATION_ENABLED=automatic)
    estimator = CountingEstimator(settings, reference_data=data)
    return BudgetAgent(settings=settings, cost_estimation_service=estimator), estimator


def run_workflow(ctx, agent, limit=2, reviewer=None):
    def updated_itinerary(current):
        output = itinerary(set(current.normalized_requirements.constraints.get("excluded_attraction_ids", [])))
        budget = BudgetBreakdown.model_validate(current.budget_breakdown)
        output.budget_status = budget.status
        output.estimated_total_cost = budget.estimated_total
        output.currency = budget.currency
        return output
    agents = agent_set(
        requirements_agent=FakeAgent("requirements", ctx.normalized_requirements),
        research_agent=FakeAgent("destination_research", research()),
        accommodation_agent=FakeAgent("accommodation_transport", accommodation()),
        budget_agent=agent, itinerary_agent=FakeAgent("itinerary", updated_itinerary),
        review_agent=reviewer or FakeAgent("optimization_review", review()),
    )
    # Use the supplied, possibly missing-flight snapshot and verified country.
    agents.accommodation_transport.output = type(accommodation()).model_validate(ctx.accommodation_transport_plan)
    agents.destination_research.output = type(research()).model_validate(ctx.destination_research)
    result = asyncio.run(plan_trip(ctx.request, agents=agents, max_revisions=limit))
    return result, agents


def test_optional_reduction_reruns_budget_itinerary_and_review_and_reconciles_reserve():
    ctx = context(reserve=True)
    agent, estimator = build_budget()
    result, agents = run_workflow(ctx, agent)
    assert result["execution_status"] == "COMPLETED"
    assert result["errors"] == []
    assert result["revision_count"] == 1
    assert result["budget_inputs_changed"] is True
    assert [record.agent_name for record in result["agent_history"]][-3:] == ["budget", "itinerary", "optimization_review"]
    budget = result["budget_breakdown"]
    assert budget.estimated_total == Decimal("27500")
    assert sum(item.amount for item in budget.line_items) == budget.known_cost_subtotal
    assert next(item.amount for item in budget.line_items if item.category == "contingency") == Decimal("2500")
    assert estimator.calls == 1
    assert len(agents.itinerary.calls) == len(agents.optimization_review.calls) == 2
    assert result["request"] == ctx.request
    assert result["normalized_requirements"] == ctx.normalized_requirements
    assert result["destination_research"].attractions == research().attractions


def test_missing_flight_remains_insufficient_after_supported_reduction():
    ctx = context(flight_missing=True)
    agent, estimator = build_budget()
    result, _ = run_workflow(ctx, agent)
    assert result["execution_status"] == "INSUFFICIENT_DATA"
    assert result["revision_count"] == 1
    budget = result["budget_breakdown"]
    assert budget.estimated_total is None
    assert budget.pricing_status == "INCOMPLETE"
    assert any(component.startswith("intercity_transport:") for component in budget.unpriced_components)
    assert any("intercity" in text for text in budget.missing_price_inputs)
    assert result["optimization_review"].approved is False
    assert estimator.calls == 1


@pytest.mark.parametrize("limit", [0, 1, 5])
def test_no_supported_alternative_or_revision_limit_does_not_loop(limit):
    ctx = context(optional=False)
    agent, estimator = build_budget()
    result, agents = run_workflow(ctx, agent, limit)
    assert result["execution_status"] == "REVISION_REQUIRED"
    assert result["revision_count"] == 0
    assert len(agents.itinerary.calls) == 1
    assert estimator.calls == 1


def test_zero_revision_limit_blocks_supported_changes():
    agent, _ = build_budget()
    result, _ = run_workflow(context(), agent, 0)
    assert result["execution_status"] == "REVISION_REQUIRED"
    assert result["revision_count"] == 0


def reference_data():
    today = date.today()
    def rate(style, typical):
        return dict(id=f"fixture-{style}", category="accommodation", country_code="ae",
                    styles=[style], unit="room_night", occupancy=2, currency="INR",
                    minimum=typical, typical=typical, maximum=typical, confidence="low",
                    source_name="Fictional test accommodation allowance", source_url="https://example.com/reference",
                    observed_on=str(today), valid_until=str(today + timedelta(days=30)),
                    assumptions=["Fictional fixture; no availability or real hotel prices."])
    return {"style_aliases": {"balanced": "balanced", "budget": "budget"},
            "rates": [rate("balanced", "30000"), rate("budget", "10000") | {"minimum": "1000"}]}


def test_supported_reference_alternative_uses_typical_not_range_minimum():
    ctx = context(optional=False)
    ctx.request.travel_style = ctx.normalized_requirements.travel_style = "balanced"
    ctx.request.constraints["budget_assumptions"]["other_expenses"] = []
    ctx.normalized_requirements.constraints["budget_assumptions"]["other_expenses"] = []
    agent, _ = build_budget(automatic=True, data=reference_data())
    # Keep fixture totals at 25000 after the reference replacement.
    agent._settings.DEFAULT_ESTIMATE_CONTINGENCY_PERCENT = Decimal("0")
    result, _ = run_workflow(ctx, agent)
    assert result["execution_status"] == "COMPLETED"
    assert result["revision_count"] == 1
    line = next(item for item in result["budget_breakdown"].line_items if item.category == "accommodation")
    assert line.amount == Decimal("10000")
    assert line.source == "reference_data" and line.price_sources
    assert result["accommodation_transport_plan"].accommodation_cost.amount == line.amount
    assert result["accommodation_transport_plan"].accommodation.live_availability_checked is False
    assert result["normalized_requirements"].travel_style == "balanced"


def test_mandatory_constraints_prevent_unconfirmed_downgrades():
    agent, _ = build_budget()
    result, _ = run_workflow(context(constraints={"accessibility": "step-free"}), agent)
    assert result["execution_status"] == "REVISION_REQUIRED"
    assert result["revision_count"] == 0
    assert result["normalized_requirements"].constraints["accessibility"] == "step-free"


def test_identical_directive_is_a_noop_and_corrupt_snapshot_cannot_be_revised():
    async def run():
        ctx = context()
        agent, _ = build_budget()
        first = (await agent.run(ctx)).output
        ctx.budget_breakdown = first.model_dump(mode="json")
        directive = await agent.propose_budget_revision(ctx)
        ctx.budget_revision = directive
        assert await agent.propose_budget_revision(ctx) is None
        ctx.budget_breakdown["known_cost_subtotal"] = "1"
        assert reconciled_snapshot(ctx) is None
        assert await agent.propose_budget_revision(ctx) is None
    asyncio.run(run())


def test_reviewer_does_not_confuse_known_budget_with_incomplete_over_budget_total():
    async def run():
        ctx = context(flight_missing=True)
        agent, _ = build_budget()
        output = (await agent.run(ctx)).output
        assert output.status == "OVER_BUDGET" and output.estimated_total is None
        ctx.budget_breakdown = output.model_dump(mode="json")
        reviewer = OptimizationAgent(settings=Settings(_env_file=None, GEMINI_API_KEY=None))
        result = (await reviewer.run(ctx)).output
        assert result.review_status == "INSUFFICIENT_DATA"
        codes = {issue.code for issue in result.issues}
        assert "missing_budget_total" not in codes
        assert "missing_or_invalid_budget" not in codes
        assert "missing_required_prices" in codes
        assert "not a complete trip estimate" in result.qualitative_summary
    asyncio.run(run())


def test_dubai_five_day_missing_flight_regression():
    async def run():
        ctx = context(flight_missing=True, optional=False)
        ctx.request.currency = ctx.normalized_requirements.currency = "AED"
        ctx.request.total_budget = ctx.normalized_requirements.total_budget = Decimal("8000")
        ctx.request.end_date = ctx.normalized_requirements.end_date = ctx.request.start_date + timedelta(days=4)
        ctx.normalized_requirements.travel_duration_days = 5
        ctx.request.destination = ctx.normalized_requirements.destination = "Dubai, UAE"
        assumptions = ctx.normalized_requirements.constraints["budget_assumptions"]
        assumptions.update(food={"amount": "5000"}, activities={"amount": "0"}, other_expenses=[])
        plan = ctx.accommodation_transport_plan
        plan["currency"] = "AED"
        for name in ("accommodation_cost", "local_transport_cost", "intercity_transport_cost", "combined_estimated_cost"):
            plan[name]["currency"] = "AED"
        plan["accommodation_cost"]["amount"] = "5000"
        plan["local_transport_cost"]["amount"] = "667.80"
        for name in ("estimated_nightly_rate", "estimated_total_cost"):
            plan["accommodation"][name]["currency"] = "AED"
        agent, _ = build_budget()
        output = (await agent.run(ctx)).output
        assert output.known_cost_subtotal == Decimal("10667.80")
        assert output.total_budget == Decimal("8000")
        assert output.status == "OVER_BUDGET" and output.estimated_total is None
        ctx.budget_breakdown = output.model_dump(mode="json")
        assert await agent.propose_budget_revision(ctx) is None
        result = (await OptimizationAgent(settings=Settings(_env_file=None, GEMINI_API_KEY=None)).run(ctx)).output
        assert result.review_status == "INSUFFICIENT_DATA" and not result.approved
        assert not any(issue.code == "missing_or_invalid_budget" for issue in result.issues)
    asyncio.run(run())


def test_identical_budget_proposal_stops_after_one_attempt():
    class RepeatingBudget(FakeAgent):
        async def propose_budget_revision(self, context):
            return {"replacement_items": {}, "removed_other_indexes": [0]}
    ctx = context()
    agent, _ = build_budget()
    original = asyncio.run(agent.run(ctx)).output
    repeating = RepeatingBudget("budget", original)
    result, agents = run_workflow(ctx, repeating, limit=5)
    assert result["execution_status"] == "REVISION_REQUIRED"
    assert result["revision_count"] == 1
    assert len(repeating.calls) == len(agents.itinerary.calls) == 2


def test_reconciled_total_is_required_for_approval_and_for_reduction():
    ctx = context()
    agent, _ = build_budget()
    output = asyncio.run(agent.run(ctx)).output
    output.known_cost_subtotal = Decimal("1")
    ctx.budget_breakdown = output.model_dump(mode="json")
    assert reconciled_snapshot(ctx) is None
    state = {"request": ctx.request, "normalized_requirements": ctx.normalized_requirements,
             "budget_breakdown": output}
    assert _budget_safety_status(state)[0] == "INSUFFICIENT_DATA"


def test_gemini_summary_cannot_claim_supplied_budget_is_missing():
    class IncorrectNarrative:
        async def generate_structured(self, *args, **kwargs):
            return OptimizationNarrative(summary="The user budget is missing.")
    async def run():
        ctx = context()
        agent, _ = build_budget()
        ctx.budget_breakdown = (await agent.run(ctx)).output.model_dump(mode="json")
        result = (await OptimizationAgent(settings=Settings(_env_file=None, GEMINI_API_KEY=None),
                                         gemini_service=IncorrectNarrative()).run(ctx)).output
        assert result.qualitative_summary is None
        assert any("contradicted" in warning for warning in result.warnings)
    asyncio.run(run())


@pytest.mark.parametrize("limit, expected", [(1, "REVISION_REQUIRED"), (2, "COMPLETED")])
def test_budget_and_evidence_revisions_share_limit_and_keep_reduced_costs(limit, expected):
    from app.schemas.optimization import OptimizationIssue, OptimizationReview
    blocker = OptimizationReview(approved=False, review_status="REVISION_REQUIRED", issues=[
        OptimizationIssue(code="unverified_attraction_coordinates", severity="error",
            description="Test evidence issue", evidence_basis="Fixture evidence", recommended_action="Exclude the unsupported entry",
            blocks_approval=True, place_id="place-1")])
    reviewer = FakeAgent("optimization_review", lambda current:
        review() if "place-1" in current.normalized_requirements.constraints.get("excluded_attraction_ids", []) else blocker)
    agent, estimator = build_budget()
    result, agents = run_workflow(context(), agent, limit, reviewer)
    assert result["execution_status"] == expected
    assert result["revision_count"] == limit
    assert result["budget_breakdown"].estimated_total == Decimal("25000")
    assert estimator.calls == 1
    assert len([record for record in result["agent_history"] if record.agent_name == "budget"]) == 2
    if limit == 2:
        assert result["budget_revision"] is not None
        assert result["excluded_attraction_ids"] == ["place-1"]
        assert len(agents.itinerary.calls) == 3


@pytest.mark.parametrize("unsupported", ["expired", "ambiguous", "foreign_currency"])
def test_unsupported_reference_alternatives_are_not_applied(unsupported):
    ctx = context(optional=False)
    ctx.request.travel_style = ctx.normalized_requirements.travel_style = "balanced"
    data = reference_data()
    if unsupported == "expired":
        data["rates"][1].update(observed_on=str(date.today() - timedelta(days=60)),
                                 valid_until=str(date.today() - timedelta(days=1)))
    elif unsupported == "ambiguous":
        data["rates"].append(data["rates"][1] | {"id": "ambiguous-fixture"})
    else:
        data["rates"][1]["currency"] = "AED"
    agent, _ = build_budget(automatic=True, data=data)
    result, _ = run_workflow(ctx, agent)
    assert result["execution_status"] == "REVISION_REQUIRED"
    assert result["revision_count"] == 0


def test_incomplete_estimate_claims_from_gemini_are_discarded():
    class IncorrectNarrative:
        async def generate_structured(self, *args, **kwargs):
            return OptimizationNarrative(summary="Consider the travel preferences.", suggestions=["The estimate is complete."])
    async def run():
        ctx = context(flight_missing=True)
        agent, _ = build_budget()
        ctx.budget_breakdown = (await agent.run(ctx)).output.model_dump(mode="json")
        result = (await OptimizationAgent(settings=Settings(_env_file=None, GEMINI_API_KEY=None),
                                         gemini_service=IncorrectNarrative()).run(ctx)).output
        assert result.review_status == "INSUFFICIENT_DATA"
        assert not any(suggestion.recommendation == "The estimate is complete." for suggestion in result.suggestions)
        assert any("contradicted missing" in warning for warning in result.warnings)
    asyncio.run(run())
