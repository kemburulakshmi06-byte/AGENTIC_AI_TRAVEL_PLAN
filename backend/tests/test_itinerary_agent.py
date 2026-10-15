import asyncio
from datetime import date, time
from decimal import Decimal
from typing import Any, Literal, cast

import pytest
from pydantic import ValidationError

from app.agents.itinerary_agent import (
    ItineraryAgent,
    ItineraryDraft,
    ItineraryDraftDay,
    validate_trip_itinerary,
)
from app.core.config import Settings
from app.schemas.agent_schemas import AgentContext, NormalizedTravelRequirements
from app.schemas.budget import BudgetBreakdown
from app.schemas.destination_research import (
    Attraction,
    DestinationResearch,
    RecommendedActivity,
    SourceReference,
    WeatherSummary,
)
from app.schemas.external import DailyWeather, RouteResult
from app.schemas.itinerary import (
    DailyCostEstimate,
    DailyItinerary,
    ItineraryActivity,
    TripItinerary,
)
from app.services.gemini_service import GeminiModelUnavailableError
from app.tools.base import TravelAPIResponseError

START = date(2027, 5, 1)
_MISSING = object()


def requirements(**overrides: Any) -> NormalizedTravelRequirements:
    values: dict[str, Any] = {
        "origin": "Sample Origin",
        "destination": "Sample City",
        "start_date": START,
        "end_date": START,
        "travelers_count": 2,
        "total_budget": Decimal("25000"),
        "currency": "INR",
        "travel_duration_days": 1,
        "interests": ["history"],
        "constraints": {},
    }
    values.update(overrides)
    return NormalizedTravelRequirements.model_validate(values)


def attraction(
    place_id: str,
    name: str,
    categories: list[str] | None = None,
    latitude: float = 12.5,
    longitude: float = 77.5,
    verified: bool = True,
) -> Attraction:
    return Attraction(
        place_id=place_id,
        name=name,
        categories=categories or ["tourism.attraction"],
        latitude=latitude,
        longitude=longitude,
        formatted_address=f"{name}, Sample City",
        verified_provider_fact=verified,
    )


def research(
    attractions: list[Attraction] | None = None,
    weather: list[DailyWeather] | None = None,
    *,
    forecast_available: bool = True,
    limitation: str | None = None,
) -> DestinationResearch:
    sources = [
        SourceReference(
            title="Sample destination source",
            url="https://example.com/sample-city",
            topic="travel_tips",
        )
    ]
    recommended = [
        RecommendedActivity(
            title="Research-backed activity",
            description="A grounded suggestion.",
            related_place_ids=["place-1"],
            source_urls=["https://example.com/sample-city"],
        )
    ]
    return DestinationResearch(
        destination_name="Sample City",
        latitude=12.5,
        longitude=77.5,
        attractions=attractions
        if attractions is not None
        else [
            attraction("place-1", "History Museum", ["entertainment.museum"]),
            attraction("place-2", "City Park", ["leisure.park"], 12.6, 77.6),
        ],
        weather_summary=WeatherSummary(
            forecast_available=forecast_available,
            daily_forecasts=weather or [],
            limitation=limitation,
        ),
        recommended_activities=recommended,
        source_references=sources,
    )


def budget(
    status: str = "WITHIN_BUDGET",
    total: str | None = "25000",
) -> dict[str, Any]:
    return BudgetBreakdown(
        total_budget=Decimal("25000"),
        currency="INR",
        known_cost_subtotal=Decimal(total) if total else None,
        estimated_total=Decimal(total) if total else None,
        remaining_budget=Decimal("0") if total else None,
        over_budget_amount=Decimal("0") if total else None,
        status=status,
    ).model_dump(mode="json")


def context(
    req: NormalizedTravelRequirements | None = None,
    destination: DestinationResearch | None = None,
    budget_data: dict[str, Any] | None = _MISSING,  # type: ignore[assignment]
) -> AgentContext:
    return AgentContext(
        normalized_requirements=req or requirements(),
        destination_research=(destination or research()).model_dump(mode="json"),
        budget_breakdown=(
            budget()
            if budget_data is _MISSING
            else cast(dict[str, Any] | None, budget_data)
        ),
    )


