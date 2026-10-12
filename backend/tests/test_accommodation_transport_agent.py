import asyncio
from datetime import date
from decimal import Decimal
from typing import Any, Literal

import httpx
from pydantic import SecretStr

from app.agents.accommodation_transport_agent import (
    AccommodationTransportAgent,
)
from app.core.config import Settings
from app.schemas.accommodation_transport import (
    AccommodationTransportRecommendation,
    RecommendedTransportMode,
)
from app.schemas.agent_schemas import AgentContext, NormalizedTravelRequirements
from app.schemas.destination_research import (
    Attraction,
    DestinationResearch,
    SourceReference,
)
from app.schemas.external import GeocodingResult, RouteResult
from app.services.cache_service import CACHE_MISS_VALUE
from app.services.gemini_service import GeminiModelUnavailableError
from app.tools.base import TravelAPIResponseError
from app.tools.openrouteservice import OpenRouteServiceTool

_DEFAULT_ROUTE = object()


def requirements(**overrides: Any) -> NormalizedTravelRequirements:
    values: dict[str, Any] = {
        "origin": "Chennai",
        "destination": "Hyderabad",
        "start_date": date(2027, 1, 10),
        "end_date": date(2027, 1, 13),
        "travelers_count": 3,
        "total_budget": Decimal("50000"),
        "currency": "INR",
        "travel_style": "budget",
        "interests": [],
        "constraints": {},
        "travel_duration_days": 4,
    }
    values.update(overrides)
    return NormalizedTravelRequirements.model_validate(values)


def research(
    *,
    latitude: float | None = 17.385,
    longitude: float | None = 78.4867,
) -> DestinationResearch:
    return DestinationResearch(
        destination_name="Hyderabad, India",
        latitude=latitude,
        longitude=longitude,
        attractions=[
            Attraction(
                place_id="destination-place-1",
                name="Historic District",
                latitude=17.39,
                longitude=78.49,
                formatted_address="Central Heritage Area, Hyderabad",
            )
        ],
        source_references=[
            SourceReference(
                title="Example Hyderabad travel guide",
                url="https://example.com/hyderabad",
                topic="travel_tips",
            )
        ],
    )


def context(
    req: NormalizedTravelRequirements | None = None,
    destination: DestinationResearch | None = None,
) -> AgentContext:
    return AgentContext(
        normalized_requirements=req or requirements(),
        destination_research=(destination or research()).model_dump(mode="json"),
    )


def route_result(
    *,
    distance_meters: float = 630_500,
    duration_seconds: float = 39_600,
) -> RouteResult:
    return RouteResult(
        profile="driving",
        distance_meters=distance_meters,
        duration_seconds=duration_seconds,
        geometry=[],
    )


def recommendation(
    *,
    accommodation_types: list[str] | None = None,
    areas: list[str] | None = None,
    modes: list[tuple[str, str]] | None = None,
) -> AccommodationTransportRecommendation:
    return AccommodationTransportRecommendation(
        accommodation_types=accommodation_types or ["budget_hotel", "hostel"],
        stay_area_suggestions=areas or ["Central Heritage Area, Hyderabad"],
        transport_modes=[
            RecommendedTransportMode(mode=mode, reason=reason)
            for mode, reason in (modes or [("driving", "Flexible road travel.")])
        ],
    )


class FakeGeo:
    def __init__(
        self,
        result: GeocodingResult | None = GeocodingResult(
            latitude=13.0827,
            longitude=80.2707,
            formatted_address="Chennai, Tamil Nadu, India",
            city="Chennai",
        ),
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    async def geocode_destination(
        self, destination: str, *, language: str = "en"
    ) -> GeocodingResult | None:
        self.calls.append(destination)
        if self.error:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        return None


class FakeRouting:
    def __init__(
        self,
        result: RouteResult | None | Exception | object = _DEFAULT_ROUTE,
    ) -> None:
        self.result = route_result() if result is _DEFAULT_ROUTE else result
        self.calls: list[tuple[Any, ...]] = []

    async def calculate_route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        profile: Literal["driving", "walking", "cycling"] = "driving",
    ) -> RouteResult | None:
        self.calls.append((origin, destination, profile))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result  # type: ignore[return-value]

    async def aclose(self) -> None:
        return None


