from __future__ import annotations

import asyncio
from datetime import date, time
from decimal import Decimal
from typing import Any, Callable

import pytest
from pydantic import BaseModel

from app.schemas.accommodation_transport import (
    AccommodationTransportPlan,
    AccommodationRecommendation,
    CostEstimate,
)
from app.schemas.agent_schemas import (
    AgentContext,
    AgentResult,
    NormalizedTravelRequirements,
    TravelRequest,
)
from app.schemas.budget import (
    BudgetBreakdown,
    BudgetCategoryTotal,
    BudgetLineItem,
)
from app.schemas.destination_research import (
    Attraction,
    DestinationResearch,
    SourceReference,
    WeatherSummary,
)
from app.schemas.itinerary import (
    DailyCostEstimate,
    DailyItinerary,
    ItineraryActivity,
    TripItinerary,
)
from app.schemas.optimization import OptimizationIssue, OptimizationReview
from app.workflows.state import AgentExecutionRecord
from app.workflows.travel_graph import TravelAgentSet, plan_trip

START = date(2027, 6, 1)
SOURCE_URL = "https://example.com/guide"


def request() -> TravelRequest:
    return TravelRequest(
        origin="Example Origin",
        destination="Example City",
        start_date=START,
        end_date=date(2027, 6, 2),
        travelers_count=2,
        total_budget=Decimal("25000"),
        currency="INR",
        travel_style="culture",
        interests=["history"],
    )


def requirements() -> NormalizedTravelRequirements:
    return NormalizedTravelRequirements(
        **request().model_dump(exclude={"user_message"}),
        travel_duration_days=2,
    )


def source() -> SourceReference:
    return SourceReference(
        title="Example guide",
        url=SOURCE_URL,
        topic="travel_tips",
    )


def research() -> DestinationResearch:
    return DestinationResearch(
        destination_name="Example City",
        latitude=12,
        longitude=77,
        attractions=[
            Attraction(
                place_id="place-1",
                name="History Museum",
                categories=["entertainment.museum"],
                latitude=12.1,
                longitude=77.1,
            ),
            Attraction(
                place_id="place-2",
                name="Heritage Gallery",
                categories=["entertainment.gallery"],
                latitude=12.2,
                longitude=77.2,
            ),
        ],
        weather_summary=WeatherSummary(forecast_available=True),
        source_references=[source()],
    )


def cost(amount: str) -> CostEstimate:
    return CostEstimate(
        amount=Decimal(amount),
        currency="INR",
        classification="estimate",
        pricing_basis="Fictional test assumption",
    )


def accommodation() -> AccommodationTransportPlan:
    return AccommodationTransportPlan(
        destination_name="Example City",
        currency="INR",
        accommodation=AccommodationRecommendation(
            recommended_types=["mid_range_hotel"],
            assumed_occupancy_per_room=2,
            estimated_nightly_rate=cost("5000"),
            estimated_total_cost=cost("10000"),
        ),
        accommodation_cost=cost("10000"),
        local_transport_cost=cost("2000"),
        intercity_transport_cost=cost("5000"),
        combined_estimated_cost=cost("17000"),
    )


def budget(status: str = "WITHIN_BUDGET") -> BudgetBreakdown:
    amounts = {
        "accommodation": Decimal("10000"),
        "local_transport": Decimal("2000"),
        "intercity_transport": Decimal("5000"),
        "food": Decimal("6000"),
        "activities": Decimal("2000"),
    }
    return BudgetBreakdown(
        total_budget=Decimal("25000"),
        currency="INR",
        known_cost_subtotal=Decimal("25000"),
        estimated_total=Decimal("25000"),
        remaining_budget=Decimal("0"),
        over_budget_amount=Decimal("0"),
        status=status,
        line_items=[
            BudgetLineItem(
                category=category,
                description=category.title(),
                amount=amount,
                currency="INR",
                classification="estimated",
                pricing_basis="Fictional test assumption",
            )
            for category, amount in amounts.items()
        ],
        category_totals=[
            BudgetCategoryTotal(
                category=category,
                known_subtotal=amount,
                currency="INR",
                complete=status == "WITHIN_BUDGET",
                included=True,
                line_item_count=1,
            )
            for category, amount in amounts.items()
        ],
        unpriced_components=["food"] if status == "INCOMPLETE_ESTIMATE" else [],
    )


