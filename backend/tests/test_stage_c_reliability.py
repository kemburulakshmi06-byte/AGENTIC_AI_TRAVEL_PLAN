"""Offline provider and workflow regressions; no production database or Redis."""
import asyncio
import json
import logging
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from google.genai import errors

from app.core.config import Settings
from app.core.diagnostics import diagnostic_context, safe_failure_reason
from app.core.retries import retry_delay
from app.services.gemini_service import GeminiService, GeminiInvalidResponseError
from app.services.trip_planning_service import TripPlanningService
from app.tools.base import TravelAPIError
from app.tools.geoapify import GeoapifyTool
from app.tools.open_meteo import OpenMeteoTool
from app.tools.openrouteservice import OpenRouteServiceTool
from app.tools.tavily import TavilyTool
from test_external_api_tools import FakeCache, mocked_client, provider_settings
from test_gemini_service import install_fake_client, settings as gemini_settings, SampleResponse
from test_dynamic_destinations import DynamicProvider, location, request
from test_cost_preferences_flow import run_real_agents


@pytest.mark.parametrize("failure", [429, 503, "timeout", "network"])
def test_transient_provider_failure_retries_and_preserves_actual_response(monkeypatch, failure, caplog):
    async def run():
        calls = []
        def handler(req):
            calls.append(req)
            if len(calls) == 1:
                if failure == "timeout":
                    raise httpx.ReadTimeout("private detail")
                if failure == "network":
                    raise httpx.ConnectError("private detail")
                return httpx.Response(failure, headers={"Retry-After": "0"}, json={"private": "do not log"})
            return httpx.Response(200, json={"results": []})
        sleep = AsyncMock()
        monkeypatch.setattr("app.tools.base.asyncio.sleep", sleep)
        tool = TavilyTool(provider_settings(), client=mocked_client(handler), cache=FakeCache())
        with diagnostic_context(trip_id=uuid4(), agent="destination_research"):
            result = await tool.search_travel_information("Fixture destination tips")
        assert result.sources == []
        assert len(calls) == 2
        assert sleep.await_count == 1
        events = [json.loads(record.message) for record in caplog.records if record.name == "travel.diagnostics"]
        assert any(event["retry_count"] == 1 and event["trip_id"] and event["agent"] == "destination_research" for event in events)
        assert "do not log" not in caplog.text
        assert "tavily-test-key" not in caplog.text
    caplog.set_level(logging.INFO, logger="travel.diagnostics")
    asyncio.run(run())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_nonretryable_provider_errors_make_only_one_request(monkeypatch, status):
    async def run():
        calls = []
        def handler(req):
            calls.append(req)
            return httpx.Response(status, text="private secret")
        sleep = AsyncMock()
        monkeypatch.setattr("app.tools.base.asyncio.sleep", sleep)
        tool = TavilyTool(provider_settings(), client=mocked_client(handler), cache=FakeCache())
        with pytest.raises(TravelAPIError) as raised:
            await tool.search_travel_information("Fixture tips")
        assert len(calls) == 1
        sleep.assert_not_awaited()
        assert "private secret" not in str(raised.value)
        assert "private secret" not in safe_failure_reason(raised.value)
    asyncio.run(run())


def test_retry_limit_and_long_retry_after_are_respected(monkeypatch):
    async def run():
        for wait, expected in [("0", 3), ("60", 1)]:
            calls = []
            def handler(req):
                calls.append(req)
                return httpx.Response(429, headers={"Retry-After": wait})
            monkeypatch.setattr("app.tools.base.asyncio.sleep", AsyncMock())
            tool = TavilyTool(provider_settings(), client=mocked_client(handler), cache=FakeCache())
            with pytest.raises(TravelAPIError):
                await tool.search_travel_information("Fixture tips")
            assert len(calls) == expected
        assert retry_delay(0, retry_after=60) is None
        assert 0.25 <= retry_delay(0) <= 0.5
    asyncio.run(run())


