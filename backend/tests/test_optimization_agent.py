import asyncio
from datetime import date, time
from decimal import Decimal
from typing import Any

import pytest

from app.agents.optimization_agent import OptimizationAgent
from app.core.config import Settings
from app.schemas.accommodation_transport import (
    AccommodationRecommendation,
    AccommodationTransportPlan,
    CostEstimate,
)
from app.schemas.agent_schemas import AgentContext, NormalizedTravelRequirements
from app.schemas.budget import (
    BudgetBreakdown,
    BudgetCategoryTotal,
    BudgetLineItem,
    BudgetReductionSuggestion,
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
from app.schemas.optimization import OptimizationNarrative
from app.services.gemini_service import GeminiModelUnavailableError

START = date(2027, 6, 1)
SOURCE_URL = "https://example.com/source"


def requirements(**overrides: Any) -> NormalizedTravelRequirements:
    values: dict[str, Any] = {
        "origin": "Example Origin",
        "destination": "Example City",
        "start_date": START,
        "end_date": date(2027, 6, 2),
        "travelers_count": 2,
        "total_budget": Decimal("25000.00"),
        "currency": "INR",
        "travel_duration_days": 2,
        "travel_style": "culture",
        "interests": ["history"],
        "constraints": {},
    }
    values.update(overrides)
    return NormalizedTravelRequirements.model_validate(values)


def source() -> SourceReference:
    return SourceReference(
        title="Example source",
        url=SOURCE_URL,
        topic="travel_tips",
    )


def research() -> DestinationResearch:
    return DestinationResearch(
        destination_name="Example City",
        latitude=12.0,
        longitude=77.0,
        attractions=[
            Attraction(
                place_id="place-1",
                name="History Museum",
                categories=["entertainment.museum"],
                latitude=12.1,
                longitude=77.1,
                formatted_address="Museum Road, Example City",
            ),
            Attraction(
                place_id="place-2",
                name="Heritage Gallery",
                categories=["entertainment.gallery"],
                latitude=12.2,
                longitude=77.2,
                formatted_address="Gallery Road, Example City",
            ),
        ],
        weather_summary=WeatherSummary(
            forecast_available=True,
            daily_forecasts=[],
        ),
        source_references=[source()],
    )


def cost(amount: str) -> CostEstimate:
    return CostEstimate(
        amount=Decimal(amount),
        currency="INR",
        classification="estimate",
        pricing_basis="Fictional user-supplied assumption",
    )


def accommodation_plan() -> AccommodationTransportPlan:
    return AccommodationTransportPlan(
        destination_name="Example City",
        currency="INR",
        accommodation=AccommodationRecommendation(
            recommended_types=["family_accommodation", "mid_range_hotel"],
            room_count=1,
            nights=1,
            assumed_occupancy_per_room=2,
            estimated_nightly_rate=cost("10000"),
            estimated_total_cost=cost("10000"),
        ),
        transport_options=[],
        accommodation_cost=cost("10000"),
        local_transport_cost=cost("2000"),
        intercity_transport_cost=cost("5000"),
        combined_estimated_cost=cost("17000"),
    )


def budget(
    *,
    status: str = "WITHIN_BUDGET",
    total: str = "25000",
    include_items: bool = True,
) -> BudgetBreakdown:
    amounts = {
        "accommodation": Decimal("10000"),
        "local_transport": Decimal("2000"),
        "intercity_transport": Decimal("5000"),
        "food": Decimal("6000"),
        "activities": Decimal("2000"),
    }
    items = (
        [
            BudgetLineItem(
                category=category,
                description=category.title(),
                amount=amount,
                currency="INR",
                classification="estimated",
                pricing_basis="Fictional planning assumption",
            )
            for category, amount in amounts.items()
        ]
        if include_items
        else []
    )
    category_totals = [
        BudgetCategoryTotal(
            category=category,
            known_subtotal=amount,
            currency="INR",
            complete=True,
            included=True,
            line_item_count=1 if include_items else 0,
        )
        for category, amount in amounts.items()
    ]
    return BudgetBreakdown(
        total_budget=Decimal("25000"),
        currency="INR",
        known_cost_subtotal=Decimal(total),
        estimated_total=Decimal(total),
        remaining_budget=Decimal("25000") - Decimal(total),
        over_budget_amount=max(Decimal("0"), Decimal(total) - Decimal("25000")),
        status=status,
        line_items=[
            item.model_copy(
                update={
                    "optional": item.category == "activities",
                }
            )
            for item in items
        ],
        category_totals=category_totals,
    )


def itinerary(
    *,
    second_day: date | None = date(2027, 6, 2),
    include_second_day: bool = True,
    budget_status: str = "WITHIN_BUDGET",
    total: str = "25000",
    activities: list[ItineraryActivity] | None = None,
) -> TripItinerary:
    def make_day(day: date, number: int, place_id: str) -> DailyItinerary:
        activity_list = activities if activities is not None else [
            ItineraryActivity(
                activity_type="attraction",
                title="History Museum" if place_id == "place-1" else "Heritage Gallery",
                description="Visit a researched cultural place.",
                start_time=time(10),
                end_time=time(11),
                duration_minutes=60,
                place_id=place_id,
                location=f"{place_id} road",
                latitude=12.1 if place_id == "place-1" else 12.2,
                longitude=77.1 if place_id == "place-1" else 77.2,
                coordinates_verified=True,
                source_references=[source()],
            )
        ]
        return DailyItinerary(
            date=day,
            day_number=number,
            theme="History and heritage",
            activities=activity_list,
            daily_cost=DailyCostEstimate(
                amount=Decimal("12500"),
                currency="INR",
                classification="estimated",
                allocation_basis="Equal allocation",
            ),
        )

    days = [make_day(START, 1, "place-1")]
    if include_second_day and second_day is not None:
        days.append(make_day(second_day, 2, "place-2"))
    return TripItinerary(
        destination_name="Example City",
        start_date=START,
        end_date=date(2027, 6, 2),
        currency="INR",
        days=days,
        source_references=[source()],
        estimated_total_cost=Decimal(total),
        budget_status=budget_status,
        requires_revision=budget_status == "OVER_BUDGET",
    )


def agent_context(
    *,
    req: NormalizedTravelRequirements | None = None,
    dest: DestinationResearch | None = None,
    accommodation: AccommodationTransportPlan | None = None,
    budget_result: BudgetBreakdown | None = None,
    trip: TripItinerary | None = None,
) -> AgentContext:
    return AgentContext(
        normalized_requirements=req or requirements(),
        destination_research=(
            dest or research()
        ).model_dump(mode="json"),
        accommodation_transport_plan=(
            accommodation or accommodation_plan()
        ).model_dump(mode="json"),
        budget_breakdown=(budget_result or budget()).model_dump(mode="json"),
        trip_itinerary=(trip or itinerary()).model_dump(mode="json"),
    )


class FakeGemini:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result or OptimizationNarrative(
            summary="Consider grouping related activities."
        )
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        response_model: type[OptimizationNarrative],
        **kwargs: Any,
    ) -> OptimizationNarrative:
        self.calls.append({"prompt": prompt, "model": response_model, **kwargs})
        if self.error:
            raise self.error
        return self.result


