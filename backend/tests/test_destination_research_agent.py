import asyncio
from datetime import date
from typing import Any, Literal

import httpx
import pytest
from pydantic import SecretStr

from app.agents.destination_research_agent import DestinationResearchAgent
from app.core.config import Settings
from app.schemas.agent_schemas import (
    AgentContext,
    NormalizedTravelRequirements,
    TravelRequest,
)
from app.schemas.destination_research import (
    DestinationResearchNarrative,
    SuggestedActivity,
)
from app.schemas.external import (
    DailyWeather,
    GeocodingResult,
    PlaceResult,
    TravelSearchResult,
    TravelSearchSource,
    WeatherForecast,
)
from app.services.cache_service import CACHE_MISS_VALUE
from app.tools.base import TravelAPIResponseError
from app.tools.open_meteo import UnsupportedForecastDateError
from app.services.gemini_service import GeminiAPIError

_DEFAULT_GEOCODING = object()


def requirements(**overrides: Any) -> NormalizedTravelRequirements:
    values: dict[str, Any] = {
        "destination": "Hyderabad",
        "origin": "Chennai",
        "start_date": date(2026, 10, 11),
        "end_date": date(2026, 10, 12),
        "travelers_count": 2,
        "total_budget": "30000.00",
        "currency": "INR",
        "travel_style": "culture",
        "interests": ["museum", "history"],
        "constraints": {},
        "travel_duration_days": 2,
    }
    values.update(overrides)
    return NormalizedTravelRequirements.model_validate(values)


def context(req: NormalizedTravelRequirements | None = None) -> AgentContext:
    return AgentContext(
        normalized_requirements=req or requirements(),
    )


def geocoding(
    *,
    formatted_address: str = "Hyderabad, Telangana, India",
    city: str | None = "Hyderabad",
) -> GeocodingResult:
    return GeocodingResult(
        latitude=17.385,
        longitude=78.4867,
        formatted_address=formatted_address,
        city=city,
        country="India",
    )


def place(
    place_id: str,
    name: str,
    *,
    categories: list[str] | None = None,
    latitude: float = 17.4,
    longitude: float = 78.5,
) -> PlaceResult:
    return PlaceResult(
        place_id=place_id,
        name=name,
        categories=categories or ["tourism.attraction"],
        latitude=latitude,
        longitude=longitude,
        distance_meters=120,
    )


def weather_forecast() -> WeatherForecast:
    return WeatherForecast(
        latitude=17.385,
        longitude=78.4867,
        timezone="Asia/Kolkata",
        days=[
            DailyWeather(
                date=date(2026, 10, 11),
                temperature_max_celsius=31,
                temperature_min_celsius=22,
                precipitation_sum_millimeters=0.2,
                weather_code=2,
                condition="Partly cloudy",
            )
        ],
    )


def search_result(
    *,
    title: str = "City travel guide",
    url: str = "https://example.org/hyderabad-guide",
    summary: str = "Explore historic sites and local cuisine.",
) -> TravelSearchResult:
    return TravelSearchResult(
        query="Hyderabad research",
        sources=[
            TravelSearchSource(
                title=title,
                url=url,
                summary=summary,
                content_is_untrusted=True,
            )
        ],
    )


class FakeGeoapify:
    def __init__(
        self,
        result: GeocodingResult | None | object = _DEFAULT_GEOCODING,
        places: list[PlaceResult] | Exception | None = None,
        *,
        geocode_error: Exception | None = None,
    ) -> None:
        self.result = (
            geocoding() if result is _DEFAULT_GEOCODING else result
        )
        self.places = places if places is not None else [
            place("geo-1", "Hyderabad History Museum", categories=["tourism.museum"]),
            place("geo-2", "Charminar", categories=["tourism.attraction"]),
        ]
        self.geocode_error = geocode_error
        self.geocode_calls: list[str] = []
        self.place_calls: list[tuple[float, float]] = []
        self.closed = False

    async def geocode_destination(
        self, destination: str, *, language: str = "en"
    ) -> GeocodingResult | None:
        self.geocode_calls.append(destination)
        if self.geocode_error:
            raise self.geocode_error
        return self.result  # type: ignore[return-value]

    async def find_nearby_places(
        self,
        latitude: float,
        longitude: float,
        *,
        category: Literal["attractions", "hotels", "tourist_places"] = "attractions",
        radius_meters: int = 5_000,
        limit: int = 20,
    ) -> list[PlaceResult]:
        self.place_calls.append((latitude, longitude))
        if isinstance(self.places, Exception):
            raise self.places
        return self.places or []

    async def aclose(self) -> None:
        self.closed = True


