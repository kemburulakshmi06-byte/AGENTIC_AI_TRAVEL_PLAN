import asyncio
from datetime import date
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.schemas.external import (
    GeocodingResult,
    RouteResult,
    TravelSearchResult,
    WeatherForecast,
)
from app.services.cache_service import CACHE_MISS_VALUE, CacheService
from app.tools.base import (
    TravelAPIAuthenticationError,
    TravelAPIError,
    TravelAPIRateLimitError,
    TravelAPIResponseError,
    TravelAPITimeoutError,
)
from app.tools.geoapify import GeoapifyTool
from app.tools.open_meteo import OpenMeteoTool, UnsupportedForecastDateError
from app.tools.openrouteservice import OpenRouteServiceTool
from app.tools.tavily import TavilyTool


def provider_settings() -> Settings:
    return Settings(
        _env_file=None,
        GEOAPIFY_API_KEY=SecretStr("geo-test-key"),
        OPENROUTESERVICE_API_KEY=SecretStr("ors-test-key"),
        TAVILY_API_KEY=SecretStr("tavily-test-key"),
    )


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], Any] = {}
        self.set_calls: list[tuple[str, Any, Any, int | None]] = []
        self.closed = False

    async def get_cached(self, namespace: str, identifier: Any) -> Any:
        return self.values.get((namespace, repr(identifier)), CACHE_MISS_VALUE)

    async def set_cached(
        self,
        namespace: str,
        identifier: Any,
        data: Any,
        ttl_seconds: int | None = None,
    ) -> bool:
        self.set_calls.append((namespace, identifier, data, ttl_seconds))
        self.values[(namespace, repr(identifier))] = data
        return True

    async def aclose(self) -> None:
        self.closed = True


def mocked_client(
    handler: httpx.MockTransport | Any,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_geoapify_geocodes_and_caches_normalized_destination() -> None:
    async def run() -> None:
        cache = FakeCache()

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/v1/geocode/search")
            assert request.url.params["text"] == "Paris"
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "properties": {
                                "lat": 48.8566,
                                "lon": 2.3522,
                                "formatted": "Paris, France",
                                "city": "Paris",
                                "country": "France",
                                "country_code": "fr",
                            }
                        }
                    ]
                },
            )

        tool = GeoapifyTool(
            provider_settings(), client=mocked_client(handler), cache=cache
        )  # type: ignore[arg-type]
        result = await tool.geocode_destination(" Paris ")
        assert isinstance(result, GeocodingResult)
        assert result.latitude == 48.8566
        assert result.provider == "geoapify"
        assert cache.set_calls[0][0] == "destination"
        assert cache.set_calls[0][3] == 86_400

    asyncio.run(run())


def test_city_discovery_uses_provider_boundary_and_retains_location_metadata() -> None:
    async def run() -> None:
        def handler(request):
            assert request.url.params["filter"] == "place:provider-region-id"
            assert request.url.params["categories"] == "populated_place.city,populated_place.town"
            return httpx.Response(200, json={"features": [{"properties": {
                "lat": 10, "lon": 20, "formatted": "Provider city, Provider region",
                "city": "Provider city", "state": "Provider region", "name": "Provider city",
                "place_id": "provider-city-id", "country_code": "xx",
            }}]})

        tool = GeoapifyTool(provider_settings(), client=mocked_client(handler), cache=FakeCache())
        parent = GeocodingResult(latitude=10, longitude=20, formatted_address="Provider region", result_type="state", place_id="provider-region-id")
        try:
            result = await tool.find_destination_cities(parent)
            assert result[0].city == "Provider city"
            assert result[0].state == "Provider region"
            assert result[0].result_type == "city"
            assert result[0].place_id == "provider-city-id"
        finally:
            await tool.aclose()

    asyncio.run(run())