def itinerary(excluded: set[str] | None = None) -> TripItinerary:
    excluded = excluded or set()
    days = []
    for offset, place_id in enumerate(("place-1", "place-2")):
        if place_id in excluded:
            activity_list: list[ItineraryActivity] = []
        else:
            activity_list = [
                ItineraryActivity(
                    activity_type="attraction",
                    title="History Museum" if place_id == "place-1" else "Heritage Gallery",
                    description="Visit a verified cultural attraction.",
                    start_time=time(10),
                    end_time=time(11),
                    duration_minutes=60,
                    place_id=place_id,
                    latitude=12.1 if place_id == "place-1" else 12.2,
                    longitude=77.1 if place_id == "place-1" else 77.2,
                    coordinates_verified=True,
                    source_references=[source()],
                )
            ]
        days.append(
            DailyItinerary(
                date=date(2027, 6, 1 + offset),
                day_number=offset + 1,
                theme="History",
                activities=activity_list,
                daily_cost=DailyCostEstimate(
                    amount=Decimal("12500"),
                    currency="INR",
                    classification="estimated",
                    allocation_basis="Equal cost allocation",
                ),
            )
        )
    return TripItinerary(
        destination_name="Example City",
        start_date=START,
        end_date=date(2027, 6, 2),
        currency="INR",
        days=days,
        source_references=[source()],
        estimated_total_cost=Decimal("25000"),
        budget_status="WITHIN_BUDGET",
    )


def review(approved: bool = True) -> OptimizationReview:
    return OptimizationReview(
        approved=approved,
        review_status="APPROVED" if approved else "REVISION_REQUIRED",
        budget_status="WITHIN_BUDGET",
        currency="INR",
        source_references=[source()],
    )