@pytest.mark.parametrize("reason,category", [("MAX_TOKENS", "truncated"), ("SAFETY", "refusal")])
def test_gemini_truncated_and_refused_output_is_not_success(monkeypatch, reason, category):
    async def run():
        _, calls, _ = install_fake_client(monkeypatch, [SimpleNamespace(text='{"status":"ok","count":1}', candidates=[SimpleNamespace(finish_reason=reason)])])
        service = GeminiService(gemini_settings(GEMINI_MAX_RETRIES=2))
        with pytest.raises(GeminiInvalidResponseError) as raised:
            await service.generate_structured("Fixture prompt", SampleResponse)
        assert raised.value.category == category
        assert len(calls.calls) == 1
        await service.aclose()
    asyncio.run(run())


def test_gemini_invalid_request_is_not_retried_and_sdk_retries_are_disabled(monkeypatch):
    async def run():
        _, calls, init = install_fake_client(monkeypatch, [errors.APIError(400, {"error": {"message": "private response"}})])
        service = GeminiService(gemini_settings(GEMINI_MAX_RETRIES=2))
        with pytest.raises(Exception) as raised:
            await service.generate_structured("Fixture prompt", SampleResponse)
        assert "private response" not in str(raised.value)
        assert len(calls.calls) == 1
        assert init[0]["http_options"].retry_options.attempts == 1
        config = calls.calls[0]["config"]
        assert config.response_json_schema == SampleResponse.model_json_schema()
        assert config.response_schema is None
        await service.aclose()
    asyncio.run(run())


def test_geoapify_filters_far_places_deduplicates_and_keeps_valid_partial_results():
    async def run():
        good = {"properties": {"lat": 10.001, "lon": 20, "name": "Nearby fixture", "place_id": "fixture-id"}}
        payload = {"features": [good, good, {"properties": {"lat": 50, "lon": 100, "name": "Distant fixture"}}, {"properties": {"lat": 500, "lon": 20, "name": "Invalid fixture"}}]}
        tool = GeoapifyTool(provider_settings(), client=mocked_client(lambda req: httpx.Response(200, json=payload)), cache=FakeCache())
        result = await tool.find_nearby_places(10, 20)
        assert [place.place_id for place in result] == ["fixture-id"]
    asyncio.run(run())


def test_invalid_cached_response_is_refetched_instead_of_breaking_a_trip():
    async def run():
        cache = FakeCache()
        key = ("destination", repr({"query": "fixture", "language": "en"}))
        cache.values[key] = {"latitude": "invalid"}
        tool = GeoapifyTool(provider_settings(), client=mocked_client(lambda req: httpx.Response(200, json={"features": []})), cache=cache)
        assert await tool.geocode_destination("Fixture") is None
        assert cache.values[key] is None
    asyncio.run(run())


def test_ors_no_route_category_is_safe_and_nonretryable():
    async def run():
        calls = []
        def handler(req):
            calls.append(req)
            assert json.loads(req.content)["coordinates"] == [[20, 10], [21, 11]]
            return httpx.Response(404, json={"error": {"code": 2010, "message": "private provider coordinates"}})
        tool = OpenRouteServiceTool(provider_settings(), client=mocked_client(handler), cache=FakeCache())
        with pytest.raises(TravelAPIError) as raised:
            await tool.calculate_route((10, 20), (11, 21))
        assert raised.value.category == "no_route"
        assert "supported route" in safe_failure_reason(raised.value)
        assert len(calls) == 1
    asyncio.run(run())


def test_forecast_response_must_match_requested_local_dates():
    async def run():
        payload = {"daily": {"time": ["2027-03-02"], "temperature_2m_max": [10], "temperature_2m_min": [5], "precipitation_sum": [0], "weather_code": [0]}}
        def handler(req):
            assert req.url.params["timezone"] == "auto"
            return httpx.Response(200, json=payload)
        tool = OpenMeteoTool(provider_settings(), client=mocked_client(handler), cache=FakeCache(), clock=lambda: date(2027, 3, 1))
        with pytest.raises(TravelAPIError) as raised:
            await tool.get_weather(10, 20, date(2027, 3, 1), date(2027, 3, 1))
        assert raised.value.category == "invalid_schema"
    asyncio.run(run())