def test_geoapify_empty_geocode_is_none_and_nearby_places_are_normalized() -> None:
    async def run() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if request.url.path.endswith("/geocode/search"):
                return httpx.Response(200, json={"features": []})
            assert request.url.params["filter"] == "circle:2.35,48.85,5000"
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "properties": {
                                "name": "Museum",
                                "categories": ["tourism.attraction"],
                                "lat": 48.86,
                                "lon": 2.36,
                                "distance": 700,
                                "formatted": "Museum, Paris",
                            }
                        }
                    ]
                },
            )

        tool = GeoapifyTool(
            provider_settings(), client=mocked_client(handler), cache=FakeCache()
        )  # type: ignore[arg-type]
        assert await tool.geocode_destination("Nowhere") is None
        places = await tool.find_nearby_places(48.85, 2.35)
        assert len(places) == 1
        assert places[0].name == "Museum"
        assert places[0].distance_meters == 700
        assert calls == 2

    asyncio.run(run())


def test_geoapify_skips_wrong_geocode_candidate_and_selects_full_destination_match() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/geocode/search"):
                assert request.url.params["limit"] == "5"
                return httpx.Response(
                    200,
                    json={
                        "features": [
                            {
                                "properties": {
                                    "lat": 39.78,
                                    "lon": -89.64,
                                    "formatted": "Springfield, Illinois, United States",
                                    "city": "Springfield",
                                    "country": "United States",
                                }
                            },
                            {
                                "properties": {
                                    "lat": 48.8566,
                                    "lon": 2.3522,
                                    "formatted": "Paris, France",
                                    "city": "Paris",
                                    "country": "France",
                                }
                            },
                        ]
                    },
                )
            raise AssertionError("places are not needed for this test")

        tool = GeoapifyTool(
            provider_settings(), client=mocked_client(handler), cache=FakeCache()
        )  # type: ignore[arg-type]
        result = await tool.geocode_destination("Paris, France")
        assert result is not None
        assert result.city == "Paris"
        assert result.country == "France"
        assert result.latitude == 48.8566
        await tool.aclose()

    asyncio.run(run())


def test_geoapify_does_not_accept_partial_destination_match() -> None:
    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "properties": {
                                "lat": 39.78,
                                "lon": -89.64,
                                "formatted": "Springfield, Illinois, United States",
                                "city": "Springfield",
                                "country": "United States",
                            }
                        }
                    ]
                },
            )

        tool = GeoapifyTool(
            provider_settings(), client=mocked_client(handler), cache=FakeCache()
        )  # type: ignore[arg-type]
        with pytest.raises(TravelAPIResponseError) as raised:
            await tool.geocode_destination("Paris, France")
        assert raised.value.category == "irrelevant_results"
        await tool.aclose()

    asyncio.run(run())