def route_result(minutes: int = 25) -> RouteResult:
    return RouteResult(
        profile="walking",
        distance_meters=1200,
        duration_seconds=minutes * 60,
    )


class FakeRouting:
    def __init__(self, result: RouteResult | None | Exception = route_result()) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []

    async def calculate_route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        profile: Literal["driving", "walking", "cycling"] = "walking",
    ) -> RouteResult | None:
        self.calls.append((origin, destination, profile))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        return None


class FakeGemini:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result or ItineraryDraft(
            days=[
                ItineraryDraftDay(
                    date=START,
                    theme="History and local discovery",
                    attraction_place_ids=["place-1", "place-2"],
                )
            ]
        )
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        response_model: type[ItineraryDraft],
        **kwargs: Any,
    ) -> ItineraryDraft:
        self.calls.append(
            {"prompt": prompt, "response_model": response_model, **kwargs}
        )
        if self.error:
            raise self.error
        return self.result


def build_agent(
    *,
    routing: FakeRouting | None = None,
    gemini: FakeGemini | None = None,
) -> ItineraryAgent:
    return ItineraryAgent(
        routing=routing or FakeRouting(),
        gemini_service=gemini,
        settings=Settings(_env_file=None),
    )


def test_food_stop_is_a_located_meal_with_verified_route_and_unknown_price() -> None:
    async def run() -> None:
        routing = FakeRouting(route_result(25))
        dest = research(attractions=[
            attraction("sight", "Provider Memorial", ["tourism.sights.memorial"]),
            attraction("food", "Hotel Udupi Krishna", ["catering.restaurant"], 12.501, 77.501),
            attraction("road", "MDR48", ["catering.restaurant"]),
            attraction("stay", "Sample Stay", ["accommodation.hotel"]),
        ])
        result = await build_agent(routing=routing).run(context(requirements(interests=["culture"]), dest))
        located = [item for item in result.output.days[0].activities if item.place_id]
        assert [(item.place_id, item.activity_type) for item in located] == [("sight", "attraction"), ("food", "meal_break")]
        food = located[1]
        assert food.coordinates_verified and food.travel_time_verified
        assert food.travel_time_provider == "openrouteservice"
        assert food.estimated_cost is None and food.cost_classification == "unknown"
        assert "Meal availability and prices are not verified" in food.description
        assert "Confirm current opening hours" in food.description
        assert len(routing.calls) == 1
        food.coordinates_verified = False
        assert any(issue.code == "unverified_attraction_coordinates" and issue.place_id == "food"
                   for issue in validate_trip_itinerary(result.output))
    asyncio.run(run())


def test_valid_one_day_itinerary_includes_meal_rest_and_sources() -> None:
    async def run() -> None:
        result = await build_agent().run(context())
        itinerary = result.output
        assert result.status == "completed"
        assert len(itinerary.days) == 1
        assert itinerary.days[0].date == START
        assert itinerary.days[0].day_number == 1
        assert itinerary.days[0].theme
        assert any(a.activity_type == "meal_break" for a in itinerary.days[0].activities)
        assert any(a.activity_type == "rest" for a in itinerary.days[0].activities)
        attraction_activity = next(
            a for a in itinerary.days[0].activities if a.activity_type == "attraction"
        )
        assert attraction_activity.place_id == "place-1"
        assert attraction_activity.coordinates_verified
        assert attraction_activity.estimated_cost is None
        assert attraction_activity.cost_classification == "unknown"
        assert attraction_activity.duration_minutes == 90
        assert any(
            "Estimated visit duration" in warning
            and "opening hours were not verified" in warning
            for warning in attraction_activity.warnings
        )
        assert str(itinerary.source_references[0].url) == "https://example.com/sample-city"
        assert itinerary.estimated_total_cost == Decimal("25000")

    asyncio.run(run())