def test_tavily_deduplicates_sources_without_verifying_search_content():
    async def run():
        source = {"title": "Fixture", "url": "https://example.org/fixture", "content": "Unverified snippet"}
        tool = TavilyTool(provider_settings(), client=mocked_client(lambda req: httpx.Response(200, json={"results": [source, source]})), cache=FakeCache())
        result = await tool.search_travel_information("Fixture tips")
        assert len(result.sources) == 1
        assert result.sources[0].content_is_untrusted
    asyncio.run(run())


def test_six_real_agents_preserve_results_when_weather_and_search_fail():
    class PartialProvider(DynamicProvider):
        async def get_weather(self, *args, **kwargs):
            raise TravelAPIError("open-meteo", "private detail", category="timeout")
        async def search_travel_information(self, *args, **kwargs):
            raise TravelAPIError("tavily", "private detail", category="rate_limit")
    provider = PartialProvider({"Origin": location("Origin", "Country", 10, 20), "City": location("City", "Country", 11, 21)})
    state = asyncio.run(run_real_agents(request("Origin", "City"), provider))
    assert state["execution_status"] == "COMPLETED"
    assert state["optimization_review"].approved
    assert state["destination_research"].attractions
    assert not state["destination_research"].weather_summary.forecast_available
    assert any("timed out" in warning for warning in state["warnings"])
    assert any("rate limit" in warning for warning in state["warnings"])
    assert "private detail" not in str(state["warnings"])
    assert state["budget_breakdown"].pricing_status == "COMPLETE_ESTIMATE"


def test_result_persistence_failure_sets_a_terminal_status():
    async def run():
        status = SimpleNamespace(update_task_status=AsyncMock(), delete_task_status=AsyncMock())
        service = TripPlanningService(lambda: None, status, workflow=AsyncMock(return_value={"execution_status": "COMPLETED"}), settings=Settings(_env_file=None))
        service._mark_running = Mock(return_value=True)
        service._persist_result = Mock(side_effect=RuntimeError("private detail"))
        service._persist_failure = Mock(return_value=True)
        await service.run_trip(uuid4(), request("Origin", "City"))
        assert service._persist_failure.call_args.args[1] == "failed"
        assert status.update_task_status.call_args.kwargs["status"] == "failed"
        assert "private detail" not in str(status.update_task_status.call_args)
    asyncio.run(run())


@pytest.mark.parametrize("existing_status,accepted", [("queued", True), ("running", False), ("completed", False), ("failed", False)])
def test_duplicate_execution_cannot_restart_a_running_or_terminal_trip(existing_status, accepted):
    trip = SimpleNamespace(status=existing_status)
    session = Mock()
    session.begin.return_value = nullcontext()
    session.execute.return_value.scalar_one_or_none.return_value = trip
    service = TripPlanningService(lambda: nullcontext(session), Mock(), settings=Settings(_env_file=None))
    assert service._mark_running(uuid4()) is accepted
    assert trip.status == ("running" if accepted else existing_status)


def test_lost_background_task_is_reported_as_failed_and_polling_can_stop():
    async def run():
        now = datetime.now(timezone.utc)
        data = {"status": "running", "created_at": now - timedelta(hours=1), "updated_at": now - timedelta(hours=1), "error": None}
        status = SimpleNamespace(get_task_status=AsyncMock(return_value=None), update_task_status=AsyncMock())
        service = TripPlanningService(lambda: None, status, settings=Settings(_env_file=None))
        service._get_status_source = Mock(side_effect=[data, {**data, "status": "failed", "error": "Background worker stopped."}])
        service._persist_failure = Mock(return_value=True)
        result = await service.get_trip_status(uuid4())
        assert result["status"] == "failed"
        assert result["progress_percentage"] == 100
        assert result["error_message"] == "Background worker stopped."
    asyncio.run(run())


def test_gemini_long_retry_info_stops_requests_instead_of_ignoring_rate_limits(monkeypatch):
    async def run():
        error = errors.APIError(429, {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "60s"}]}})
        _, calls, _ = install_fake_client(monkeypatch, [error])
        service = GeminiService(gemini_settings(GEMINI_MAX_RETRIES=2))
        with pytest.raises(Exception) as raised:
            await service.generate_content("Fixture prompt")
        assert raised.value.category == "rate_limit"
        assert len(calls.calls) == 1
        await service.aclose()
    asyncio.run(run())


