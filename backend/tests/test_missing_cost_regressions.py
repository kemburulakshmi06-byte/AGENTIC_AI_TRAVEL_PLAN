"""All injected prices/providers in these tests are isolated fixtures only."""
import asyncio
import json
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.agents.budget_agent import BudgetAgent
from app.core.config import Settings
from app.database.models import AgentRun
from app.services.cost_estimation_service import CostEstimationService
from app.services.trip_planning_service import TripPlanningService
from test_automatic_cost_estimation import (
    FXFixture, agent_context, all_agents, req, run_budget, stable_reference_date,
)


def test_invalid_reference_registry_does_not_discard_resolved_city_for_pricing():
    class Provider:
        calls = []

        async def flight_cost(self, requirements, destination, *, country_code=None):
            self.calls.append((destination, country_code))
            return None

    provider = Provider()
    settings = Settings(_env_file=None, AMADEUS_API_KEY="test-only", AMADEUS_API_SECRET="test-only")
    service = CostEstimationService(settings, pricing=provider, fx=FXFixture(), reference_data={"invalid": True})
    result = asyncio.run(BudgetAgent(settings=settings, cost_estimation_service=service).run(agent_context(
        req(destination="Japan"), {"destination_name": "Tokyo, Japan", "country_code": "jp"})))
    assert provider.calls == [("Tokyo, Japan", "jp")]
    assert result.output.estimated_total is None
    assert all(item.amount is None for item in result.output.line_items)


def test_reference_room_capacity_cannot_underprice_four_travelers_in_one_double_room():
    budget = run_budget(req(travelers_count=4, cost_preferences={"room_count": 1})).output
    assert next(item for item in budget.line_items if item.category == "accommodation").amount is None
    assert budget.estimated_total is None
    assert any("cannot accommodate" in action for action in budget.missing_price_actions)


def test_allowance_fallback_completes_unsupported_destination_without_fake_sources():
    request = req(origin="Mumbai", destination="Dubai", cost_preferences={
        "accommodation_nightly_rate_per_room": "100.01", "room_count": 1,
        "local_transport_allowance_per_day": "20.01",
        "intercity_round_trip_fare_per_traveler": "300.01",
        "food_allowance_per_traveler_per_day": "30.01",
        "activities_allowance_per_traveler_per_trip": "40.01", "contingency_percentage": "10",
    })
    budget = run_budget(request, research={"destination_name": "Dubai", "country_code": "ae"}).output
    assert budget.estimated_total == Decimal("1628.25")
    assert budget.pricing_status == "COMPLETE_ESTIMATE"
    assert not budget.missing_price_actions and not budget.missing_price_inputs
    assert all(item.source == "user_assumption" for item in budget.line_items)


def test_unknown_base_costs_leave_contingency_unknown_even_with_explicit_percentage():
    budget = run_budget(req(destination="Dubai", cost_preferences={"contingency_percentage": "12.5"}),
                        research={"destination_name": "Dubai", "country_code": "ae"}).output
    reserve = next(item for item in budget.line_items if item.category == "contingency")
    assert reserve.amount is None and reserve.classification == "unknown"
    assert budget.remaining_budget is None and budget.estimated_total is None
    assert any(action.startswith("contingency:") for action in budget.missing_price_actions)


def test_one_day_without_hotel_nights_needs_no_accommodation_quote():
    budget = run_budget(req(end_date="2026-10-15")).output
    accommodation = next(item for item in budget.line_items if item.category == "accommodation")
    assert accommodation.amount == Decimal("0.00")
    assert accommodation.source == "configured_assumption"
    assert budget.estimated_total is not None


def test_medium_reference_confidence_is_not_reported_as_unassessed():
    data = json.loads(Settings(_env_file=None).COST_REFERENCE_FILE.read_text(encoding="utf-8"))
    for row in data["rates"]:
        row["confidence"] = "medium"
    assert run_budget(data=data).output.estimate_confidence == "medium"


def test_failed_aed_conversion_keeps_amounts_unknown_and_actionable():
    budget = run_budget(req(currency="AED"), fx=FXFixture(fail=True)).output
    assert budget.estimated_total is None and all(item.amount is None for item in budget.line_items)
    assert any("JPY/AED" in action for action in budget.missing_price_actions)
    assert any("INR/AED" in action for action in budget.missing_price_actions)


@pytest.mark.parametrize("changes,rule", [
    ({"origin": "Unsupported origin"}, "unsupported_route"),
    ({"travel_style": "luxury"}, "unsupported_style"),
    ({"destination": "Osaka"}, "unsupported_city"),
    ({"cost_preferences": {"transport_mode": "train"}}, "unsupported_transport"),
])
def test_missing_price_diagnostics_identify_the_matching_rule(changes, rule, caplog):
    research = {"destination_name": changes.get("destination", "Tokyo"), "country_code": "jp"}
    budget = run_budget(req(**changes), research=research).output
    events = [json.loads(record.message) for record in caplog.records if record.name == "travel.diagnostics"]
    assert any(event["error_category"] == rule for event in events)
    assert budget.estimated_total is None


def test_disabled_estimator_and_invalid_metadata_explain_missing_inputs():
    budget = run_budget(settings=Settings(_env_file=None, AUTOMATIC_COST_ESTIMATION_ENABLED=False)).output
    assert any("automatic estimation is disabled" in action for action in budget.missing_price_actions)
    assert budget.estimated_total is None
    budget = run_budget(research={"country_code": ["invalid"]}).output
    assert any("destination country metadata is missing" in action for action in budget.missing_price_actions)
    assert budget.estimated_total is None


def test_coverage_diagnostics_include_rules_without_request_values_or_secrets(caplog):
    budget = run_budget(req(origin="Private Origin", destination="Private Destination"),
                        research={"destination_name": "Private Destination", "country_code": "ae"}).output
    events = [json.loads(record.message) for record in caplog.records if record.name == "travel.diagnostics"]
    # This module's shared reference date is 2026-10-11. UAE coverage exists,
    # but its reviewed records are observed on 2026-10-12 and must fail closed
    # rather than being reported as an unsupported country.
    assert any(event["error_category"] == "expired_reference" for event in events)
    assert "Private Origin" not in caplog.text and "Private Destination" not in caplog.text
    assert budget.estimated_total is None


def test_location_whitespace_does_not_stop_real_six_agent_workflow():
    state = asyncio.run(all_agents(req(origin="New  Delhi", destination="Tokyo")))
    assert state["execution_status"] == "COMPLETED"
    assert {item.agent_name for item in state["agent_history"]} == {
        "requirements", "destination_research", "accommodation_transport", "budget", "itinerary", "optimization_review"}


def test_persisted_agent_history_preserves_each_runs_output():
    state = asyncio.run(all_agents(req()))
    record = next(item for item in state["agent_history"] if item.agent_name == "budget")
    original = dict(record.output_data)
    state["budget_breakdown"] = state["budget_breakdown"].model_copy(update={"qualitative_summary": "Later revision"})
    session = MagicMock()
    TripPlanningService(session_factory=MagicMock(), task_status=MagicMock(), settings=Settings(_env_file=None))._save_agent_runs(session, uuid4(), state)
    saved = [call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], AgentRun)]
    assert next(item for item in saved if item.agent_name == "budget").output_data["result"] == original
    assert original["qualitative_summary"] != "Later revision"