class FakeAgent:
    def __init__(
        self,
        name: str,
        output: BaseModel | Callable[[AgentContext], BaseModel],
        *,
        status: str = "completed",
        error: Exception | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self.agent_name = name
        self.output = output
        self.status = status
        self.error = error
        self.warnings = warnings or []
        self.calls: list[AgentContext] = []
        self.close_calls = 0

    async def run(self, context: AgentContext) -> AgentResult[Any]:
        self.calls.append(context)
        if self.error:
            raise self.error
        output = self.output(context) if callable(self.output) else self.output
        return AgentResult(
            agent_name=self.agent_name,
            status=self.status,
            output=output,
            warnings=self.warnings,
        )

    async def aclose(self) -> None:
        self.close_calls += 1


def agent_set(
    *,
    research_agent: FakeAgent | None = None,
    budget_agent: FakeAgent | None = None,
    itinerary_agent: FakeAgent | None = None,
    review_agent: FakeAgent | None = None,
    requirements_agent: FakeAgent | None = None,
    accommodation_agent: FakeAgent | None = None,
) -> TravelAgentSet:
    return TravelAgentSet(
        requirements=requirements_agent
        or FakeAgent("requirements", requirements()),
        destination_research=research_agent
        or FakeAgent("destination_research", research()),
        accommodation_transport=accommodation_agent
        or FakeAgent("accommodation_transport", accommodation()),
        budget=budget_agent or FakeAgent("budget", budget()),
        itinerary=itinerary_agent or FakeAgent("itinerary", itinerary()),
        optimization_review=review_agent
        or FakeAgent("optimization_review", review()),
    )


def test_agents_run_in_order_and_successful_state_preserves_sources() -> None:
    async def run() -> None:
        agents = agent_set()
        result = await plan_trip(request(), agents=agents)
        assert result["execution_status"] == "COMPLETED"
        assert [record.agent_name for record in result["agent_history"]] == [
            "requirements",
            "destination_research",
            "accommodation_transport",
            "budget",
            "itinerary",
            "optimization_review",
        ]
        assert all(
            isinstance(record, AgentExecutionRecord)
            and record.duration_ms >= 0
            for record in result["agent_history"]
        )
        assert [str(ref.url) for ref in result["source_references"]] == [
            SOURCE_URL
        ]
        assert all(agent.close_calls == 1 for agent in agents.all())

    asyncio.run(run())


def test_invalid_request_terminates_before_agents() -> None:
    async def run() -> None:
        result = await plan_trip({"total_budget": "-2"})
        assert result["execution_status"] == "INVALID_REQUEST"
        assert result["agent_history"] == []
        assert "2" not in " ".join(result["errors"])

    asyncio.run(run())


def test_missing_requirements_terminate_as_invalid_request() -> None:
    async def run() -> None:
        req_agent = FakeAgent(
            "requirements",
            requirements().model_copy(
                update={"missing_required_fields": ["destination"]}
            ),
            status="needs_input",
        )
        agents = agent_set(requirements_agent=req_agent)
        result = await plan_trip(request(), agents=agents)
        assert result["execution_status"] == "INVALID_REQUEST"
        assert [item.agent_name for item in result["agent_history"]] == [
            "requirements"
        ]

    asyncio.run(run())


def test_provider_failure_is_sanitized_and_stops_workflow() -> None:
    async def run() -> None:
        agents = agent_set(
            research_agent=FakeAgent(
                "destination_research",
                research(),
                error=RuntimeError("API_KEY=super-secret-value"),
            )
        )
        result = await plan_trip(request(), agents=agents)
        assert result["execution_status"] == "FAILED"
        assert "super-secret-value" not in " ".join(result["errors"])
        assert [record.agent_name for record in result["agent_history"]] == [
            "requirements",
            "destination_research",
        ]

    asyncio.run(run())


def test_gemini_unavailable_warning_does_not_fail_deterministic_workflow() -> None:
    async def run() -> None:
        agents = agent_set(
            itinerary_agent=FakeAgent(
                "itinerary",
                itinerary(),
                warnings=["Gemini unavailable; deterministic schedule used."],
            )
        )
        result = await plan_trip(request(), agents=agents)
        assert result["execution_status"] == "COMPLETED"
        assert any("Gemini unavailable" in item for item in result["warnings"])

    asyncio.run(run())


def test_over_budget_cannot_be_approved_by_review_agent() -> None:
    async def run() -> None:
        over = budget()
        over.total_budget = Decimal("20000")
        over.remaining_budget = Decimal("-5000")
        over.over_budget_amount = Decimal("5000")
        over.status = "OVER_BUDGET"
        agents = agent_set(
            budget_agent=FakeAgent("budget", over),
            review_agent=FakeAgent("optimization_review", review(approved=True)),
        )
        result = await plan_trip(request(), agents=agents)
        assert result["execution_status"] == "REVISION_REQUIRED"
        assert result["optimization_review"].approved is False

    asyncio.run(run())


def test_unknown_price_finishes_as_insufficient_data() -> None:
    async def run() -> None:
        unknown = budget("INCOMPLETE_ESTIMATE")
        agents = agent_set(
            budget_agent=FakeAgent("budget", unknown),
            review_agent=FakeAgent("optimization_review", review(approved=True)),
        )
        result = await plan_trip(request(), agents=agents)
        assert result["execution_status"] == "INSUFFICIENT_DATA"
        assert result["optimization_review"].approved is False

    asyncio.run(run())


def test_unresolved_destination_stops_before_cost_and_itinerary_agents() -> None:
    async def run() -> None:
        agents = agent_set(
            research_agent=FakeAgent(
                "destination_research",
                DestinationResearch(destination_name="America"),
                status="needs_input",
                warnings=["No verified destination coordinates were returned."],
            )
        )
        result = await plan_trip(request(), agents=agents)
        assert result["execution_status"] == "INSUFFICIENT_DATA"
        assert result["current_stage"] == "destination_research"
        assert "clarification_required" in result["errors"][0]
        assert "No verified destination coordinates" in result["errors"][0]
        assert not agents.accommodation_transport.calls
        assert not agents.itinerary.calls
        assert not agents.optimization_review.calls

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["completed", "insufficient_data", "failed"])
def test_real_graph_outcome_reaches_planning_service_status(outcome: str) -> None:
    from uuid import uuid4
    from app.core.config import Settings
    from app.services.trip_planning_service import TripPlanningService, _database_status

    async def run() -> None:
        agents = agent_set()
        if outcome == "insufficient_data":
            agents = agent_set(budget_agent=FakeAgent("budget", budget("INCOMPLETE_ESTIMATE")))
        elif outcome == "failed":
            agents = agent_set(research_agent=FakeAgent("destination_research", research(), error=RuntimeError("API_KEY=private")))

        class StatusRecorder:
            def __init__(self):
                self.updates = []

            async def update_task_status(self, trip_id, **values):
                self.updates.append(values)

        async def workflow(raw_request, **kwargs):
            return await plan_trip(raw_request, agents=agents, **kwargs)

        recorder = StatusRecorder()
        service = TripPlanningService(
            session_factory=lambda: None, task_status=recorder,
            workflow=workflow, settings=Settings(_env_file=None),
        )
        persisted = []
        service._mark_running = lambda trip_id: True
        service._persist_result = lambda trip_id, state: persisted.append(state) or True
        await service.run_trip(uuid4(), request())
        assert len(persisted) == 1
        assert _database_status(persisted[0]["execution_status"]) == outcome
        assert recorder.updates[-1]["status"] == outcome
        assert recorder.updates[-1]["progress_percentage"] == 100
        assert "private" not in str(recorder.updates)
        if outcome == "completed":
            assert persisted[0]["optimization_review"].approved is True
            assert len(persisted[0]["trip_itinerary"].days) == 2
        elif outcome == "insufficient_data":
            assert persisted[0]["optimization_review"].approved is False
        else:
            assert recorder.updates[-1]["error_message"]

    asyncio.run(run())