def test_geoapify_rejects_equally_ranked_ambiguous_destinations() -> None:
    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "properties": {
                                "lat": 48.8566,
                                "lon": 2.3522,
                                "formatted": "Paris, France",
                                "city": "Paris",
                                "country": "France",
                            }
                        },
                        {
                            "properties": {
                                "lat": 33.6609,
                                "lon": -95.5555,
                                "formatted": "Paris, Texas, United States",
                                "city": "Paris",
                                "country": "France",
                            }
                        },
                    ]
                },
            )

        tool = GeoapifyTool(
            provider_settings(), client=mocked_client(handler), cache=FakeCache()
        )  # type: ignore[arg-type]
        assert await tool.geocode_destination("Paris, France") is None
        await tool.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "query,first,second",
    [
        (
            "Hyderabad, India",
            {"lat": 17.3850, "lon": 78.4867, "formatted": "Hyderabad, Telangana, India", "name": "Hyderabad", "city": "Hyderabad", "state": "Telangana", "country": "India", "country_code": "in", "result_type": "city"},
            {"lat": 17.4126, "lon": 78.2679, "formatted": "Hyderabad, Telangana, India", "name": "Hyderabad", "city": "Hyderabad", "state": "Telangana", "country": "India", "country_code": "in", "result_type": "city"},
        ),
        (
            "Dubai, UAE",
            {"lat": 25.2647227, "lon": 55.2924146, "formatted": "Dubai, Dubai Emirate, United Arab Emirates", "name": "Dubai", "city": "Dubai", "state": "Dubai Emirate", "country": "United Arab Emirates", "country_code": "ae", "result_type": "city"},
            {"lat": 25.080209, "lon": 55.283596, "formatted": "Dubai, United Arab Emirates", "name": "Dubai", "city": "Dubai", "state": "Dubai", "country": "United Arab Emirates", "country_code": "ae", "result_type": "city"},
        ),
        (
            "Tokyo, Japan",
            {"lat": 35.6762, "lon": 139.6503, "formatted": "Tokyo, Japan", "name": "Tokyo", "city": "Tokyo", "state": "Tokyo", "country": "Japan", "country_code": "jp", "result_type": "city"},
            {"lat": 35.6895, "lon": 139.6917, "formatted": "Tokyo, Japan", "name": "Tokyo", "city": "Tokyo", "state": "Tokyo", "country": "Japan", "country_code": "jp", "result_type": "city"},
        ),
        (
            "Goa, India",
            {"lat": 15.2993, "lon": 74.1240, "formatted": "Goa, India", "name": "Goa", "state": "Goa", "country": "India", "country_code": "in", "result_type": "state"},
            {"lat": 15.4909, "lon": 73.8278, "formatted": "Goa, India", "name": "Goa", "state": "Goa", "country": "India", "country_code": "in", "result_type": "state"},
        ),
    ],
)
def test_geoapify_deduplicates_provider_centroids_for_the_same_locality(
    query: str,
    first: dict[str, object],
    second: dict[str, object],
) -> None:
    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"features": [{"properties": first}, {"properties": second}]},
            )

        tool = GeoapifyTool(
            provider_settings(), client=mocked_client(handler), cache=FakeCache()
        )  # type: ignore[arg-type]
        try:
            result = await tool.geocode_destination(query)
            assert result is not None
            assert result.latitude == first["lat"]
            assert result.longitude == first["lon"]
        finally:
            await tool.aclose()

    asyncio.run(run())


def test_geoapify_keeps_nearby_same_name_localities_ambiguous_when_regions_differ() -> None:
    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"features": [
                {"properties": {
                    "lat": 10.0, "lon": 20.0, "formatted": "Example, North, Testland",
                    "name": "Example", "city": "Example", "state": "North",
                    "country": "Testland", "country_code": "tl", "result_type": "city",
                }},
                {"properties": {
                    "lat": 10.01, "lon": 20.01, "formatted": "Example, South, Testland",
                    "name": "Example", "city": "Example", "state": "South",
                    "country": "Testland", "country_code": "tl", "result_type": "city",
                }},
            ]})

        tool = GeoapifyTool(
            provider_settings(), client=mocked_client(handler), cache=FakeCache()
        )  # type: ignore[arg-type]
        try:
            assert await tool.geocode_destination("Example") is None
        finally:
            await tool.aclose()

    asyncio.run(run())


def test_america_is_not_silently_resolved_to_an_arbitrary_city() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["text"] == "America"
            return httpx.Response(200, json={"features": [
                {"properties": {
                    "lat": latitude, "lon": longitude,
                    "formatted": f"America, {country}", "city": "America",
                    "country": country,
                }}
                for latitude, longitude, country in [
                    (40.8, -85.7, "United States of America"),
                    (13.5, 123.3, "Philippines"),
                    (-13.0, 39.0, "Mozambique"),
                ]
            ]})

        tool = GeoapifyTool(provider_settings(), client=mocked_client(handler), cache=FakeCache())
        try:
            assert await tool.geocode_destination("America") is None
        finally:
            await tool.aclose()

    asyncio.run(run())