class FakeGemini:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result or recommendation()
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        response_model: type[AccommodationTransportRecommendation],
        **kwargs: Any,
    ) -> AccommodationTransportRecommendation:
        self.calls.append({"prompt": prompt, "response_model": response_model, **kwargs})
        if self.error:
            raise self.error
        return self.result


def build_agent(
    *,
    geo: FakeGeo | None = None,
    routing: FakeRouting | None = None,
    gemini: FakeGemini | None = None,
) -> AccommodationTransportAgent:
    return AccommodationTransportAgent(
        geoapify=geo or FakeGeo(),
        routing=routing or FakeRouting(),
        gemini_service=gemini,
        settings=Settings(_env_file=None),
    )


def test_accommodation_group_rooms_nights_and_unknown_costs() -> None:
    async def run() -> None:
        result = await build_agent().run(context())
        plan = result.output
        assert result.status == "completed"
        assert plan.accommodation.recommended_types == [
            "budget_hotel",
            "hostel",
            "homestay",
        ]
        assert plan.number_of_nights == 3
        assert plan.accommodation.room_count == 2
        assert plan.accommodation.assumed_occupancy_per_room == 2
        assert plan.accommodation.estimated_total_cost.amount is None
        assert plan.accommodation.estimated_total_cost.classification == "unknown"
        assert plan.accommodation.live_availability_checked is False
        assert "accommodation" in plan.unpriced_components
        assert str(plan.source_references[0].url) == "https://example.com/hyderabad"

    asyncio.run(run())


def test_solo_room_count_and_user_supplied_pricing_are_decimal_estimates() -> None:
    async def run() -> None:
        req = requirements(
            travelers_count=1,
            constraints={
                "room_occupancy": 1,
                "cost_assumptions": {
                    "accommodation_nightly_rate_per_room": "1250.50",
                    "local_transport_total": "500.25",
                    "intercity_transport_total": "2000",
                },
            },
        )
        plan = (await build_agent().run(context(req))).output
        assert plan.accommodation.room_count == 1
        assert plan.accommodation.assumed_occupancy_per_room == 1
        assert plan.accommodation.estimated_nightly_rate.amount == Decimal("1250.50")
        assert plan.accommodation_cost.amount == Decimal("3751.50")
        assert plan.local_transport_cost.amount == Decimal("500.25")
        assert plan.intercity_transport_cost.amount == Decimal("2000")
        assert plan.combined_estimated_cost.amount == Decimal("6251.75")
        assert plan.combined_estimated_cost.classification == "estimate"
        assert all(
            cost.currency == "INR"
            for cost in (
                plan.accommodation_cost,
                plan.local_transport_cost,
                plan.intercity_transport_cost,
                plan.combined_estimated_cost,
            )
        )

    asyncio.run(run())


def test_zero_night_trip_has_zero_total_without_inventing_rate() -> None:
    async def run() -> None:
        plan = (
            await build_agent().run(
                context(
                    requirements(
                        start_date=date(2027, 1, 10),
                        end_date=date(2027, 1, 10),
                    )
                )
            )
        ).output
        assert plan.number_of_nights == 0
        assert plan.accommodation.estimated_nightly_rate.amount is None
        assert plan.accommodation_cost.amount == Decimal("0")
        assert plan.accommodation_cost.classification == "estimate"
        assert "hotel quote" in plan.accommodation_cost.uncertainty_notes[0]

    asyncio.run(run())


def test_incomplete_dates_and_traveler_count_remain_unknown() -> None:
    async def run() -> None:
        req = requirements(
            start_date=None,
            end_date=None,
            travelers_count=None,
            travel_duration_days=None,
        )
        result = await build_agent().run(context(req))
        plan = result.output
        assert result.status == "needs_input"
        assert plan.number_of_nights is None
        assert plan.accommodation.nights is None
        assert plan.accommodation.room_count is None
        assert plan.accommodation_cost.amount is None
        assert "Accommodation price is unknown" in " ".join(plan.warnings)

    asyncio.run(run())