def test_actionable_evidence_issue_revises_once_then_completes() -> None:
    async def run() -> None:
        first_review = OptimizationReview(
            approved=False,
            review_status="REVISION_REQUIRED",
            budget_status="WITHIN_BUDGET",
            currency="INR",
            issues=[
                OptimizationIssue(
                    code="unverified_attraction_coordinates",
                    severity="error",
                    description="Attraction coordinates are not verified.",
                    evidence_basis="The activity has unverified coordinates.",
                    recommended_action="Use a verified attraction.",
                    blocks_approval=True,
                    place_id="place-1",
                )
            ],
        )
        itinerary_agent = FakeAgent(
            "itinerary",
            lambda context: itinerary(
                set(
                    context.normalized_requirements.constraints.get(
                        "excluded_attraction_ids", []
                    )
                )
            ),
        )
        review_agent = FakeAgent(
            "optimization_review",
            lambda context: (
                first_review
                if "place-1"
                not in context.normalized_requirements.constraints.get(
                    "excluded_attraction_ids", []
                )
                else review()
            ),
        )
        agents = agent_set(
            itinerary_agent=itinerary_agent,
            review_agent=review_agent,
        )
        result = await plan_trip(request(), agents=agents)
        assert result["execution_status"] == "COMPLETED"
        assert result["revision_count"] == 1
        assert len(itinerary_agent.calls) == 2
        assert "place-1" in itinerary_agent.calls[1].normalized_requirements.constraints[
            "excluded_attraction_ids"
        ]
        assert len([item for item in agents.all() if item.agent_name == "budget"][0].calls) == 1

    asyncio.run(run())


def test_no_actionable_revision_terminates_without_retry() -> None:
    async def run() -> None:
        blocker = OptimizationReview(
            approved=False,
            review_status="REVISION_REQUIRED",
            budget_status="WITHIN_BUDGET",
            issues=[
                OptimizationIssue(
                    code="route_feasibility_unresolved",
                    severity="error",
                    description="Route duration is unavailable.",
                    evidence_basis="No verified route duration.",
                    recommended_action="Resolve route feasibility.",
                    blocks_approval=True,
                )
            ],
        )
        itinerary_agent = FakeAgent("itinerary", itinerary())
        agents = agent_set(
            itinerary_agent=itinerary_agent,
            review_agent=FakeAgent("optimization_review", blocker),
        )
        result = await plan_trip(request(), agents=agents)
        assert result["execution_status"] == "REVISION_REQUIRED"
        assert result["revision_count"] == 0
        assert len(itinerary_agent.calls) == 1

    asyncio.run(run())


def test_revision_limit_prevents_retry() -> None:
    async def run() -> None:
        blocker = OptimizationReview(
            approved=False,
            review_status="REVISION_REQUIRED",
            budget_status="WITHIN_BUDGET",
            issues=[
                OptimizationIssue(
                    code="unverified_attraction_coordinates",
                    severity="error",
                    description="Coordinates are not verified.",
                    evidence_basis="No verified coordinates.",
                    recommended_action="Use a verified attraction.",
                    blocks_approval=True,
                    place_id="place-1",
                )
            ],
        )
        itinerary_agent = FakeAgent("itinerary", itinerary())
        agents = agent_set(
            itinerary_agent=itinerary_agent,
            review_agent=FakeAgent("optimization_review", blocker),
        )
        result = await plan_trip(request(), agents=agents, max_revisions=0)
        assert result["execution_status"] == "REVISION_REQUIRED"
        assert result["revision_count"] == 0
        assert len(itinerary_agent.calls) == 1

    asyncio.run(run())