def test_geoapify_invalid_inputs_and_missing_key_do_not_call_provider() -> None:
    async def run() -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"features": []})

        tool = GeoapifyTool(
            provider_settings(), client=mocked_client(handler), cache=FakeCache()
        )  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await tool.geocode_destination(" ")
        with pytest.raises(ValueError):
            await tool.find_nearby_places(91, 0)
        missing_key = GeoapifyTool(
            Settings(_env_file=None), client=mocked_client(handler), cache=FakeCache()
        )  # type: ignore[arg-type]
        with pytest.raises(TravelAPIAuthenticationError):
            await missing_key.geocode_destination("Paris")
        assert calls == 0

    asyncio.run(run())


def test_openrouteservice_uses_lon_lat_order_and_returns_units() -> None:
    async def run() -> None:
        cache = FakeCache()

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/driving-car/geojson")
            assert request.headers["Authorization"] == "ors-test-key"
            import json

            body = json.loads(request.content)
            assert body["coordinates"] == [[2.35, 48.85], [2.36, 48.86]]
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "properties": {"summary": {"distance": 1200, "duration": 500}},
                            "geometry": {"coordinates": [[2.35, 48.85], [2.36, 48.86]]},
                        }
                    ]
                },
            )

        tool = OpenRouteServiceTool(
            provider_settings(), client=mocked_client(handler), cache=cache
        )  # type: ignore[arg-type]
        result = await tool.calculate_route((48.85, 2.35), (48.86, 2.36))
        assert isinstance(result, RouteResult)
        assert result.distance_meters == 1200
        assert result.duration_seconds == 500
        assert result.geometry[0].longitude == 2.35
        assert cache.set_calls[0][0] == "routes"

    asyncio.run(run())


def test_openrouteservice_maps_all_profiles_and_empty_route() -> None:
    async def run() -> None:
        observed_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed_paths.append(request.url.path)
            return httpx.Response(200, json={"features": []})

        tool = OpenRouteServiceTool(
            provider_settings(), client=mocked_client(handler), cache=FakeCache()
        )  # type: ignore[arg-type]
        assert await tool.calculate_route(
            (0, 0), (1, 1), profile="walking"
        ) is None
        assert await tool.calculate_route(
            (0, 0), (1, 1), profile="cycling"
        ) is None
        assert observed_paths == [
            "/v2/directions/foot-walking/geojson",
            "/v2/directions/cycling-regular/geojson",
        ]

    asyncio.run(run())


def test_open_meteo_returns_daily_units_and_weather_condition() -> None:
    async def run() -> None:
        cache = FakeCache()

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["temperature_unit"] == "celsius"
            assert request.url.params["precipitation_unit"] == "mm"
            assert request.url.params["daily"] == (
                "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code"
            )
            return httpx.Response(
                200,
                json={
                    "latitude": 48.85,
                    "longitude": 2.35,
                    "timezone": "Europe/Paris",
                    "daily": {
                        "time": ["2026-10-11"],
                        "temperature_2m_max": [17.5],
                        "temperature_2m_min": [10.0],
                        "precipitation_sum": [2.4],
                        "weather_code": [61],
                    },
                },
            )

        tool = OpenMeteoTool(
            settings=provider_settings(),
            client=mocked_client(handler),
            cache=cache,  # type: ignore[arg-type]
            clock=lambda: date(2026, 10, 10),
        )
        result = await tool.get_weather(
            48.85,
            2.35,
            date(2026, 10, 11),
            date(2026, 10, 11),
            timezone="Europe/Paris",
        )
        assert isinstance(result, WeatherForecast)
        assert result.days[0].temperature_max_celsius == 17.5
        assert result.days[0].precipitation_sum_millimeters == 2.4
        assert result.days[0].condition == "Slight rain"
        assert cache.set_calls[0][0] == "weather"

    asyncio.run(run())


