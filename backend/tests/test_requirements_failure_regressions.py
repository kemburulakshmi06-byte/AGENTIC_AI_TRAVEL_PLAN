"""Deterministic, isolated LLM fixtures; never used by application code."""
import asyncio
import json
import logging
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.agents.requirements_agent import RequirementsAgent
from app.core.config import Settings
from app.core.diagnostics import diagnostic_context
from app.schemas.agent_schemas import AgentContext, TravelRequestInterpretation, TripCostPreferences
from app.services.gemini_service import GeminiInvalidResponseError
from app.services.trip_planning_service import TripPlanningService
from app.workflows.travel_graph import plan_trip
from test_requirements_agent import FakeGemini, valid_request
from test_travel_graph import agent_set, request as graph_request


@pytest.mark.parametrize("origin,destination,short_destination,currency,travelers", [
    ("Hyderabad", "Tokyo, Japan", "Tokyo", "INR", 2),
    ("Delhi", "Paris, France", "Paris", "EUR", 1),
    ("Mumbai", "Dubai, United Arab Emirates", "Dubai", "AED", 4),
    ("Bengaluru", "Panaji, Goa, India", "Panaji", "INR", 3),
])
def test_qualified_destination_and_soft_style_do_not_create_false_conflicts(origin, destination, short_destination, currency, travelers):
    trip = valid_request(origin=origin, destination=destination, currency=currency,
        travelers_count=travelers, travel_style="balanced",
        user_message=f"Plan a comfortable three-day {short_destination} trip.")
    agent = RequirementsAgent(gemini_service=FakeGemini(TravelRequestInterpretation(
        destination=short_destination, travel_style="comfortable")))
    result = asyncio.run(agent.run(AgentContext(request=trip)))
    assert result.status == "completed"
    assert not result.output.conflicting_requirements
    for field in ("origin", "destination", "currency", "travelers_count", "total_budget", "start_date", "end_date"):
        assert getattr(result.output, field) == getattr(trip, field)
    assert result.output.travel_style == "balanced"
    assert any("inferred free-text travel style" in warning for warning in result.warnings)


@pytest.mark.parametrize("explicit,extracted", [
    ("Tokyo, Japan", "Kyoto, Japan"),
    ("Springfield, Illinois, USA", "Springfield, Missouri, USA"),
    ("Japan", "Tokyo, Japan"),
    ("New York, USA", "York New, USA"),
    ("York, UK", "New York, USA"),
])
def test_different_cities_regions_or_location_order_still_require_clarification(explicit, extracted):
    result = asyncio.run(RequirementsAgent(gemini_service=FakeGemini(
        TravelRequestInterpretation(destination=extracted))).run(AgentContext(
            request=valid_request(destination=explicit, user_message=f"Travel to {extracted}."))))
    assert result.status == "needs_input"
    assert "explicit destination conflicts with free-text interpretation" in result.output.conflicting_requirements
    assert result.output.destination == explicit


@pytest.mark.parametrize("field,value", [
    ("start_date", date(2027, 1, 9)), ("end_date", date(2027, 1, 13)),
    ("travelers_count", 5), ("total_budget", Decimal("10000")), ("currency", "USD"),
])
def test_genuine_hard_field_conflicts_are_not_converted_to_success(field, value):
    extracted = TravelRequestInterpretation(**{field: value})
    result = asyncio.run(RequirementsAgent(gemini_service=FakeGemini(extracted)).run(
        AgentContext(request=valid_request(user_message=f"Requested {field}: {value}"))))
    assert result.status == "needs_input"
    assert any(field in conflict for conflict in result.output.conflicting_requirements)


@pytest.mark.parametrize("preferences", [None, TripCostPreferences(
    room_count=2, accommodation_nightly_rate_per_room=Decimal("100.25"),
    transport_mode="train", food_allowance_per_traveler_per_day=Decimal("20.50"),
    contingency_percentage=Decimal("5"))])
def test_optional_partial_cost_preferences_are_preserved_without_inferred_prices(preferences):
    result = asyncio.run(RequirementsAgent(gemini_service=FakeGemini(
        TravelRequestInterpretation(destination="Goa", travel_style="comfortable"))).run(
            AgentContext(request=valid_request(destination="Goa, India", cost_preferences=preferences,
                user_message="Comfortable Goa trip."))))
    assert result.status == "completed"
    assert result.output.cost_preferences == preferences


@pytest.mark.parametrize("unexpected", [None, [], "invalid JSON", {"unknown_field": "SECRET"},
    {"travelers_count": 0}, {"start_date": "2027-02-03", "end_date": "2027-02-01"}])
def test_unexpected_llm_shapes_are_validated_and_do_not_replace_structured_inputs(unexpected):
    result = asyncio.run(RequirementsAgent(gemini_service=FakeGemini(unexpected)).run(
        AgentContext(request=valid_request(user_message="A relaxed trip."))))
    assert result.status == "completed"
    assert result.output.destination == "Goa"
    assert not result.output.conflicting_requirements
    assert any("Could not interpret" in warning for warning in result.warnings)
    assert "SECRET" not in str(result.warnings)