def test_explicit_family_accessibility_and_area_constraints_are_preserved() -> None:
    async def run() -> None:
        req = requirements(
            constraints={
                "family_friendly": True,
                "accessibility": "step-free access",
                "preferred_area": "Old City",
            }
        )
        gemini = FakeGemini(
            recommendation(
                accommodation_types=["hostel"],
                areas=["invented neighborhood"],
            )
        )
        plan = (await build_agent(gemini=gemini).run(context(req))).output
        assert "family_accommodation" in plan.accommodation.recommended_types
        assert plan.accommodation.stay_area_suggestions == []
        assert any("stay-area suggestions were discarded" in w for w in plan.warnings)
        assert any("accessibility" in w for w in plan.warnings)
        assert any("step-free access" in a for a in plan.assumptions)

    asyncio.run(run())


def test_explicit_costs_over_budget_are_flagged_and_styles_are_preserved() -> None:
    async def run() -> None:
        req = requirements(
            total_budget=Decimal("1000"),
            travel_style="luxury",
            constraints={
                "cost_assumptions": {
                    "accommodation_nightly_rate_per_room": "1500",
                    "local_transport_total": "250",
                    "intercity_transport_total": "500",
                }
            },
        )
        gemini = FakeGemini(recommendation(accommodation_types=["hostel"]))
        plan = (await build_agent(gemini=gemini).run(context(req))).output
        assert "premium_hotel" in plan.accommodation.recommended_types
        assert plan.combined_estimated_cost.amount == Decimal("9750")
        assert any("exceed the supplied total budget" in w for w in plan.warnings)

    asyncio.run(run())


def test_valid_route_is_parsed_and_converted_to_km_and_minutes() -> None:
    async def run() -> None:
        geo = FakeGeo()
        routing = FakeRouting(route_result(distance_meters=12345, duration_seconds=3670))
        plan = (await build_agent(geo=geo, routing=routing).run(context())).output
        drive = next(item for item in plan.transport_options if item.mode == "driving")
        assert geo.calls == ["Chennai"]
        assert routing.calls == [
            ((13.0827, 80.2707), (17.385, 78.4867), "driving")
        ]
        assert drive.route_estimate is not None
        assert drive.route_estimate.distance_kilometers == Decimal("12.345")
        assert drive.route_estimate.duration_minutes == Decimal("61.17")
        assert drive.route_estimate.verified_provider_fact is True
        assert drive.live_availability_checked is False

    asyncio.run(run())


def test_missing_origin_or_destination_coordinates_skips_route() -> None:
    async def run() -> None:
        routing = FakeRouting()
        agent = build_agent(geo=FakeGeo(result=None), routing=routing)
        plan = (await agent.run(context())).output
        assert routing.calls == []
        assert any("origin coordinates" in item for item in plan.warnings)

        routing2 = FakeRouting()
        plan2 = (
            await build_agent(routing=routing2).run(
                context(destination=research(latitude=None, longitude=None))
            )
        ).output
        assert routing2.calls == []
        assert any("destination coordinates" in item for item in plan2.warnings)

    asyncio.run(run())


def test_geocoding_mismatch_and_route_failure_are_safe_partial_results() -> None:
    async def run() -> None:
        mismatch = FakeGeo(
            result=GeocodingResult(
                latitude=1,
                longitude=1,
                formatted_address="Mumbai, India",
                city="Mumbai",
            )
        )
        routing = FakeRouting()
        plan = (await build_agent(geo=mismatch, routing=routing).run(context())).output
        assert routing.calls == []
        assert any("did not clearly match" in warning for warning in plan.warnings)

        failed_routing = FakeRouting(
            TravelAPIResponseError("ors", "authorization=secret-token")
        )
        plan2 = (await build_agent(routing=failed_routing).run(context())).output
        assert any("no route was fabricated" in item for item in plan2.warnings)
        assert "secret-token" not in " ".join(plan2.warnings)

    asyncio.run(run())