def test_open_meteo_refuses_unsupported_dates_without_network() -> None:
    async def run() -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={})

        tool = OpenMeteoTool(
            settings=provider_settings(),
            client=mocked_client(handler),
            cache=FakeCache(),  # type: ignore[arg-type]
            clock=lambda: date(2026, 10, 10),
        )
        with pytest.raises(UnsupportedForecastDateError):
            await tool.get_daily_forecast(0, 0, date(2026, 10, 9))
        with pytest.raises(UnsupportedForecastDateError):
            await tool.get_daily_forecast(0, 0, date(2026, 10, 26))
        with pytest.raises(ValueError, match="end_date"):
            await tool.get_weather(
                0, 0, date(2026, 10, 12), date(2026, 10, 11)
            )
        with pytest.raises(ValueError, match="latitude"):
            await tool.get_weather(
                float("nan"), 0, date(2026, 10, 11), date(2026, 10, 11)
            )
        assert calls == 0

    asyncio.run(run())


def test_open_meteo_empty_or_incomplete_response_fails_safely() -> None:
    async def run() -> None:
        payloads = [
            {"daily": {"time": []}},
            {
                "daily": {
                    "time": ["2026-10-11"],
                    "temperature_2m_max": [30],
                    "temperature_2m_min": [20],
                    "precipitation_sum": [0],
                }
            },
        ]

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payloads.pop(0))

        tool = OpenMeteoTool(
            settings=provider_settings(),
            client=mocked_client(handler),
            cache=FakeCache(),  # type: ignore[arg-type]
            clock=lambda: date(2026, 10, 10),
        )
        for _ in range(2):
            with pytest.raises(TravelAPIResponseError):
                await tool.get_weather(
                    17.385, 78.4867, date(2026, 10, 11), date(2026, 10, 11)
                )

    asyncio.run(run())


def test_open_meteo_weather_cache_hit_skips_http_and_preserves_values() -> None:
    async def run() -> None:
        cache = FakeCache()
        identifier = {
            "latitude": 17.385,
            "longitude": 78.4867,
            "start_date": "2026-10-11",
            "end_date": "2026-10-11",
            "timezone": "Asia/Kolkata",
        }
        cached = {
            "provider": "open-meteo",
            "latitude": 17.385,
            "longitude": 78.4867,
            "timezone": "Asia/Kolkata",
            "days": [
                {
                    "date": "2026-10-11",
                    "temperature_max_celsius": 31,
                    "temperature_min_celsius": 22,
                    "precipitation_sum_millimeters": 1.2,
                    "weather_code": 61,
                    "condition": "Slight rain",
                }
            ],
        }
        cache.values[("weather", repr(identifier))] = cached
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        tool = OpenMeteoTool(
            settings=provider_settings(),
            client=mocked_client(handler),
            cache=cache,  # type: ignore[arg-type]
            clock=lambda: date(2026, 10, 10),
        )
        forecast = await tool.get_weather(
            17.385,
            78.4867,
            date(2026, 10, 11),
            date(2026, 10, 11),
            timezone="Asia/Kolkata",
        )
        assert forecast.days[0].temperature_max_celsius == 31
        assert forecast.days[0].condition == "Slight rain"
        assert calls == 0

    asyncio.run(run())


def test_tavily_returns_untrusted_sources_without_answer_injection() -> None:
    async def run() -> None:
        cache = FakeCache()

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            body = json.loads(request.content)
            assert body["query"] == "Paris museums"
            assert body["include_answer"] is False
            assert body["max_results"] == 3
            assert "api_key" in body
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Museum guide",
                            "url": "https://example.com/museum",
                            "content": "Untrusted page text.",
                        }
                    ]
                },
            )

        tool = TavilyTool(
            provider_settings(), client=mocked_client(handler), cache=cache
        )  # type: ignore[arg-type]
        result = await tool.search_travel_information(
            "Paris museums", max_results=3
        )
        assert isinstance(result, TravelSearchResult)
        assert result.sources[0].title == "Museum guide"
        assert str(result.sources[0].url) == "https://example.com/museum"
        assert result.sources[0].summary == "Untrusted page text."
        assert result.sources[0].content_is_untrusted is True
        assert cache.set_calls[0][0] == "search"
        cached_sources = cache.set_calls[0][2]
        assert isinstance(cached_sources, list)
        assert all("query" not in source for source in cached_sources)

    asyncio.run(run())