def test_invalid_llm_response_cannot_complete_a_text_only_request():
    from app.schemas.agent_schemas import TravelRequest
    result = asyncio.run(RequirementsAgent(gemini_service=FakeGemini(None)).run(
        AgentContext(request=TravelRequest(user_message="Plan a trip."))))
    assert result.status == "needs_input"
    assert result.output.missing_required_fields
    assert result.output.destination is None


def test_directly_stated_conflicting_style_is_not_treated_as_a_soft_inference():
    trip = valid_request(travel_style="balanced", user_message="Travel style: luxury.")
    result = asyncio.run(RequirementsAgent(gemini_service=FakeGemini(
        TravelRequestInterpretation(travel_style="luxury"))).run(AgentContext(request=trip)))
    assert result.status == "needs_input"
    assert "explicit travel_style conflicts with free-text interpretation" in result.output.conflicting_requirements
    assert not any("inferred free-text travel style" in warning for warning in result.warnings)


def test_constructed_invalid_llm_model_is_revalidated_before_merge():
    unexpected = TravelRequestInterpretation.model_construct(travelers_count=0)
    result = asyncio.run(RequirementsAgent(gemini_service=FakeGemini(unexpected)).run(
        AgentContext(request=valid_request(user_message="Comfortable trip."))))
    assert result.status == "completed"
    assert result.output.travelers_count == 2
    assert any("Could not interpret" in warning for warning in result.warnings)


def test_diagnostics_identify_rules_stage_and_trip_without_input_values(caplog):
    caplog.set_level(logging.INFO, logger="travel.diagnostics")
    trip_id = uuid4()
    trip = valid_request(user_message="SECRET_PROMPT", interests=["SECRET_INTEREST"],
                         constraints={"minimum_budget": "30000", "maximum_budget": "20000",
                                      "excluded_interests": ["SECRET_INTEREST"]})
    with diagnostic_context(trip_id=trip_id, agent="requirements"):
        result = asyncio.run(RequirementsAgent(gemini_service=FakeGemini(
            GeminiInvalidResponseError("SECRET_PROVIDER_BODY"))).run(AgentContext(request=trip)))
    assert result.status == "needs_input"
    events = [json.loads(record.message) for record in caplog.records if record.name == "travel.diagnostics"]
    rules = {event.get("rule") for event in events}
    assert {"llm_response_invalid", "minimum_exceeds_maximum", "minimum_exceeds_total", "selected_and_excluded"} <= rules
    assert all(event["agent"] == "requirements" and event["trip_id"] == str(trip_id) for event in events)
    assert "SECRET" not in caplog.text
    assert "30000" not in caplog.text


def test_real_workflow_and_background_service_keep_genuine_conflict_failed():
    async def run():
        trip = graph_request().model_copy(update={"constraints": {"minimum_budget": "30000"}})
        agents = agent_set(requirements_agent=RequirementsAgent(settings=Settings(_env_file=None)))
        async def workflow(request, **kwargs):
            return await plan_trip(request, agents=agents, **kwargs)
        task_status = SimpleNamespace(update_task_status=AsyncMock(), delete_task_status=AsyncMock())
        service = TripPlanningService(lambda: None, task_status, workflow=workflow, settings=Settings(_env_file=None))
        service._mark_running = Mock(return_value=True)
        service._persist_result = Mock(return_value=True)
        await service.run_trip(uuid4(), trip)
        state = service._persist_result.call_args.args[1]
        assert state["execution_status"] == "INVALID_REQUEST"
        assert state["current_stage"] == "requirements"
        assert "minimum budget exceeds total budget" in state["normalized_requirements"].conflicting_requirements
        assert not agents.destination_research.calls
        assert not agents.itinerary.calls
        terminal = task_status.update_task_status.call_args.kwargs
        assert terminal["status"] == "failed"
        assert terminal["current_agent"] == "requirements"
        assert terminal["error_message"] == "The request contains conflicting travel requirements."
    asyncio.run(run())


def test_unexpected_agent_envelope_fails_with_a_distinct_diagnostic(caplog):
    class UnexpectedAgent:
        agent_name = "requirements"
        async def run(self, context):
            return {"unexpected": "SECRET_AGENT_PAYLOAD"}
        async def aclose(self):
            pass
    caplog.set_level(logging.INFO, logger="travel.diagnostics")
    agents = agent_set(requirements_agent=UnexpectedAgent())
    state = asyncio.run(plan_trip(graph_request(), agents=agents))
    assert state["execution_status"] == "FAILED"
    assert state["current_stage"] == "requirements"
    assert any("invalid result" in message for message in state["errors"])
    assert not any("conflicting travel requirements" in message for message in state["errors"])
    assert not agents.destination_research.calls
    assert "unexpected_agent_response" in caplog.text
    assert "SECRET_AGENT_PAYLOAD" not in caplog.text