def test_itinerary_schema_is_request_sized_and_short_refs_restore_provider_ids():
    from app.agents.itinerary_agent import ItineraryAgent
    from app.schemas.agent_schemas import AgentContext
    class GroundedGenerator:
        async def generate_structured(self, prompt, response_model, **kwargs):
            data = json.loads(prompt)
            schema = response_model.model_json_schema()
            assert schema["properties"]["days"]["maxItems"] == len(data["trip_dates"])
            assert schema["$defs"]["RequestedDraftDay"]["properties"]["attraction_place_ids"]["maxItems"] == 3
            assert all(place["place_id"].startswith("ref_") for place in data["verified_attractions"])
            return response_model.model_validate({"days": [{"date": data["trip_dates"][0], "theme": "Provider visits", "attraction_place_ids": [data["verified_attractions"][0]["place_id"]]}]})
    async def run():
        provider = DynamicProvider({"Origin": location("Origin", "Country", 10, 20), "City": location("City", "Country", 11, 21)})
        trip_request = request("Origin", "City")
        state = await run_real_agents(trip_request, provider)
        result = await ItineraryAgent(settings=Settings(_env_file=None), gemini_service=GroundedGenerator(), routing=provider).run(AgentContext(
            request=trip_request, normalized_requirements=state["normalized_requirements"],
            destination_research=state["destination_research"].model_dump(mode="json"), budget_breakdown=state["budget_breakdown"].model_dump(mode="json")))
        ids = {place.place_id for place in state["destination_research"].attractions}
        selected = [activity.place_id for day in result.output.days for activity in day.activities if activity.activity_type == "attraction"]
        assert selected
        assert set(selected).issubset(ids)
        assert not any(place_id.startswith("ref_") for place_id in selected)
    asyncio.run(run())


def test_destination_short_refs_restore_provider_ids_before_grounding():
    from app.agents.destination_research_agent import DestinationResearchAgent
    from app.schemas.agent_schemas import AgentContext
    class GroundedGenerator:
        async def generate_structured(self, prompt, response_model, **kwargs):
            assert '"place_id": "ref_1"' in prompt
            return response_model.model_validate({"summary": "Supplied provider facts.", "ranked_place_ids": ["ref_1"], "recommended_activities": [{"title": "Provider visit", "description": "Visit this supplied location.", "related_place_ids": ["ref_1"]}]})
    async def run():
        provider = DynamicProvider({"Origin": location("Origin", "Country", 10, 20), "City": location("City", "Country", 11, 21)})
        trip_request = request("Origin", "City")
        state = await run_real_agents(trip_request, provider)
        result = await DestinationResearchAgent(settings=Settings(_env_file=None), gemini_service=GroundedGenerator(), geoapify=provider, weather=provider, tavily=provider, routing=provider).run(AgentContext(request=trip_request, normalized_requirements=state["normalized_requirements"]))
        assert result.output.recommended_activities[0].related_place_ids == ["City-0"]
        assert not any("unsupported" in warning.lower() for warning in result.warnings)
    asyncio.run(run())


def test_all_null_weather_measurements_are_unavailable():
    async def run():
        payload = {"daily": {"time": ["2027-03-01"], "temperature_2m_max": [None], "temperature_2m_min": [None], "precipitation_sum": [None], "weather_code": [None]}}
        tool = OpenMeteoTool(provider_settings(), client=mocked_client(lambda req: httpx.Response(200, json=payload)), cache=FakeCache(), clock=lambda: date(2027, 3, 1))
        with pytest.raises(TravelAPIError) as raised:
            await tool.get_weather(10, 20, date(2027, 3, 1), date(2027, 3, 1))
        assert raised.value.category == "empty_response"
    asyncio.run(run())


def test_missing_currency_cannot_be_persisted_as_an_assumed_currency():
    from app.schemas.agent_schemas import TravelRequest
    from app.services.trip_planning_service import TripPersistenceError
    factory = Mock()
    service = TripPlanningService(factory, Mock(), settings=Settings(_env_file=None))
    with pytest.raises(TripPersistenceError):
        service._create_trip(uuid4(), TravelRequest())
    factory.assert_not_called()