def build_agent(gemini: FakeGemini | None = None) -> OptimizationAgent:
    return OptimizationAgent(
        gemini_service=gemini,
        settings=Settings(_env_file=None),
    )


def test_valid_itinerary_is_approved_with_nonblocking_suggestions() -> None:
    async def run() -> None:
        result = await build_agent().run(agent_context())
        assert result.status == "completed"
        assert result.output.approved
        assert result.output.review_status == "APPROVED"
        assert result.output.budget_status == "WITHIN_BUDGET"
        assert any(issue.code == "missing_source_reference" for issue in result.output.issues) is False

    asyncio.run(run())


def test_exact_budget_is_approved_when_all_categories_reconcile() -> None:
    async def run() -> None:
        result = await build_agent().run(agent_context())
        assert result.output.approved
        assert result.output.budget_status == "WITHIN_BUDGET"
        assert result.output.estimated_savings is None

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        ("OVER_BUDGET", "budget_over_budget"),
        ("INCOMPLETE_ESTIMATE", "budget_incomplete_estimate"),
    ],
)
def test_over_budget_and_unknown_costs_block_approval(
    status: str,
    expected_code: str,
) -> None:
    async def run() -> None:
        current = budget(status=status, total="26000")
        if status == "INCOMPLETE_ESTIMATE":
            current.unpriced_components = ["food"]
        result = await build_agent().run(
            agent_context(budget_result=current)
        )
        assert not result.output.approved
        assert result.output.review_status == "REVISION_REQUIRED"
        assert any(issue.code == expected_code for issue in result.output.issues)

    asyncio.run(run())


def test_budget_and_itinerary_cost_mismatch_is_blocking() -> None:
    async def run() -> None:
        trip = itinerary(total="24999")
        result = await build_agent().run(agent_context(trip=trip))
        assert not result.output.approved
        assert any(
            issue.code == "itinerary_budget_total_mismatch"
            for issue in result.output.issues
        )

    asyncio.run(run())