def test_workflow_excluded_attractions_are_not_scheduled() -> None:
    async def run() -> None:
        req = requirements(
            constraints={"excluded_attraction_ids": ["place-1"]}
        )
        result = await build_agent().run(context(req))
        scheduled_ids = {
            activity.place_id
            for day in result.output.days
            for activity in day.activities
            if activity.activity_type == "attraction"
        }
        assert "place-1" not in scheduled_ids
        assert "place-2" in scheduled_ids

    asyncio.run(run())


def test_multi_day_itinerary_represents_every_trip_date() -> None:
    async def run() -> None:
        end = date(2027, 5, 3)
        req = requirements(end_date=end, travel_duration_days=3)
        result = await build_agent().run(context(req))
        assert [day.date for day in result.output.days] == [
            date(2027, 5, 1),
            date(2027, 5, 2),
            date(2027, 5, 3),
        ]
        assert [day.day_number for day in result.output.days] == [1, 2, 3]
        assert not [
            issue
            for issue in result.output.validation_issues
            if issue.code == "missing_day"
        ]

    asyncio.run(run())


def test_invalid_and_missing_trip_dates_are_handled() -> None:
    async def run() -> None:
        reversed_dates = requirements().model_copy(
            update={"start_date": date(2027, 5, 2), "end_date": START}
        )
        invalid = await build_agent().run(context(reversed_dates))
        assert invalid.status == "failed"
        assert invalid.output.days == []
        assert any(
            issue.code == "invalid_trip_date_range"
            for issue in invalid.output.validation_issues
        )

        missing = requirements().model_copy(update={"start_date": None})
        needs_input = await build_agent().run(context(missing))
        assert needs_input.status == "needs_input"
        assert needs_input.output.days == []

    asyncio.run(run())


def test_deterministic_validator_catches_missing_duplicate_and_out_of_range_days() -> None:
    day = DailyItinerary(
        date=START,
        day_number=1,
        theme="Rest",
        daily_cost=DailyCostEstimate(),
    )
    itinerary = TripItinerary.model_construct(
        start_date=START,
        end_date=date(2027, 5, 2),
        days=[day, day.model_copy(update={"day_number": 2})],
    )
    issues = validate_trip_itinerary(itinerary)
    assert any(issue.code == "duplicate_day_date" for issue in issues)
    assert any(
        issue.code == "missing_day" and issue.date == date(2027, 5, 2)
        for issue in issues
    )


def test_repeat_attraction_is_allowed_only_when_explicitly_requested() -> None:
    async def run() -> None:
        gemini = FakeGemini(
            ItineraryDraft(
                days=[
                    ItineraryDraftDay(
                        date=START,
                        theme="History",
                        attraction_place_ids=["place-1"],
                    ),
                    ItineraryDraftDay(
                        date=date(2027, 5, 2),
                        theme="History revisit",
                        attraction_place_ids=["place-1"],
                    ),
                ]
            )
        )
        req = requirements(
            end_date=date(2027, 5, 2),
            travel_duration_days=2,
            constraints={"allow_repeat_attractions": True},
        )
        result = await build_agent(gemini=gemini).run(context(req))
        repeated = [
            activity
            for day in result.output.days
            for activity in day.activities
            if activity.place_id == "place-1"
        ]
        assert len(repeated) == 2
        assert not any(
            issue.code == "duplicate_attraction"
            for issue in result.output.validation_issues
        )

    asyncio.run(run())


def test_daily_schema_rejects_time_overlap_and_invalid_duration() -> None:
    activity = ItineraryActivity(
        activity_type="rest",
        title="First",
        description="Rest",
        start_time=time(10),
        end_time=time(11),
        duration_minutes=60,
    )
    overlap = activity.model_copy(
        update={"title": "Overlap", "start_time": time(10, 30), "end_time": time(11, 30)}
    )
    with pytest.raises(ValidationError, match="cannot overlap"):
        DailyItinerary(
            date=START,
            day_number=1,
            theme="Rest",
            activities=[activity, overlap],
            daily_cost=DailyCostEstimate(),
        )
    with pytest.raises(ValidationError, match="duration_minutes"):
        ItineraryActivity(
            activity_type="rest",
            title="Bad duration",
            description="Bad",
            start_time=time(10),
            end_time=time(11),
            duration_minutes=30,
        )