class FakeWeather:
    def __init__(self, result: WeatherForecast | Exception | None = None) -> None:
        self.result = result if result is not None else weather_forecast()
        self.calls: list[tuple[Any, ...]] = []

    async def get_weather(
        self,
        latitude: float,
        longitude: float,
        start_date: date,
        end_date: date,
        *,
        timezone: str = "auto",
    ) -> WeatherForecast:
        self.calls.append((latitude, longitude, start_date, end_date))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        return None


class FakeTavily:
    def __init__(self, result: TravelSearchResult | Exception | None = None) -> None:
        self.result = result if result is not None else search_result()
        self.calls: list[str] = []

    async def search_travel_information(
        self,
        query: str,
        *,
        search_depth: Literal["basic", "advanced"] = "basic",
        max_results: int = 5,
    ) -> TravelSearchResult:
        self.calls.append(query)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        return None


class FakeGemini:
    def __init__(
        self, result: DestinationResearchNarrative | Exception | Any
    ) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        response_model: type[DestinationResearchNarrative],
        **kwargs: Any,
    ) -> DestinationResearchNarrative:
        self.calls.append(
            {"prompt": prompt, "response_model": response_model, **kwargs}
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_agent(
    *,
    geo: FakeGeoapify | None = None,
    weather: FakeWeather | None = None,
    tavily: FakeTavily | None = None,
    gemini: FakeGemini | None = None,
) -> DestinationResearchAgent:
    return DestinationResearchAgent(
        geoapify=geo or FakeGeoapify(),
        weather=weather or FakeWeather(),
        tavily=tavily or FakeTavily(),
        gemini_service=gemini or FakeGemini(
            DestinationResearchNarrative(
                summary="A history-focused city visit.",
                ranked_place_ids=["geo-1", "invented-id"],
                recommended_activities=[
                    SuggestedActivity(
                        title="Visit the museum",
                        description="Explore local history.",
                        related_place_ids=["geo-1", "invented-id"],
                    )
                ],
                travel_advisories=["Check local opening times."],
            )
        ),
        settings=Settings(_env_file=None),
    )


def test_research_excludes_address_only_and_non_travel_pois_but_keeps_food_venues() -> None:
    async def run() -> None:
        geo = FakeGeoapify(places=[
            place("road", "MDR48", categories=["building.catering", "catering.restaurant"]),
            place("stay", "Sample Hotel", categories=["accommodation.hotel"]),
            place("other", "Office", categories=["office"]),
            place("food", "Hotel Udupi Krishna", categories=["catering.restaurant"]),
            place("sight", "Provider Memorial", categories=["tourism.sights.memorial"]),
        ])
        result = await make_agent(geo=geo).run(context(requirements(interests=["Food", "Culture"])))
        assert {item.place_id for item in result.output.attractions} == {"food", "sight"}
        assert any("accommodation is researched separately" in warning for warning in result.output.warnings)
    asyncio.run(run())


def test_successful_destination_research_keeps_verified_facts_and_ai_separate() -> None:
    async def run() -> None:
        geo, weather, tavily = FakeGeoapify(), FakeWeather(), FakeTavily()
        gemini = FakeGemini(
            DestinationResearchNarrative(
                summary="A museum-focused research summary.",
                ranked_place_ids=["geo-1", "geo-2"],
                recommended_activities=[
                    SuggestedActivity(
                        title="Explore the museum",
                        description="Spend time with local collections.",
                        related_place_ids=["geo-1"],
                    )
                ],
                travel_advisories=["Plan visits around opening hours."],
            )
        )
        result = await make_agent(
            geo=geo, weather=weather, tavily=tavily, gemini=gemini
        ).run(context())
        output = result.output
        assert result.status == "completed"
        assert output.latitude == 17.385
        assert output.longitude == 78.4867
        assert output.attractions[0].place_id == "geo-1"
        assert output.attractions[0].verified_provider_fact is True
        assert output.weather_summary is not None
        assert output.weather_summary.weather_status == "available"
        assert output.weather_summary.daily_forecasts[0].weather_code == 2
        assert len(output.source_references) == 1
        assert str(output.source_references[0].url) == "https://example.org/hyderabad-guide"
        assert output.travel_advisories[0].evidence_type == "provider_search_result"
        assert output.travel_advisories[0].content_is_untrusted is True
        assert output.travel_advisories[-1].evidence_type == "ai_suggestion"
        assert output.recommended_activities[0].related_place_ids == ["geo-1"]
        assert len(tavily.calls) == 5
        assert weather.calls[0][2:] == (date(2026, 10, 11), date(2026, 10, 12))
        assert gemini.calls[0]["response_model"] is DestinationResearchNarrative
        assert "untrusted data" in gemini.calls[0]["system_instruction"]

    asyncio.run(run())


def test_missing_or_invalid_destination_returns_needs_input_without_fabrication() -> None:
    async def run() -> None:
        empty_geo = FakeGeoapify(result=None)
        result = await make_agent(geo=empty_geo).run(
            context(requirements(destination=None))
        )
        assert result.status == "needs_input"
        assert result.output.latitude is None
        assert result.output.attractions == []
        assert empty_geo.place_calls == []

        invalid_geo = FakeGeoapify(result=None)
        failed = await make_agent(geo=invalid_geo).run(context())
        assert failed.status == "needs_input"
        assert failed.output.destination_name == "Hyderabad"
        assert not failed.output.source_references
        assert any("specific destination city and country" in warning for warning in failed.warnings)

    asyncio.run(run())


def test_geocoding_mismatch_is_treated_as_ambiguous_and_not_used() -> None:
    async def run() -> None:
        geo = FakeGeoapify(
            result=geocoding(
                formatted_address="Springfield, Illinois, United States",
                city="Springfield",
            )
        )
        result = await make_agent(geo=geo).run(
            context(requirements(destination="Hyderabad"))
        )
        assert result.status == "needs_input"
        assert result.output.latitude is None
        assert geo.place_calls == []
        assert any("does not clearly match" in item for item in result.warnings)

    asyncio.run(run())


def test_geoapify_failure_returns_partial_research_without_secret_leak() -> None:
    async def run() -> None:
        secret_text = "api_key=do-not-show"
        geo = FakeGeoapify(geocode_error=TravelAPIResponseError("geoapify", secret_text))
        result = await make_agent(geo=geo).run(context())
        assert result.status == "needs_input"
        assert result.output.latitude is None
        assert all(secret_text not in warning for warning in result.warnings)
        assert "Geoapify" in " ".join(result.warnings)

    asyncio.run(run())


def test_places_weather_tavily_and_gemini_failures_are_partial() -> None:
    async def run() -> None:
        geo = FakeGeoapify(
            places=TravelAPIResponseError("geoapify", "private error")
        )
        weather = FakeWeather(
            TravelAPIResponseError("open-meteo", "private error")
        )
        tavily = FakeTavily(TravelAPIResponseError("tavily", "private error"))
        gemini = FakeGemini(GeminiAPIError("private Gemini error"))
        result = await make_agent(
            geo=geo, weather=weather, tavily=tavily, gemini=gemini
        ).run(context())
        assert result.status == "needs_input"
        assert result.output.resolution_status == "provider_unavailable"
        assert result.output.attractions == []
        assert result.output.weather_summary is not None
        assert result.output.weather_summary.forecast_available is False
        assert result.output.source_references == []
        joined = " ".join(result.warnings)
        assert "private error" not in joined
        assert "private Gemini error" not in joined
        assert "Tavily" in joined
        assert "Gemini" in joined

    asyncio.run(run())


def test_unsupported_weather_dates_return_limitation_without_forecast() -> None:
    async def run() -> None:
        weather = FakeWeather(
            UnsupportedForecastDateError("unsupported future window")
        )
        result = await make_agent(weather=weather).run(context())
        summary = result.output.weather_summary
        assert summary is not None
        assert summary.forecast_available is False
        assert summary.weather_status == "unavailable"
        assert summary.daily_forecasts == []
        assert "outside" in (summary.limitation or "")

    asyncio.run(run())


def test_attractions_are_deduplicated_and_filtered_by_interest() -> None:
    async def run() -> None:
        geo = FakeGeoapify(
            places=[
                place("museum-1", "City History Museum", categories=["tourism.museum"]),
                place("museum-1", "Duplicate Museum", categories=["tourism.museum"]),
                place("park-1", "Urban Park", categories=["leisure.park"]),
            ]
        )
        result = await make_agent(geo=geo).run(
            context(requirements(interests=["museum"]))
        )
        assert [item.place_id for item in result.output.attractions] == ["museum-1"]
        assert result.output.attractions[0].name == "City History Museum"

    asyncio.run(run())


def test_gemini_unknown_place_ids_and_generated_citations_are_rejected() -> None:
    async def run() -> None:
        gemini = FakeGemini(
            DestinationResearchNarrative.model_validate(
                {
                    "summary": "Provider-based summary.",
                    "ranked_place_ids": ["not-a-provider-id", "geo-2"],
                    "recommended_activities": [
                        {
                            "title": "Visit",
                            "description": "A suggested activity.",
                            "related_place_ids": ["fake-id"],
                        }
                    ],
                    "travel_advisories": [],
                }
            )
        )
        result = await make_agent(gemini=gemini).run(
            context(requirements(interests=[]))
        )
        assert [item.place_id for item in result.output.attractions] == [
            "geo-2",
            "geo-1",
        ]
        assert result.output.recommended_activities[0].related_place_ids == []
        assert result.output.recommended_activities[0].source_urls == []
        assert any("unknown attraction IDs" in item for item in result.warnings)
        assert all(
            str(reference.url) != "https://invented.example/citation"
            for reference in result.output.source_references
        )

    asyncio.run(run())


def test_malformed_gemini_payload_is_discarded() -> None:
    async def run() -> None:
        gemini = FakeGemini(
            {
                "summary": "Bad",
                "ranked_place_ids": ["geo-1"],
                "recommended_activities": [
                    {
                        "title": "Visit",
                        "description": "A suggestion.",
                        "related_place_ids": ["geo-1"],
                        "source_urls": ["https://fake.invalid/source"],
                    }
                ],
            }
        )
        result = await make_agent(gemini=gemini).run(context())
        assert result.output.summary is None
        assert result.output.recommended_activities == []
        assert any("Gemini recommendations" in item for item in result.warnings)
        assert all(
            "fake.invalid" not in str(reference.url)
            for reference in result.output.source_references
        )

    asyncio.run(run())


def test_partial_dates_skip_weather_without_fabricating() -> None:
    async def run() -> None:
        weather = FakeWeather()
        result = await make_agent(weather=weather).run(
            context(requirements(start_date=None, end_date=None))
        )
        assert weather.calls == []
        assert result.output.weather_summary is not None
        assert not result.output.weather_summary.forecast_available
        assert "incomplete" in (result.output.weather_summary.limitation or "")

    asyncio.run(run())


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.writes = 0

    async def get_cached(self, namespace: str, identifier: Any) -> Any:
        return self.values.get(
            f"{namespace}:{repr(identifier)}",
            CACHE_MISS_VALUE,
        )

    async def set_cached(
        self,
        namespace: str,
        identifier: Any,
        data: Any,
        ttl_seconds: int | None = None,
    ) -> bool:
        self.writes += 1
        self.values[f"{namespace}:{repr(identifier)}"] = data
        return True

    async def aclose(self) -> None:
        return None


def test_agent_uses_existing_geoapify_cache_to_avoid_redundant_requests() -> None:
    async def run() -> None:
        cache = FakeCache()
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if request.url.path.endswith("geocode/search"):
                return httpx.Response(
                    200,
                    json={
                        "features": [
                            {
                                "properties": {
                                    "lat": 17.385,
                                    "lon": 78.4867,
                                    "formatted": "Hyderabad, India",
                                    "city": "Hyderabad",
                                    "country": "India",
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "properties": {
                                "name": "Hyderabad History Museum",
                                "categories": ["tourism.museum"],
                                "lat": 17.4,
                                "lon": 78.5,
                                "place_id": "museum-cache-1",
                            }
                        }
                    ]
                },
            )

        from app.tools.geoapify import GeoapifyTool

        geo = GeoapifyTool(
            Settings(
                _env_file=None,
                GEOAPIFY_API_KEY=SecretStr("test-only"),
            ),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            cache=cache,  # type: ignore[arg-type]
        )
        agent = make_agent(geo=geo)
        try:
            await agent.run(context(requirements(interests=[])))
            await agent.run(context(requirements(interests=[])))
        finally:
            await agent.aclose()
        assert calls == 2
        assert cache.writes == 2

    asyncio.run(run())