def test_unreachable_route_and_unsupported_modes_are_not_fabricated() -> None:
    async def run() -> None:
        unreachable = FakeRouting()
        no_route = FakeRouting(result=None)
        gemini = FakeGemini(
            recommendation(
                modes=[
                    ("flight", "Consider a flight for long distances."),
                    ("train", "Rail may be an option."),
                ]
            )
        )
        plan = (
            await build_agent(routing=no_route, gemini=gemini).run(context())
        ).output
        assert any(
            "road routing was skipped" in warning.lower() for warning in plan.warnings
        )
        assert all(
            option.route_estimate is None
            for option in plan.transport_options
            if option.mode in {"flight", "train"}
        )
        assert all(
            not option.supported_by_routing_provider
            for option in plan.transport_options
            if option.mode in {"flight", "train"}
        )
        assert all(option.live_availability_checked is False for option in plan.transport_options)
        assert unreachable.calls == []
        assert no_route.calls == []
        assert {option.mode for option in plan.transport_options} == {"flight", "train"}

    asyncio.run(run())


def test_gemini_failure_and_malformed_response_use_deterministic_fallback() -> None:
    async def run() -> None:
        for gemini in (
            FakeGemini(error=GeminiModelUnavailableError("sensitive internal")),
            FakeGemini(result={"hotel": "invented"}),
        ):
            plan = (await build_agent(gemini=gemini).run(context())).output
            assert plan.accommodation.recommended_types
            assert any("Gemini recommendations" in warning for warning in plan.warnings)
            assert "sensitive internal" not in " ".join(plan.warnings)

    asyncio.run(run())


def test_no_gemini_credentials_needed_for_deterministic_plan() -> None:
    async def run() -> None:
        agent = AccommodationTransportAgent(
            geoapify=FakeGeo(),
            routing=FakeRouting(),
            settings=Settings(_env_file=None),
        )
        plan = (await agent.run(context())).output
        assert plan.accommodation.recommended_types
        assert any("deterministic preferences" in w for w in plan.warnings)

    asyncio.run(run())


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], Any] = {}
        self.write_count = 0

    async def get_cached(self, namespace: str, identifier: Any) -> Any:
        return self.values.get((namespace, repr(identifier)), CACHE_MISS_VALUE)

    async def set_cached(
        self,
        namespace: str,
        identifier: Any,
        data: Any,
        ttl_seconds: int | None = None,
    ) -> bool:
        self.write_count += 1
        self.values[(namespace, repr(identifier))] = data
        return True

    async def aclose(self) -> None:
        return None


def test_existing_openrouteservice_cache_prevents_duplicate_route_request() -> None:
    async def run() -> None:
        cache = FakeCache()
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "properties": {
                                "summary": {"distance": 1000, "duration": 600}
                            },
                            "geometry": {"coordinates": [[80.2, 13.0], [78.4, 17.3]]},
                        }
                    ]
                },
            )

        routing = OpenRouteServiceTool(
            Settings(
                _env_file=None,
                OPENROUTESERVICE_API_KEY=SecretStr("test-only-secret"),
            ),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            cache=cache,  # type: ignore[arg-type]
        )
        geo = FakeGeo()
        agent = build_agent(geo=geo, routing=routing)
        try:
            await agent.run(context())
            await agent.run(context())
        finally:
            await agent.aclose()
        assert request_count == 1
        assert cache.write_count == 1

    asyncio.run(run())


def test_currency_missing_is_unknown_not_defaulted_or_converted() -> None:
    async def run() -> None:
        req = requirements(
            currency=None,
            constraints={
                "cost_assumptions": {
                    "accommodation_nightly_rate_per_room": "1250",
                    "local_transport_total": "500",
                    "intercity_transport_total": "2000",
                }
            },
        )
        result = await build_agent().run(context(req))
        plan = result.output
        assert result.status == "needs_input"
        assert plan.currency == "UNKNOWN"
        assert plan.combined_estimated_cost.amount is None
        assert all(
            cost.currency == "UNKNOWN"
            for cost in (
                plan.accommodation_cost,
                plan.local_transport_cost,
                plan.intercity_transport_cost,
                plan.combined_estimated_cost,
            )
        )
        assert all(
            cost.amount is None
            for cost in (
                plan.accommodation_cost,
                plan.local_transport_cost,
                plan.intercity_transport_cost,
                plan.combined_estimated_cost,
            )
        )

    asyncio.run(run())