def test_deterministic_validator_flags_invalid_duration_and_route_claim() -> None:
    activity = ItineraryActivity.model_construct(
        activity_type="attraction",
        title="Museum",
        description="Visit",
        start_time=time(9),
        end_time=time(10),
        duration_minutes=30,
        place_id="place-1",
        coordinates_verified=True,
        latitude=1,
        longitude=1,
        travel_time_from_previous_minutes=Decimal("15"),
        travel_time_verified=True,
        travel_time_provider="invented-provider",
    )
    day = DailyItinerary.model_construct(
        date=START,
        day_number=1,
        theme="History",
        activities=[activity],
        daily_cost=DailyCostEstimate(),
    )
    itinerary = TripItinerary.model_construct(
        start_date=START,
        end_date=START,
        days=[day],
        budget_status="INCOMPLETE_ESTIMATE",
    )
    issues = validate_trip_itinerary(itinerary)
    assert any(issue.code == "invalid_activity_duration" for issue in issues)
    assert any(issue.code == "unsupported_travel_time_claim" for issue in issues)


def test_duplicate_research_attractions_are_removed() -> None:
    async def run() -> None:
        duplicate = attraction("place-1", "History Museum", ["entertainment.museum"])
        destination = research([duplicate, duplicate])
        result = await build_agent().run(context(destination=destination))
        attraction_items = [
            activity
            for day in result.output.days
            for activity in day.activities
            if activity.activity_type == "attraction"
        ]
        assert len(attraction_items) == 1
        assert any(issue.code == "duplicate_attraction" for issue in result.output.validation_issues)

    asyncio.run(run())


def test_interest_ranking_selects_matching_attraction_first() -> None:
    async def run() -> None:
        destination = research(
            [
                attraction("food-place", "Market", ["catering.market"]),
                attraction("history-place", "Historic Museum", ["entertainment.museum"]),
            ]
        )
        result = await build_agent().run(
            context(requirements(interests=["history"]), destination)
        )
        first = next(
            item
            for item in result.output.days[0].activities
            if item.activity_type == "attraction"
        )
        assert first.place_id == "history-place"

    asyncio.run(run())


def test_adverse_weather_prioritizes_provider_listed_indoor_attractions() -> None:
    async def run() -> None:
        destination = research(
            [
                attraction("park", "City Park", ["leisure.park"]),
                attraction("museum", "History Museum", ["entertainment.museum"]),
            ],
            [
                DailyWeather(
                    date=START,
                    weather_code=61,
                    condition="rain",
                    precipitation_sum_millimeters=8,
                )
            ],
        )
        result = await build_agent().run(context(destination=destination))
        first = next(
            item
            for item in result.output.days[0].activities
            if item.activity_type == "attraction"
        )
        assert first.place_id == "museum"
        assert result.output.days[0].weather_warning
        assert result.output.days[0].weather_condition == "rain"

    asyncio.run(run())


def test_unsupported_forecast_date_is_unknown_and_warned() -> None:
    async def run() -> None:
        destination = research(
            weather=[],
            forecast_available=False,
            limitation="Date outside forecast horizon",
        )
        result = await build_agent().run(context(destination=destination))
        assert result.output.days[0].weather_condition is None
        assert result.output.days[0].weather_warning is None
        assert any("outside forecast horizon" in w for w in result.output.warnings)
        assert any("no forecast is available" in w for w in result.output.warnings)

    asyncio.run(run())