def test_tool_cache_hit_avoids_provider_request_and_cache_miss_calls_it() -> None:
    async def run() -> None:
        cache = FakeCache()
        identifier = {"query": "cached place", "language": "en"}
        cache.values[("destination", repr(identifier))] = {
            "provider": "geoapify",
            "latitude": 1,
            "longitude": 2,
            "formatted_address": "Cached",
            "result_type": "city",
        }
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"features": []})

        tool = GeoapifyTool(
            provider_settings(), client=mocked_client(handler), cache=cache
        )  # type: ignore[arg-type]
        assert (await tool.geocode_destination("cached place")).formatted_address == "Cached"
        assert await tool.geocode_destination("uncached place") is None
        assert calls == 1

    asyncio.run(run())


def test_provider_request_continues_when_redis_is_not_configured() -> None:
    async def run() -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json={
                    "features": [
                        {
                            "properties": {
                                "lat": 10,
                                "lon": 20,
                                "formatted": "Somewhere",
                            }
                        }
                    ]
                },
            )

        settings = Settings(
            _env_file=None,
            GEOAPIFY_API_KEY=SecretStr("geo-test-key"),
        )
        cache = CacheService(settings=settings)
        tool = GeoapifyTool(
            settings, client=mocked_client(handler), cache=cache
        )  # type: ignore[arg-type]
        result = await tool.geocode_destination("Somewhere")
        assert result is not None
        assert result.formatted_address == "Somewhere"
        assert calls == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, TravelAPIAuthenticationError),
        (429, TravelAPIRateLimitError),
        (503, TravelAPIResponseError),
    ],
)
def test_provider_http_errors_are_classified_without_body_leaks(
    status_code: int, error_type: type[TravelAPIError]
) -> None:
    async def run() -> None:
        client = mocked_client(
            lambda _request: httpx.Response(
                status_code, text="private provider body should not escape"
            )
        )
        tool = TavilyTool(
            provider_settings(), client=client, cache=FakeCache()
        )  # type: ignore[arg-type]
        with pytest.raises(error_type) as error:
            await tool.search_travel_information("travel")
        assert "private provider body" not in str(error.value)
        assert error.value.status_code == status_code

    asyncio.run(run())


def test_provider_timeout_is_sanitized() -> None:
    async def run() -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("private timeout details")

        tool = GeoapifyTool(
            provider_settings(),
            client=mocked_client(handler),
            cache=FakeCache(),
        )  # type: ignore[arg-type]
        with pytest.raises(TravelAPITimeoutError) as error:
            await tool.geocode_destination("Paris")
        assert str(error.value) == "geoapify: request timed out."

    asyncio.run(run())


def test_provider_invalid_payload_is_not_fabricated() -> None:
    async def run() -> None:
        tool = GeoapifyTool(
            provider_settings(),
            client=mocked_client(
                lambda _request: httpx.Response(
                    200, json={"features": [{"properties": {"name": "No coordinates"}}]}
                )
            ),
            cache=FakeCache(),
        )  # type: ignore[arg-type]
        with pytest.raises(TravelAPIResponseError):
            await tool.geocode_destination("Incomplete place")

    asyncio.run(run())


@pytest.mark.parametrize("query,formatted", [("東京", "東京, 日本"), ("São Paulo", "Sao Paulo, Brasil"), ("الرياض", "الرياض, السعودية")])
def test_location_matching_preserves_native_names_and_accents(query, formatted):
    from app.tools.geoapify import _location_tokens
    from app.agents.accommodation_transport_agent import _text_tokens

    assert _location_tokens(query)
    assert _location_tokens(query).issubset(_location_tokens(formatted))
    assert _text_tokens(query) == _location_tokens(query)