def test_budget_arithmetic_and_required_category_gaps_block_approval() -> None:
    async def run() -> None:
        current = budget()
        current.known_cost_subtotal = Decimal("24000")
        current.estimated_total = Decimal("25000")
        result = await build_agent().run(
            agent_context(budget_result=current)
        )
        codes = {issue.code for issue in result.output.issues}
        assert not result.output.approved
        assert "budget_subtotal_mismatch" in codes

        incomplete = budget()
        incomplete.category_totals = [
            item
            for item in incomplete.category_totals
            if item.category != "food"
        ]
        result = await build_agent().run(
            agent_context(budget_result=incomplete)
        )
        assert any(
            issue.code == "budget_category_incomplete_food"
            for issue in result.output.issues
        )

    asyncio.run(run())


def test_missing_days_duplicate_days_and_out_of_range_dates_block() -> None:
    async def run() -> None:
        missing = itinerary(include_second_day=False)
        result = await build_agent().run(agent_context(trip=missing))
        assert not result.output.approved
        assert any(issue.code == "missing_day" for issue in result.output.issues)

        duplicate = itinerary()
        duplicate.days[1] = duplicate.days[1].model_copy(update={"date": START})
        result = await build_agent().run(agent_context(trip=duplicate))
        assert any(
            issue.code == "duplicate_day_date" for issue in result.output.issues
        )

        outside = itinerary()
        outside.days[1] = outside.days[1].model_copy(
            update={"date": date(2027, 6, 3)}
        )
        result = await build_agent().run(agent_context(trip=outside))
        assert any(
            issue.code == "day_outside_trip_dates" for issue in result.output.issues
        )

    asyncio.run(run())


def test_duplicate_attractions_block_unless_repeat_is_user_requested() -> None:
    async def run() -> None:
        trip = itinerary(include_second_day=False)
        repeated_day = trip.days[0].model_copy(
            update={
                "date": date(2027, 6, 2),
                "day_number": 2,
                "activities": [
                    trip.days[0].activities[0].model_copy(
                        update={"start_time": time(12), "end_time": time(13)}
                    )
                ],
            }
        )
        trip.days.append(repeated_day)
        trip.allow_repeat_attractions = False
        result = await build_agent().run(agent_context(trip=trip))
        assert any(
            issue.code == "duplicate_attraction" and issue.blocks_approval
            for issue in result.output.issues
        )

        trip.allow_repeat_attractions = True
        result = await build_agent().run(agent_context(trip=trip))
        assert not any(
            issue.code == "duplicate_attraction" and issue.blocks_approval
            for issue in result.output.issues
        )

    asyncio.run(run())


def test_overlapping_malformed_days_produce_structured_blocker() -> None:
    async def run() -> None:
        first = ItineraryActivity(
            activity_type="rest",
            title="Rest",
            description="Pause",
            start_time=time(10),
            end_time=time(11),
            duration_minutes=60,
        )
        second = first.model_copy(
            update={"title": "Second rest", "start_time": time(10, 30), "end_time": time(11, 30)}
        )
        day = DailyItinerary.model_construct(
            date=START,
            day_number=1,
            theme="Rest",
            activities=[first, second],
            daily_cost=DailyCostEstimate(),
        )
        trip = itinerary(include_second_day=False)
        trip.days = [day, trip.days[0].model_copy(update={"date": date(2027, 6, 2), "day_number": 2})]
        context = agent_context(trip=trip)
        context.trip_itinerary = trip.model_dump(mode="json")
        result = await build_agent().run(context)
        assert any(
            issue.code == "activity_overlap" and issue.blocks_approval
            for issue in result.output.issues
        )

    asyncio.run(run())


def test_invalid_duration_is_returned_as_structured_issue() -> None:
    async def run() -> None:
        activity = ItineraryActivity.model_construct(
            activity_type="rest",
            title="Bad duration",
            description="Pause",
            start_time=time(10),
            end_time=time(11),
            duration_minutes=30,
            source_references=[],
        )
        day = DailyItinerary.model_construct(
            date=START,
            day_number=1,
            theme="Rest",
            activities=[activity],
            daily_cost=DailyCostEstimate(),
        )
        trip = itinerary(include_second_day=False)
        trip.days = [
            day,
            trip.days[0].model_copy(
                update={"date": date(2027, 6, 2), "day_number": 2}
            ),
        ]
        context = agent_context(trip=trip)
        context.trip_itinerary = trip.model_dump(mode="json")
        result = await build_agent().run(context)
        assert any(
            issue.code == "invalid_activity_duration"
            for issue in result.output.issues
        )

    asyncio.run(run())