def test_missing_verified_coordinates_skip_routes_and_are_flagged() -> None:
    async def run() -> None:
        raw_research = research().model_dump(mode="json")
        raw_research["attractions"][0]["latitude"] = None
        raw_research["attractions"][0]["longitude"] = None
        routing = FakeRouting()
        result = await build_agent(routing=routing).run(
            AgentContext(
                normalized_requirements=requirements(
                    end_date=date(2027, 5, 2), travel_duration_days=2
                ),
                destination_research=raw_research,
                budget_breakdown=budget(),
            )
        )
        assert routing.calls == []
        assert any("schema" in warning for warning in result.output.warnings)

    asyncio.run(run())


def test_route_duration_is_provider_grounded_and_used_in_schedule() -> None:
    async def run() -> None:
        route = FakeRouting(route_result(37))
        result = await build_agent(routing=route).run(context())
        items = [
            activity
            for activity in result.output.days[0].activities
            if activity.activity_type == "attraction"
        ]
        assert len(items) == 2
        assert route.calls == [
            ((12.5, 77.5), (12.6, 77.6), "walking")
        ]
        assert items[1].travel_time_from_previous_minutes == Decimal("37.00")
        assert items[1].travel_time_verified
        assert items[1].start_time >= items[0].end_time

    asyncio.run(run())


@pytest.mark.parametrize(
    "routing",
    [
        FakeRouting(result=None),
        FakeRouting(result=TravelAPIResponseError("ors", "authorization=do-not-leak")),
    ],
)
def test_missing_or_failed_route_is_unverified_and_sanitized(routing: FakeRouting) -> None:
    async def run() -> None:
        result = await build_agent(routing=routing).run(context())
        assert any("route verification" in warning for warning in result.output.warnings)
        assert any(
            issue.code == "route_duration_unavailable"
            for issue in result.output.validation_issues
        )
        assert "do-not-leak" not in " ".join(result.output.warnings)
        second = [
            activity
            for activity in result.output.days[0].activities
            if activity.activity_type == "attraction"
        ][1]
        assert second.travel_time_from_previous_minutes is None
        assert not second.travel_time_verified

    asyncio.run(run())


def test_unknown_activity_and_daily_expenses_are_not_zeroed() -> None:
    async def run() -> None:
        result = await build_agent().run(context(budget_data=None))
        attraction_item = next(
            item
            for item in result.output.days[0].activities
            if item.activity_type == "attraction"
        )
        assert attraction_item.estimated_cost is None
        assert attraction_item.cost_classification == "unknown"
        assert result.output.days[0].daily_cost.amount is None
        assert result.output.estimated_total_cost is None
        assert result.output.budget_status == "INCOMPLETE_ESTIMATE"

    asyncio.run(run())


def test_over_budget_itinerary_requires_revision() -> None:
    async def run() -> None:
        result = await build_agent().run(
            context(budget_data=budget("OVER_BUDGET", "30000"))
        )
        assert result.status == "needs_input"
        assert result.output.requires_revision
        assert result.output.budget_status == "OVER_BUDGET"
        assert any(issue.code == "budget_overrun" for issue in result.output.validation_issues)

    asyncio.run(run())


def test_exact_budget_result_remains_within_and_is_not_overridden() -> None:
    async def run() -> None:
        exact = budget("WITHIN_BUDGET", "25000")
        result = await build_agent().run(context(budget_data=exact))
        assert result.output.budget_status == "WITHIN_BUDGET"
        assert result.output.estimated_total_cost == Decimal("25000")
        assert not result.output.requires_revision
        assert all(
            day.daily_cost.amount is not None
            for day in result.output.days
        )

    asyncio.run(run())


def test_budget_currency_or_total_mismatch_is_not_trusted() -> None:
    async def run() -> None:
        inconsistent = BudgetBreakdown(
            total_budget=Decimal("100"),
            currency="USD",
            known_cost_subtotal=Decimal("50"),
            estimated_total=Decimal("50"),
            remaining_budget=Decimal("50"),
            over_budget_amount=Decimal("0"),
            status="WITHIN_BUDGET",
        ).model_dump(mode="json")
        result = await build_agent().run(context(budget_data=inconsistent))
        assert result.output.budget_status == "INCOMPLETE_ESTIMATE"
        assert result.output.estimated_total_cost is None
        assert any(
            issue.code == "budget_input_mismatch"
            for issue in result.output.validation_issues
        )

    asyncio.run(run())


