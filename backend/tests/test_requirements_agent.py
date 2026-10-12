import asyncio
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.requirements_agent import RequirementsAgent
from app.core.config import Settings
from app.schemas.agent_schemas import (
    AgentContext,
    NormalizedTravelRequirements,
    TravelRequest,
    TravelRequestInterpretation,
    TripCostPreferences,
)
from app.services.gemini_service import (
    GeminiInvalidResponseError,
    GeminiModelUnavailableError,
)


def valid_request(**overrides: Any) -> TravelRequest:
    values: dict[str, Any] = {
        "origin": "  Hyderabad ",
        "destination": " Goa ",
        "start_date": date(2027, 1, 10),
        "end_date": date(2027, 1, 12),
        "travelers_count": 2,
        "total_budget": Decimal("25000.00"),
        "currency": "inr",
        "travel_style": "  Cultural   Exploration ",
        "interests": ["  History ", "Food"],
        "constraints": {"max_daily_budget": "9000"},
    }
    values.update(overrides)
    return TravelRequest.model_validate(values)


class FakeGemini:
    def __init__(
        self,
        result: TravelRequestInterpretation | Exception,
    ) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        response_model: type[TravelRequestInterpretation],
        **kwargs: Any,
    ) -> TravelRequestInterpretation:
        self.calls.append(
            {"prompt": prompt, "response_model": response_model, **kwargs}
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_valid_structured_request_normalizes_fields_and_duration() -> None:
    async def run() -> None:
        result = await RequirementsAgent().run(
            AgentContext(request=valid_request())
        )
        assert result.status == "completed"
        assert result.agent_name == "requirements"
        assert isinstance(result.output, NormalizedTravelRequirements)
        assert result.output.origin == "Hyderabad"
        assert result.output.destination == "Goa"
        assert result.output.currency == "INR"
        assert result.output.travel_style == "cultural exploration"
        assert result.output.total_budget == Decimal("25000.00")
        assert result.output.travel_duration_days == 3
        assert result.output.constraints == {"max_daily_budget": "9000"}
        assert result.output.missing_required_fields == []

    asyncio.run(run())


def test_explicit_trip_cost_preferences_survive_requirements_normalization() -> None:
    async def run() -> None:
        preferences = TripCostPreferences(
            accommodation_nightly_rate_per_room=Decimal("2500"),
            room_count=1,
            transport_mode="train",
            intercity_round_trip_fare_per_traveler=Decimal("800"),
            local_transport_allowance_per_day=Decimal("200"),
            food_allowance_per_traveler_per_day=Decimal("500"),
            activities_allowance_per_traveler_per_trip=Decimal("1200"),
            contingency_percentage=Decimal("8"),
        )
        result = await RequirementsAgent().run(
            AgentContext(
                request=valid_request(cost_preferences=preferences)
            )
        )
        assert result.output.cost_preferences == preferences

    asyncio.run(run())


@pytest.mark.parametrize("field", ["origin", "destination"])
def test_missing_origin_or_destination_is_reported_without_fabrication(
    field: str,
) -> None:
    async def run() -> None:
        result = await RequirementsAgent().run(
            AgentContext(request=valid_request(**{field: None}))
        )
        assert result.status == "needs_input"
        assert getattr(result.output, field) is None
        assert field in result.output.missing_required_fields

    asyncio.run(run())


def test_missing_required_fields_are_returned_for_follow_up() -> None:
    async def run() -> None:
        request = TravelRequest()
        result = await RequirementsAgent().run(AgentContext(request=request))
        assert result.status == "needs_input"
        assert result.output.missing_required_fields == [
            "origin",
            "destination",
            "start_date",
            "end_date",
            "travelers_count",
            "total_budget",
            "currency",
        ]
        assert result.output.travel_duration_days is None

    asyncio.run(run())


def test_invalid_date_range_is_rejected() -> None:
    with pytest.raises(ValidationError, match="end_date"):
        valid_request(
            start_date=date(2027, 2, 2),
            end_date=date(2027, 2, 1),
        )


@pytest.mark.parametrize("travelers_count", [0, -1])
def test_zero_or_negative_travelers_are_rejected(travelers_count: int) -> None:
    with pytest.raises(ValidationError, match="travelers_count"):
        valid_request(travelers_count=travelers_count)


@pytest.mark.parametrize("total_budget", [Decimal("0"), Decimal("-1")])
def test_zero_or_negative_budget_is_rejected(total_budget: Decimal) -> None:
    with pytest.raises(ValidationError, match="total_budget"):
        valid_request(total_budget=total_budget)


@pytest.mark.parametrize("currency", ["", "US", "US1", "USDD"])
def test_currency_must_be_an_explicit_three_letter_code(currency: str) -> None:
    with pytest.raises(ValidationError, match="currency"):
        valid_request(currency=currency)


def test_interests_are_normalized_and_deduplicated_case_insensitively() -> None:
    async def run() -> None:
        result = await RequirementsAgent().run(
            AgentContext(
                request=valid_request(
                    interests=["  ART   museums", "art museums", "NATURE"]
                )
            )
        )
        assert result.output.interests == ["ART museums", "NATURE"]

    asyncio.run(run())


def test_contradictory_budget_and_interest_constraints_are_reported() -> None:
    async def run() -> None:
        request = valid_request(
            interests=["history"],
            constraints={
                "minimum_budget": "30000",
                "maximum_budget": "20000",
                "excluded_interests": ["History", "nightlife"],
                "required_interests": ["nightlife"],
            },
        )
        result = await RequirementsAgent().run(AgentContext(request=request))
        conflicts = result.output.conflicting_requirements
        assert result.status == "needs_input"
        assert "minimum budget exceeds maximum budget" in conflicts
        assert "minimum budget exceeds total budget" in conflicts
        assert "interest 'nightlife' is both required and excluded" in conflicts
        assert "interest 'history' is selected but explicitly excluded" in conflicts
        assert result.warnings
        assert result.output.constraints == request.constraints

    asyncio.run(run())


def test_free_text_is_extracted_as_untrusted_data_and_normalized() -> None:
    async def run() -> None:
        gemini = FakeGemini(
            TravelRequestInterpretation(
                origin="Chennai",
                destination="Pondicherry",
                start_date=date(2027, 4, 1),
                end_date=date(2027, 4, 3),
                travelers_count=3,
                total_budget=Decimal("18000"),
                currency="inr",
                travel_style="Beach break",
                interests=["Food", "heritage"],
                constraints={"mobility": "limited walking"},
            )
        )
        agent = RequirementsAgent(
            gemini_service=gemini,
            settings=Settings(_env_file=None),
        )
        result = await agent.run(
            AgentContext(
                request=TravelRequest(
                    user_message="Plan three days from Chennai to Pondicherry."
                )
            )
        )
        assert result.status == "completed"
        assert result.output.origin == "Chennai"
        assert result.output.destination == "Pondicherry"
        assert result.output.travelers_count == 3
        assert result.output.currency == "INR"
        assert result.output.travel_duration_days == 3
        assert result.output.interests == ["Food", "heritage"]
        assert result.output.constraints == {"mobility": "limited walking"}
        assert len(gemini.calls) == 1
        assert gemini.calls[0]["response_model"] is TravelRequestInterpretation
        assert gemini.calls[0]["temperature"] == 0
        assert "untrusted data" in gemini.calls[0]["system_instruction"]

    asyncio.run(run())


def test_explicit_values_override_conflicting_gemini_interpretation() -> None:
    async def run() -> None:
        gemini = FakeGemini(
            TravelRequestInterpretation(
                origin="Mumbai",
                destination="Jaipur",
                start_date=date(2027, 5, 1),
                end_date=date(2027, 5, 2),
                travelers_count=4,
                total_budget=Decimal("50000"),
                currency="USD",
                travel_style="luxury",
                interests=["Luxury shopping"],
                constraints={"seat": "window"},
            )
        )
        result = await RequirementsAgent(gemini_service=gemini).run(
            AgentContext(
                request=valid_request(
                    user_message="I want a trip to Jaipur for four people.",
                    destination="Goa",
                    travelers_count=2,
                    total_budget=Decimal("25000"),
                    currency="INR",
                )
            )
        )
        assert result.output.destination == "Goa"
        assert result.output.travelers_count == 2
        assert result.output.total_budget == Decimal("25000")
        assert result.output.currency == "INR"
        assert result.output.travel_style == "cultural exploration"
        assert result.status == "needs_input"
        assert result.output.constraints == {
            "max_daily_budget": "9000",
            "seat": "window",
        }
        assert "explicit destination conflicts with free-text interpretation" in (
            result.output.conflicting_requirements
        )
        assert "explicit travelers_count conflicts with free-text interpretation" in (
            result.output.conflicting_requirements
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    "gemini_error",
    [
        GeminiInvalidResponseError("invalid structured output"),
        GeminiModelUnavailableError("model unavailable"),
    ],
)
def test_malformed_or_unavailable_gemini_falls_back_safely(
    gemini_error: Exception,
) -> None:
    async def run() -> None:
        agent = RequirementsAgent(gemini_service=FakeGemini(gemini_error))
        result = await agent.run(
            AgentContext(
                request=valid_request(user_message="Maybe a trip to the mountains.")
            )
        )
        assert result.status == "completed"
        assert result.output.destination == "Goa"
        assert len(result.warnings) == 1
        assert "Could not interpret the free-text request" in result.warnings[0]
        assert result.errors == []

    asyncio.run(run())


def test_deterministic_validation_does_not_require_gemini_credentials() -> None:
    async def run() -> None:
        agent = RequirementsAgent(settings=Settings(_env_file=None))
        result = await agent.run(AgentContext(request=valid_request()))
        assert result.status == "completed"
        assert result.output.destination == "Goa"

    asyncio.run(run())


def test_currency_extracted_from_text_is_normalized() -> None:
    async def run() -> None:
        result = await RequirementsAgent(
            gemini_service=FakeGemini(
                TravelRequestInterpretation(currency="usd")
            )
        ).run(
            AgentContext(
                request=TravelRequest(user_message="Budget is in US dollars.")
            )
        )
        assert result.output.currency == "USD"

    asyncio.run(run())