def test_repeated_identical_revision_does_not_loop() -> None:
    async def run() -> None:
        blocker = OptimizationReview(
            approved=False,
            review_status="REVISION_REQUIRED",
            budget_status="WITHIN_BUDGET",
            issues=[
                OptimizationIssue(
                    code="unverified_attraction_coordinates",
                    severity="error",
                    description="Coordinates are not verified.",
                    evidence_basis="No verified coordinates.",
                    recommended_action="Use a verified attraction.",
                    blocks_approval=True,
                    place_id="place-1",
                )
            ],
        )
        itinerary_agent = FakeAgent("itinerary", itinerary())
        review_agent = FakeAgent("optimization_review", blocker)
        result = await plan_trip(
            request(),
            agents=agent_set(
                itinerary_agent=itinerary_agent,
                review_agent=review_agent,
            ),
        )
        assert result["execution_status"] == "REVISION_REQUIRED"
        assert result["revision_count"] == 1
        assert len(itinerary_agent.calls) == 2
        assert len(review_agent.calls) == 2

    asyncio.run(run())


def test_cost_changing_revision_route_revalidates_budget() -> None:
    from app.workflows.travel_graph import _after_revision

    assert (
        _after_revision(
            {
                "execution_status": "RUNNING",
                "budget_inputs_changed": True,
            }
        )
        == "budget"
    )


def test_agent_result_validation_failure_is_sanitized() -> None:
    class MalformedAgent(FakeAgent):
        async def run(self, context: AgentContext) -> Any:
            self.calls.append(context)
            return {
                "agent_name": self.agent_name,
                "status": "completed",
                "output": {"private_token": "do-not-leak"},
            }

    async def run() -> None:
        agents = agent_set(
            research_agent=MalformedAgent(
                "destination_research",
                research(),
            )
        )
        result = await plan_trip(request(), agents=agents)
        assert result["execution_status"] == "FAILED"
        assert "do-not-leak" not in " ".join(result["errors"])

    asyncio.run(run())


def test_agent_timeout_is_reported_without_provider_details() -> None:
    class SlowAgent(FakeAgent):
        async def run(self, context: AgentContext) -> AgentResult[Any]:
            self.calls.append(context)
            await asyncio.sleep(0.05)
            return AgentResult(
                agent_name=self.agent_name,
                status="completed",
                output=self.output,
            )

    async def run() -> None:
        agents = agent_set(
            research_agent=SlowAgent("destination_research", research())
        )
        result = await plan_trip(
            request(),
            agents=agents,
            agent_timeout_seconds=0.001,
        )
        assert result["execution_status"] == "FAILED"
        assert result["agent_history"][-1].status == "timed_out"

    asyncio.run(run())


def test_workflow_cancellation_propagates_and_closes_agents() -> None:
    class WaitingAgent(FakeAgent):
        async def run(self, context: AgentContext) -> AgentResult[Any]:
            self.calls.append(context)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def run() -> None:
        agents = agent_set(
            requirements_agent=WaitingAgent("requirements", requirements())
        )
        task = asyncio.create_task(plan_trip(request(), agents=agents))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(agent.close_calls == 1 for agent in agents.all())

    asyncio.run(run())


def test_result_state_has_typed_outputs_and_does_not_mutate_original_budget() -> None:
    async def run() -> None:
        original = request()
        agents = agent_set()
        result = await plan_trip(original, agents=agents)
        assert original.total_budget == Decimal("25000")
        assert isinstance(result["normalized_requirements"], NormalizedTravelRequirements)
        assert isinstance(result["destination_research"], DestinationResearch)
        assert isinstance(
            result["accommodation_transport_plan"], AccommodationTransportPlan
        )
        assert isinstance(result["budget_breakdown"], BudgetBreakdown)
        assert isinstance(result["trip_itinerary"], TripItinerary)
        assert isinstance(result["optimization_review"], OptimizationReview)

    asyncio.run(run())
