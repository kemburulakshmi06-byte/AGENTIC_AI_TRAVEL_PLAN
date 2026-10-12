"""Isolated provider responses; names and data here are test fixtures only."""
import asyncio
import json
import httpx
import pytest

from app.schemas.external import GeocodingResult
from app.tools.base import TravelAPIError
from app.tools.geoapify import GeoapifyTool
from app.tools.place_interests import matches_interest
from test_destination_research_agent import context, requirements, make_agent, FakeGeoapify, place
from test_dynamic_destinations import DynamicProvider, location, request
from test_cost_preferences_flow import run_real_agents
from test_external_api_tools import FakeCache, provider_settings, mocked_client


def test_legacy_cached_country_is_refetched_before_discovering_cities():
    async def run():
        cache = FakeCache()
        identifier = {"query": "fixture country", "language": "en"}
        legacy = {"latitude": 20, "longitude": 30, "formatted_address": "Fixture Country",
                  "country": "Fixture Country", "country_code": "xx"}
        cache.values[("destination", repr(identifier))] = legacy
        # This is the exact historical failure, using the previously cached schema.
        old = GeocodingResult.model_validate(legacy)
        tool = GeoapifyTool(provider_settings(), cache=cache, client=mocked_client(handler))
        with pytest.raises(ValueError, match="boundary"):
            await tool.find_destination_cities(old)
        refreshed = await tool.geocode_destination("Fixture Country")
        assert refreshed.result_type == "country"
        assert refreshed.place_id == "country-boundary"
        cities = await tool.find_destination_cities(refreshed)
        assert cities[0].city == "Fixture City"
        assert cache.values[("destination", repr(identifier))]["place_id"] == "country-boundary"
        await tool.aclose()

    def handler(req):
        if "geocode" in req.url.path:
            return httpx.Response(200, json={"features": [{"properties": {
                "lat": 20, "lon": 30, "formatted": "Fixture Country", "name": "Fixture Country",
                "country_code": "xx", "result_type": "country", "place_id": "country-boundary",
            }}]})
        assert req.url.params["filter"] == "place:country-boundary"
        return httpx.Response(200, json={"features": [{"properties": {
            "lat": 21, "lon": 31, "formatted": "Fixture City", "city": "Fixture City", "country_code": "xx",
        }}]})
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["country", "state"])
def test_broad_area_uses_category_evidence_or_requests_clarification(kind):
    locations = {"Area": location("Area", "Area", 20, 100, kind),
                 "Base": location("Base", "Area", 21, 101), "Other": location("Other", "Area", 22, 102)}
    provider = DynamicProvider(locations, {"Area": ["Base", "Other"]},
                               {"Base": ["tourism.sights.castle"], "Other": ["natural.wood"]})
    state = asyncio.run(run_real_agents(request("Origin", "Area", interests=["Culture"]), provider))
    if kind == "state":
        assert state["destination_research"].resolution_status == "verified"
        assert "Base" in state["destination_research"].destination_name
        assert state["optimization_review"].approved
    else:
        assert state["destination_research"].resolution_status == "clarification_required"
        assert not provider.place_calls
    provider.categories["Other"] = ["tourism.sights.castle"]
    tied = asyncio.run(run_real_agents(request("Origin", "Area", interests=["Culture"]), provider))
    assert tied["execution_status"] == "INSUFFICIENT_DATA"
    assert "clarification_required" in " ".join(tied["errors"])
    assert tied["destination_research"].clarification_questions
    assert tied["destination_research"].location_candidates
    assert "trip_itinerary" not in tied


@pytest.mark.parametrize("interest,category", [("Food", "catering.restaurant"),
    ("Nature", "natural.wood"), ("Culture", "tourism.sights.castle")])
def test_city_research_retains_valid_semantic_interest_matches(interest, category):
    result = asyncio.run(make_agent(geo=FakeGeoapify(places=[place("verified-id", "Provider name", categories=[category])]))
                         .run(context(requirements(interests=[interest]))))
    assert result.status == "completed"
    assert result.output.resolution_status == "verified"
    assert [item.place_id for item in result.output.attractions] == ["verified-id"]
    assert matches_interest(result.output.attractions[0], [interest])


@pytest.mark.parametrize("category,expected", [("timeout", "provider_unavailable"),
    ("rate_limit", "provider_unavailable"), ("invalid_coordinates", "invalid_coordinates"),
    ("irrelevant_results", "irrelevant_results")])
def test_geocoding_failure_has_specific_sanitized_reason(category, expected):
    geo = FakeGeoapify(geocode_error=TravelAPIError("geoapify", "PRIVATE SECRET", category=category))
    result = asyncio.run(make_agent(geo=geo).run(context()))
    assert result.status == "needs_input"
    assert result.output.resolution_status == expected
    assert not geo.place_calls
    assert "PRIVATE SECRET" not in str(result)