def test_missing_attraction_source_is_warning_and_invalid_id_blocks() -> None:
    async def run() -> None:
        trip = itinerary()
        trip.source_references = []
        for day in trip.days:
            for activity in day.activities:
                activity.source_references = []
        result = await build_agent().run(agent_context(trip=trip))
        assert any(
            issue.code == "missing_source_reference" for issue in result.output.issues
        )

        trip.days[0].activities[0].place_id = "invented-place"
        result = await build_agent().run(agent_context(trip=trip))
        assert any(
            issue.code == "unsupported_attraction_id" and issue.blocks_approval
            for issue in result.output.issues
        )

    asyncio.run(run())


def test_coordinate_mismatch_and_unsupported_citations_block_approval() -> None:
    async def run() -> None:
        trip = itinerary()
        trip.days[0].activities[0].latitude = 10
        trip.days[0].activities[0].source_references = [
            SourceReference(
                title="Invented citation",
                url="https://fake.example/not-researched",
                topic="general",
            )
        ]
        result = await build_agent().run(agent_context(trip=trip))
        codes = {issue.code for issue in result.output.issues}
        assert "attraction_coordinate_mismatch" in codes
        assert "unsupported_source_reference" in codes

    asyncio.run(run())


def test_unsupported_opening_hours_claim_blocks_approval() -> None:
    async def run() -> None:
        trip = itinerary()
        trip.days[0].activities[0].description = "The museum opens at 9 AM."
        result = await build_agent().run(agent_context(trip=trip))
        assert any(
            issue.code == "unsupported_opening_hours" and issue.blocks_approval
            for issue in result.output.issues
        )

    asyncio.run(run())


def test_missing_route_and_insufficient_verified_route_buffer_block() -> None:
    async def run() -> None:
        first = ItineraryActivity(
            activity_type="attraction",
            title="History Museum",
            description="Visit.",
            start_time=time(9),
            end_time=time(10),
            duration_minutes=60,
            place_id="place-1",
            latitude=12.1,
            longitude=77.1,
            coordinates_verified=True,
            source_references=[source()],
        )
        second = ItineraryActivity(
            activity_type="attraction",
            title="Heritage Gallery",
            description="Visit.",
            start_time=time(10, 15),
            end_time=time(11, 15),
            duration_minutes=60,
            place_id="place-2",
            latitude=12.2,
            longitude=77.2,
            coordinates_verified=True,
            source_references=[source()],
        )
        trip = itinerary(include_second_day=False)
        trip.days[0].activities = [first, second]
        context = agent_context(trip=trip)
        context.trip_itinerary = trip.model_dump(mode="json")
        result = await build_agent().run(context)
        assert any(
            issue.code == "route_duration_unverified" and issue.blocks_approval
            for issue in result.output.issues
        )

        second.travel_time_from_previous_minutes = Decimal("30")
        second.travel_time_verified = True
        second.travel_time_provider = "openrouteservice"
        context.trip_itinerary = trip.model_dump(mode="json")
        result = await build_agent().run(context)
        assert any(
            issue.code == "insufficient_route_buffer" and issue.blocks_approval
            for issue in result.output.issues
        )

    asyncio.run(run())


def test_accessibility_and_required_preferences_are_checked() -> None:
    async def run() -> None:
        req = requirements(
            constraints={
                "accessibility": "step-free access",
                "must_include": ["beach"],
            }
        )
        result = await build_agent().run(agent_context(req=req))
        codes = {issue.code for issue in result.output.issues}
        assert "accessibility_unverified" in codes
        assert "required_preference_unmet" in codes
        assert not result.output.approved

    asyncio.run(run())


def test_family_accommodation_preference_and_daily_activity_limit() -> None:
    async def run() -> None:
        req = requirements(
            constraints={"family_friendly": True, "max_daily_activities": 0}
        )
        plan = accommodation_plan()
        plan.accommodation.recommended_types = ["budget_hotel"]
        result = await build_agent().run(
            agent_context(req=req, accommodation=plan)
        )
        codes = {issue.code for issue in result.output.issues}
        assert "family_preference_unmet" in codes
        assert "daily_activity_limit_exceeded" in codes

    asyncio.run(run())