def test_gemini_unavailable_and_malformed_json_fall_back_safely() -> None:
    async def run() -> None:
        for gemini in (
            FakeGemini(error=GeminiModelUnavailableError("private provider content")),
            FakeGemini(result={"days": [{"date": "not-a-date"}]}),
        ):
            result = await build_agent(gemini=gemini).run(context())
            assert len(result.output.days) == 1
            assert any("Gemini itinerary suggestions" in w for w in result.output.warnings)
            assert "private provider content" not in " ".join(result.output.warnings)

    asyncio.run(run())


def test_gemini_fabricated_attraction_and_out_of_range_date_are_rejected() -> None:
    async def run() -> None:
        gemini = FakeGemini(
            ItineraryDraft(
                days=[
                    ItineraryDraftDay(
                        date=START,
                        theme="Historical visit",
                        attraction_place_ids=["fake-place", "place-1"],
                    ),
                    ItineraryDraftDay(
                        date=date(2027, 6, 1),
                        theme="Extra",
                        attraction_place_ids=["place-2"],
                    ),
                ]
            )
        )
        result = await build_agent(gemini=gemini).run(context())
        ids = [
            activity.place_id
            for day in result.output.days
            for activity in day.activities
            if activity.activity_type == "attraction"
        ]
        assert "fake-place" not in ids
        assert any(
            issue.code == "fabricated_attraction_rejected"
            for issue in result.output.validation_issues
        )
        assert any(
            issue.code == "activity_outside_trip_dates"
            for issue in result.output.validation_issues
        )

    asyncio.run(run())


def test_gemini_untrusted_schedule_fields_are_rejected_by_schema() -> None:
    async def run() -> None:
        result = await build_agent(
            gemini=FakeGemini(
                result={
                    "days": [
                        {
                            "date": START.isoformat(),
                            "theme": "Theme",
                            "attraction_place_ids": ["place-1"],
                            "opening_hours": "9-5",
                            "coordinates": [1, 2],
                        }
                    ]
                }
            )
        ).run(context())
        assert any("unavailable or invalid" in warning for warning in result.output.warnings)
        assert all(
            activity.location != "1, 2"
            for day in result.output.days
            for activity in day.activities
        )

    asyncio.run(run())


def test_source_urls_are_preserved_only_from_destination_research() -> None:
    async def run() -> None:
        destination = research()
        result = await build_agent().run(context(destination=destination))
        place = next(
            activity
            for activity in result.output.days[0].activities
            if activity.place_id == "place-1"
        )
        assert [str(source.url) for source in place.source_references] == [
            "https://example.com/sample-city"
        ]

    asyncio.run(run())


def test_no_gemini_credentials_are_needed_for_deterministic_itinerary() -> None:
    async def run() -> None:
        result = await build_agent().run(context())
        assert result.output.days
        assert any("deterministic interest-based" in w for w in result.output.warnings)

    asyncio.run(run())


def test_unknown_route_profile_falls_back_to_supported_mode() -> None:
    async def run() -> None:
        routing = FakeRouting()
        req = requirements(constraints={"itinerary_routing_mode": "submarine"})
        result = await build_agent(routing=routing).run(context(req))
        assert routing.calls[0][2] == "walking"
        assert any("unsupported" in warning for warning in result.output.warnings)

    asyncio.run(run())


def test_trip_with_no_verified_attractions_has_rest_only_placeholder_day() -> None:
    async def run() -> None:
        result = await build_agent().run(
            context(destination=research(attractions=[]))
        )
        day = result.output.days[0]
        assert not any(a.activity_type == "attraction" for a in day.activities)
        assert any(a.activity_type == "rest" for a in day.activities)
        assert any("No verified attraction" in warning for warning in day.warnings)

    asyncio.run(run())