def test_empty_places_require_input_without_fabricating_itinerary():
    result = asyncio.run(make_agent(geo=FakeGeoapify(places=[])).run(context()))
    assert result.status == "needs_input"
    assert result.output.resolution_status == "no_verified_places"
    assert result.output.attractions == []
    assert result.output.clarification_questions


@pytest.mark.parametrize("invalid", [float("nan"), 91])
def test_invalid_provider_coordinates_are_diagnosed(invalid):
    async def run():
        payload = {"features": [{"properties": {
            "lat": invalid, "lon": 20, "formatted": "Requested City"}}]}
        tool = GeoapifyTool(provider_settings(), cache=FakeCache(), client=mocked_client(
            lambda req: httpx.Response(200, content=json.dumps(payload))))
        with pytest.raises(TravelAPIError) as raised:
            await tool.geocode_destination("Requested City")
        assert raised.value.category == "invalid_coordinates"
        await tool.aclose()
    asyncio.run(run())


def test_city_discovery_keeps_valid_records_and_rejects_other_countries():
    async def run():
        features = [{"properties": {"lat": lat, "lon": 20, "formatted": name, "country_code": code}}
                    for lat, name, code in [(95, "Invalid", "xx"), (11, "Valid", "xx"), (12, "Other country", "yy")]]
        tool = GeoapifyTool(provider_settings(), cache=FakeCache(), client=mocked_client(
            lambda req: httpx.Response(200, json={"features": features})))
        parent = GeocodingResult(latitude=10, longitude=20, formatted_address="Country",
                                 result_type="country", country_code="xx")
        cities = await tool.find_destination_cities(parent)
        assert [city.formatted_address for city in cities] == ["Valid"]
        await tool.aclose()
    asyncio.run(run())


def test_interest_queries_use_provider_categories_and_lon_lat_radius():
    async def run():
        calls = []
        def handler(req):
            calls.append(req.url.params["categories"])
            assert req.url.params["filter"] == "circle:20,10,5000"
            assert req.url.params["bias"] == "proximity:20,10"
            return httpx.Response(200, json={"features": [{"properties": {
                "lat": 10, "lon": 20, "name": "Verified POI", "place_id": req.url.params["categories"],
                "categories": [req.url.params["categories"].split(",")[0]],
            }}]})
        tool = GeoapifyTool(provider_settings(), cache=FakeCache(), client=mocked_client(handler))
        result = await tool.find_interest_places(10, 20, interests=["Food", "Nature", "Culture"])
        assert len(result) == 4
        assert any("catering.restaurant" in value for value in calls)
        assert any("natural" in value for value in calls)
        assert any("entertainment.museum" in value for value in calls)
        await tool.aclose()
    asyncio.run(run())


def test_partial_interest_provider_failure_retains_verified_places_and_warning():
    async def run():
        def handler(req):
            if "catering" in req.url.params["categories"]:
                return httpx.Response(429)
            return httpx.Response(200, json={"features": [{"properties": {
                "lat": 10, "lon": 20, "name": "Verified sight", "place_id": "provider-id",
                "categories": ["tourism.sights"],
            }}]})
        settings = provider_settings().model_copy(update={"EXTERNAL_API_MAX_RETRIES": 0})
        tool = GeoapifyTool(settings, cache=FakeCache(), client=mocked_client(handler))
        warnings = []
        results = await tool.find_interest_places(10, 20, interests=["Food"], warnings=warnings)
        assert [place.place_id for place in results] == ["provider-id"]
        assert any("rate limit" in warning for warning in warnings)
        await tool.aclose()
    asyncio.run(run())


def test_region_does_not_select_a_base_when_candidate_provider_comparison_fails():
    class PartialProvider(DynamicProvider):
        async def find_nearby_places(self, latitude, longitude, **kwargs):
            if latitude == 22:
                raise TravelAPIError("geoapify", "private detail", category="timeout")
            return await super().find_nearby_places(latitude, longitude, **kwargs)
    provider = PartialProvider({"Area": location("Area", "Area", 20, 100, "state"),
        "First": location("First", "Area", 21, 101), "Second": location("Second", "Area", 22, 102)},
        {"Area": ["First", "Second"]})
    state = asyncio.run(run_real_agents(request("Origin", "Area"), provider))
    assert state["execution_status"] == "INSUFFICIENT_DATA"
    assert state["destination_research"].resolution_status == "provider_unavailable"
    assert "trip_itinerary" not in state
    assert "private detail" not in str(state["errors"])