def test_verified_adverse_weather_flags_outdoor_activity() -> None:
    async def run() -> None:
        destination = research()
        destination.weather_summary.forecast_available = True
        from app.schemas.external import DailyWeather

        destination.weather_summary.daily_forecasts = [
            DailyWeather(
                date=START,
                precipitation_sum_millimeters=12,
                weather_code=61,
                condition="rain",
            )
        ]
        destination.attractions[0].name = "City Park"
        destination.attractions[0].categories = ["leisure.park"]
        trip = itinerary()
        trip.days[0].activities[0].title = "Open Air Observation Deck"
        result = await build_agent().run(
            agent_context(dest=destination, trip=trip)
        )
        assert any(
            issue.code == "weather_sensitive_activity"
            for issue in result.output.issues
        )

    asyncio.run(run())


def test_gemini_unavailable_or_malformed_does_not_change_approval() -> None:
    async def run() -> None:
        for gemini in (
            FakeGemini(error=GeminiModelUnavailableError("private provider detail")),
            FakeGemini(result={"summary": 12}),
        ):
            result = await build_agent(gemini).run(agent_context())
            assert result.output.approved
            assert result.output.review_status == "APPROVED"
            assert result.output.qualitative_summary is None
            assert "private provider detail" not in " ".join(result.output.warnings)
            assert any("Gemini review suggestions" in w for w in result.output.warnings)

    asyncio.run(run())


def test_gemini_cannot_override_over_budget_or_supply_savings() -> None:
    async def run() -> None:
        current = budget(status="OVER_BUDGET", total="26000")
        malicious = FakeGemini(
            OptimizationNarrative(
                summary="This trip is affordable and saves ₹500.",
                suggestions=["The hotel costs 100 and is cheaper."],
            )
        )
        result = await build_agent(malicious).run(
            agent_context(
                budget_result=current,
                trip=itinerary(
                    budget_status="OVER_BUDGET",
                    total="26000",
                ),
            )
        )
        assert not result.output.approved
        assert result.output.review_status == "REVISION_REQUIRED"
        assert result.output.budget_status == "OVER_BUDGET"
        assert result.output.estimated_savings is None
        assert result.output.qualitative_summary is None

    asyncio.run(run())


def test_grounded_budget_savings_are_decimal_and_only_with_pricing_basis() -> None:
    async def run() -> None:
        current = budget()
        current.line_items = [
            item.model_copy(
                update={
                    "optional": item.optional or item.category == "accommodation",
                }
            )
            for item in current.line_items
        ]
        current.reduction_suggestions = [
            BudgetReductionSuggestion(
                category="activities",
                recommendation="Remove explicitly optional activity.",
                estimated_savings=Decimal("125.35"),
                currency="INR",
                pricing_basis="User-priced optional activity item.",
            ),
            BudgetReductionSuggestion(
                category="accommodation",
                recommendation="Use a cheaper hotel.",
                estimated_savings=Decimal("900"),
                currency="INR",
            ),
            BudgetReductionSuggestion(
                category="intercity_transport",
                recommendation="Consider another currency.",
                estimated_savings=Decimal("500"),
                currency="USD",
                pricing_basis="Not comparable.",
            ),
            BudgetReductionSuggestion(
                category="accommodation",
                recommendation="Remove an explicitly optional accommodation expense.",
                estimated_savings=Decimal("100"),
                currency="INR",
                pricing_basis="User-priced optional accommodation item.",
            ),
        ]
        result = await build_agent().run(agent_context(budget_result=current))
        assert result.output.estimated_savings == Decimal("225.35")
        savings = [
            suggestion
            for suggestion in result.output.suggestions
            if suggestion.estimated_savings is not None
        ]
        assert len(savings) == 2
        assert sum(
            (item.estimated_savings or Decimal("0") for item in savings),
            Decimal("0"),
        ) == Decimal("225.35")

    asyncio.run(run())


def test_missing_upstream_outputs_return_structured_insufficient_data() -> None:
    async def run() -> None:
        result = await build_agent().run(
            AgentContext(normalized_requirements=requirements())
        )
        assert result.status == "needs_input"
        assert not result.output.approved
        assert result.output.review_status == "INSUFFICIENT_DATA"
        assert {
            "missing_destination_research",
            "missing_accommodation_and_transport_plan",
            "missing_budget_breakdown",
            "missing_trip_itinerary",
        }.issubset({issue.code for issue in result.output.issues})

    asyncio.run(run())


def test_incomplete_destination_research_and_no_gemini_credentials_are_safe() -> None:
    async def run() -> None:
        destination = research()
        trip = itinerary()
        trip.days[0].activities[0].place_id = "not-in-research"
        result = await build_agent().run(
            agent_context(dest=destination, trip=trip)
        )
        assert not result.output.approved
        assert any(
            issue.code == "unsupported_attraction_id"
            for issue in result.output.issues
        )
        assert result.output.source_references

    asyncio.run(run())
