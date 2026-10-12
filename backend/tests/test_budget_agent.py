import asyncio
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.budget_agent import BudgetAgent
from app.core.config import Settings
from app.schemas.accommodation_transport import (
    AccommodationRecommendation,
    AccommodationTransportPlan,
    CostEstimate,
)
from app.schemas.agent_schemas import (
    AgentContext,
    NormalizedTravelRequirements,
    TravelRequest,
    TripCostPreferences,
)
from app.schemas.budget import BudgetNarrative
from app.services.gemini_service import GeminiModelUnavailableError


def requirements(**overrides: Any) -> NormalizedTravelRequirements:
    values: dict[str, Any] = {
        "origin": "Chennai",
        "destination": "Hyderabad",
        "start_date": date(2027, 1, 10),
        "end_date": date(2027, 1, 11),
        "travelers_count": 3,
        "total_budget": Decimal("30000.00"),
        "currency": "INR",
        "travel_duration_days": 2,
        "constraints": {
            "budget_assumptions": {
                "food": {
                    "per_traveler_per_day": "1000.00",
                    "pricing_basis": "User's food planning estimate",
                },
                "activities": {
                    "amount": "3000.00",
                    "optional": True,
                    "pricing_basis": "User's optional activity estimate",
                },
            }
        },
    }
    values.update(overrides)
    return NormalizedTravelRequirements.model_validate(values)


def cost(amount: str | None, currency: str = "INR") -> CostEstimate:
    return CostEstimate(
        amount=Decimal(amount) if amount is not None else None,
        currency=currency,
        classification="estimate" if amount is not None else "unknown",
        pricing_basis="Fictional supplied planning assumption" if amount else None,
    )


def accommodation_plan(
    *,
    accommodation: str | None = "10000",
    local: str | None = "2000",
    intercity: str | None = "5000",
    currency: str = "INR",
) -> dict[str, Any]:
    return AccommodationTransportPlan(
        currency=currency,
        number_of_nights=1,
        accommodation=AccommodationRecommendation(
            recommended_types=["budget_hotel"],
            room_count=2,
            nights=1,
            assumed_occupancy_per_room=2,
            estimated_nightly_rate=cost("5000", currency),
            estimated_total_cost=cost(accommodation, currency),
        ),
        transport_options=[],
        accommodation_cost=cost(accommodation, currency),
        local_transport_cost=cost(local, currency),
        intercity_transport_cost=cost(intercity, currency),
        combined_estimated_cost=cost("999999", currency),
    ).model_dump(mode="json")


def context(
    req: NormalizedTravelRequirements | None = None,
    plan: dict[str, Any] | None = None,
) -> AgentContext:
    return AgentContext(
        normalized_requirements=req or requirements(),
        accommodation_transport_plan=(
            plan if plan is not None else accommodation_plan()
        ),
    )


class FakeGemini:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result or BudgetNarrative(summary="Review discretionary costs.")
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        response_model: type[BudgetNarrative],
        **kwargs: Any,
    ) -> BudgetNarrative:
        self.calls.append(
            {"prompt": prompt, "response_model": response_model, **kwargs}
        )
        if self.error:
            raise self.error
        return self.result


def build_agent(gemini: FakeGemini | None = None) -> BudgetAgent:
    return BudgetAgent(gemini_service=gemini, settings=Settings(_env_file=None))


def test_valid_decimal_budget_calculation_and_category_subtotals() -> None:
    async def run() -> None:
        req = requirements(
            constraints={
                "budget_assumptions": {
                    "food": {
                        "per_traveler_per_day": "1000.00",
                        "pricing_basis": "User's food planning estimate",
                    },
                    "activities": {
                        "amount": "3000.00",
                        "optional": True,
                        "pricing_basis": "User's activity estimate",
                    },
                    "contingency": {"percentage": "10"},
                    "other_expenses": [
                        {
                            "description": "Museum tickets",
                            "amount": "500.50",
                            "currency": "INR",
                            "pricing_basis": "User-provided total",
                        }
                    ],
                }
            }
        )
        result = await build_agent().run(context(req))
        output = result.output
        assert result.status == "completed"
        assert output.status == "WITHIN_BUDGET"
        # Base = 20,000 transport/accommodation + 6,000 food +
        # 3,000 activities + 500.50 other; contingency is 10% of base.
        assert output.known_cost_subtotal == Decimal("29150.55")
        assert output.estimated_total == Decimal("29150.55")
        assert output.remaining_budget == Decimal("849.45")
        assert output.over_budget_amount == Decimal("0.00")
        assert (
            next(t for t in output.category_totals if t.category == "food").known_subtotal
            == Decimal("6000.00")
        )
        assert (
            next(
                t for t in output.category_totals if t.category == "contingency"
            ).known_subtotal
            == Decimal("2650.05")
        )

    asyncio.run(run())


def test_exact_budget_is_within_budget() -> None:
    async def run() -> None:
        result = await build_agent().run(
            context(requirements(total_budget=Decimal("26000")))
        )
        assert result.output.status == "WITHIN_BUDGET"
        assert result.output.estimated_total == Decimal("26000.00")
        assert result.output.remaining_budget == Decimal("0.00")
        assert result.output.over_budget_amount == Decimal("0.00")

    asyncio.run(run())


def test_under_budget_is_within_budget() -> None:
    async def run() -> None:
        result = await build_agent().run(
            context(requirements(total_budget=Decimal("40000")))
        )
        assert result.output.status == "WITHIN_BUDGET"
        assert result.output.remaining_budget == Decimal("14000.00")

    asyncio.run(run())


def test_trip_cost_preferences_calculate_categories_and_contingency_exactly() -> None:
    async def run() -> None:
        req = requirements(
            total_budget=Decimal("5390.00"),
            constraints={},
            cost_preferences=TripCostPreferences(
                accommodation_nightly_rate_per_room=Decimal("1000.00"),
                room_count=2,
                transport_mode="train",
                intercity_round_trip_fare_per_traveler=Decimal("500.00"),
                local_transport_allowance_per_day=Decimal("100.00"),
                food_allowance_per_traveler_per_day=Decimal("50.00"),
                activities_allowance_per_traveler_per_trip=Decimal("300.00"),
                contingency_percentage=Decimal("10"),
            ),
        )
        output = (await build_agent().run(context(req))).output
        assert output.status == "WITHIN_BUDGET"
        assert output.known_cost_subtotal == Decimal("5390.00")
        assert output.estimated_total == Decimal("5390.00")
        assert output.remaining_budget == Decimal("0.00")
        amounts = {item.category: item.amount for item in output.line_items}
        assert amounts == {
            "accommodation": Decimal("2000.00"),
            "local_transport": Decimal("200.00"),
            "intercity_transport": Decimal("1500.00"),
            "food": Decimal("300.00"),
            "activities": Decimal("900.00"),
            "contingency": Decimal("490.00"),
        }
        assert all(item.source == "user_assumption" for item in output.line_items)
        assert "train" in next(
            item.description for item in output.line_items
            if item.category == "intercity_transport"
        )

    asyncio.run(run())


def test_trip_cost_preferences_use_inclusive_trip_days_but_hotel_nights() -> None:
    async def run() -> None:
        req = requirements(
            start_date=date(2027, 1, 10),
            end_date=date(2027, 1, 12),
            travel_duration_days=3,
            constraints={},
            cost_preferences=TripCostPreferences(
                accommodation_nightly_rate_per_room=Decimal("10.01"),
                room_count=1,
                local_transport_allowance_per_day=Decimal("2"),
                food_allowance_per_traveler_per_day=Decimal("3"),
                intercity_round_trip_fare_per_traveler=Decimal("4"),
                activities_allowance_per_traveler_per_trip=Decimal("5"),
            ),
        )
        output = (await build_agent().run(context(req))).output
        amounts = {item.category: item.amount for item in output.line_items}
        assert amounts["accommodation"] == Decimal("20.02")
        assert amounts["local_transport"] == Decimal("6.00")
        assert amounts["food"] == Decimal("27.00")
        assert amounts["intercity_transport"] == Decimal("12.00")
        assert amounts["activities"] == Decimal("15.00")

    asyncio.run(run())


def test_explicit_zero_cost_is_distinct_from_unknown_cost() -> None:
    async def run() -> None:
        preferences = TripCostPreferences(
            accommodation_nightly_rate_per_room=Decimal("0"),
            room_count=1,
            intercity_round_trip_fare_per_traveler=Decimal("0"),
            local_transport_allowance_per_day=Decimal("0"),
            food_allowance_per_traveler_per_day=Decimal("0"),
            activities_allowance_per_traveler_per_trip=Decimal("0"),
            contingency_percentage=Decimal("0"),
        )
        zero_output = (
            await build_agent().run(
                context(
                    requirements(constraints={}, cost_preferences=preferences)
                )
            )
        ).output
        assert zero_output.status == "WITHIN_BUDGET"
        assert zero_output.estimated_total == Decimal("0.00")
        assert all(item.amount == Decimal("0.00") for item in zero_output.line_items)

        unknown_output = (
            await build_agent().run(
                context(
                    requirements(constraints={}),
                    accommodation_plan(
                        accommodation=None, local=None, intercity=None
                    ),
                )
            )
        ).output
        assert unknown_output.status == "INCOMPLETE_ESTIMATE"
        assert unknown_output.estimated_total is None
        assert unknown_output.unpriced_components
        assert all(
            item.amount is None and item.source == "unknown"
            for item in unknown_output.line_items
        )

    asyncio.run(run())


def test_invalid_trip_cost_preferences_are_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        TripCostPreferences(
            accommodation_nightly_rate_per_room=Decimal("-0.01")
        )
    with pytest.raises(ValidationError):
        TripCostPreferences(room_count=0)
    with pytest.raises(ValidationError):
        TripCostPreferences(contingency_percentage=Decimal("100.01"))


def test_over_budget_is_rejected_and_suggestions_do_not_invent_savings() -> None:
    async def run() -> None:
        result = await build_agent().run(
            context(requirements(total_budget=Decimal("25000")))
        )
        output = result.output
        assert output.status == "OVER_BUDGET"
        assert output.estimated_total == Decimal("26000.00")
        assert output.remaining_budget == Decimal("-1000.00")
        assert output.over_budget_amount == Decimal("1000.00")
        assert output.reduction_suggestions
        assert all(
            suggestion.estimated_savings is None
            for suggestion in output.reduction_suggestions
            if suggestion.category != "activities"
        )
        activity_suggestion = next(
            suggestion
            for suggestion in output.reduction_suggestions
            if suggestion.category == "activities"
        )
        assert activity_suggestion.estimated_savings == Decimal("3000.00")

    asyncio.run(run())


@pytest.mark.parametrize("amount", ["0", "-1"])
def test_zero_or_negative_budget_is_rejected(amount: str) -> None:
    with pytest.raises(ValidationError):
        TravelRequest(total_budget=Decimal(amount))

    async def run() -> None:
        req = requirements().model_copy(update={"total_budget": Decimal(amount)})
        result = await build_agent().run(context(req))
        assert result.status == "failed"
        assert result.output.status == "INVALID_INPUT"

    asyncio.run(run())


def test_negative_expense_is_rejected_without_leaking_input() -> None:
    async def run() -> None:
        req = requirements(
            constraints={
                "budget_assumptions": {
                    "food": {"amount": "-25", "description": "sensitive note"},
                }
            }
        )
        result = await build_agent().run(context(req))
        assert result.status == "failed"
        assert result.output.status == "INVALID_INPUT"
        assert "sensitive note" not in " ".join(result.errors)

    asyncio.run(run())


def test_decimal_precision_and_multiple_travelers_and_days() -> None:
    async def run() -> None:
        req = requirements(
            travelers_count=2,
            travel_duration_days=3,
            constraints={
                "budget_assumptions": {
                    "food": {"per_traveler_per_day": "0.10"},
                    "activities": {"amount": "0.20"},
                }
            },
        )
        output = (await build_agent().run(context(req))).output
        food = next(item for item in output.line_items if item.category == "food")
        assert food.amount == Decimal("0.60")
        assert output.known_cost_subtotal == Decimal("17000.80")

    asyncio.run(run())


def test_combined_accommodation_transport_total_is_not_counted_twice() -> None:
    async def run() -> None:
        output = (await build_agent().run(context())).output
        assert output.known_cost_subtotal == Decimal("26000.00")
        assert output.known_cost_subtotal != Decimal("999999.00")
        assert len(
            [item for item in output.line_items if item.category == "accommodation"]
        ) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("accommodation", "accommodation"),
        ("local", "local_transport"),
        ("intercity", "intercity_transport"),
    ],
)
def test_unknown_accommodation_or_transport_cost_makes_estimate_incomplete(
    field: str,
    expected: str,
) -> None:
    async def run() -> None:
        plan_args = {"accommodation": "10000", "local": "2000", "intercity": "5000"}
        plan_args[field] = None
        output = (
            await build_agent().run(
                context(plan=accommodation_plan(**plan_args))
            )
        ).output
        assert output.status == "INCOMPLETE_ESTIMATE"
        assert output.estimated_total is None
        assert any(expected in value for value in output.unpriced_components)

    asyncio.run(run())


def test_missing_food_activity_and_transport_inputs_are_not_zeroed() -> None:
    async def run() -> None:
        req = requirements(constraints={})
        output = (
            await build_agent().run(
                context(req, plan=accommodation_plan(local=None, intercity=None))
            )
        ).output
        assert output.status == "INCOMPLETE_ESTIMATE"
        assert output.estimated_total is None
        assert all(
            any(item.startswith(category) for item in output.unpriced_components)
            for category in {
            "local_transport",
            "intercity_transport",
            "food",
            "activities",
            }
        )
        assert all(
            item.amount is None
            for item in output.line_items
            if item.category in {"food", "activities"}
        )

    asyncio.run(run())


def test_known_costs_over_budget_remain_over_budget_with_unknown_items() -> None:
    async def run() -> None:
        plan = accommodation_plan(
            accommodation="12000",
            local="0",
            intercity="0",
        )
        result = await build_agent().run(
            context(
                requirements(total_budget=Decimal("10000"), constraints={}),
                plan=plan,
            )
        )
        assert result.output.status == "OVER_BUDGET"
        assert result.output.estimated_total is None
        assert result.output.known_cost_subtotal == Decimal("13200.00")
        assert result.output.over_budget_amount == Decimal("3200.00")
        reserve = next(
            item for item in result.output.line_items
            if item.category == "contingency"
        )
        assert reserve.amount == Decimal("1200.00")
        assert reserve.source == "configured_assumption"
        assert result.output.remaining_budget is None

    asyncio.run(run())


def test_currency_mismatch_is_not_converted_or_added() -> None:
    async def run() -> None:
        req = requirements(
            constraints={
                "budget_assumptions": {
                    "food": {"amount": "100", "currency": "USD"},
                    "activities": {"amount": "50", "currency": "INR"},
                }
            }
        )
        output = (await build_agent().run(context(req))).output
        assert output.status == "INCOMPLETE_ESTIMATE"
        assert output.currency == "INR"
        assert output.estimated_total is None
        assert output.known_cost_subtotal == Decimal("17050.00")
        assert any("USD cannot be compared with INR" in x for x in output.unpriced_components)
        food = next(item for item in output.line_items if item.category == "food")
        assert food.amount == Decimal("100")
        assert food.currency == "USD"

    asyncio.run(run())


def test_missing_currency_cannot_claim_affordability_or_convert() -> None:
    async def run() -> None:
        req = requirements(currency=None)
        output = (await build_agent().run(context(req))).output
        assert output.status == "INCOMPLETE_ESTIMATE"
        assert output.estimated_total is None
        assert output.known_cost_subtotal is None
        assert any("no exchange rate" in item.lower() for item in output.warnings) is False
        assert any("currency is missing" in item for item in output.warnings)

    asyncio.run(run())


def test_contingency_percentage_uses_only_known_priced_costs() -> None:
    async def run() -> None:
        req = requirements(
            constraints={
                "budget_assumptions": {
                    "food": {"amount": "0"},
                    "activities": {"amount": "0"},
                    "contingency": {"percentage": "12.5"},
                }
            }
        )
        output = (await build_agent().run(context(req))).output
        contingency = next(
            item for item in output.line_items if item.category == "contingency"
        )
        assert contingency.amount == Decimal("2125.00")
        assert output.status == "WITHIN_BUDGET"

    asyncio.run(run())


def test_contingency_uses_supported_base_and_excludes_unknown_costs() -> None:
    async def run() -> None:
        req = requirements(
            constraints={
                "budget_assumptions": {
                    "food": {"amount": "100"},
                    "activities": {"amount": "0"},
                    "contingency": {"percentage": "10"},
                }
            }
        )
        output = (
            await build_agent().run(context(req, plan=accommodation_plan(local=None)))
        ).output
        contingency = next(
            item for item in output.line_items if item.category == "contingency"
        )
        assert contingency.amount == Decimal("1510.00")
        assert contingency.source == "user_assumption"
        assert any("unknown expenses are not treated as zero" in item for item in contingency.assumptions)
        assert any("local transport" in item for item in contingency.assumptions)
        assert output.status == "INCOMPLETE_ESTIMATE"
        assert output.estimated_total is None

    asyncio.run(run())


def test_gemini_unavailable_and_invalid_output_cannot_change_budget_result() -> None:
    async def run() -> None:
        failing = FakeGemini(error=GeminiModelUnavailableError("private response"))
        result = await build_agent(failing).run(
            context(requirements(total_budget=Decimal("25000")))
        )
        assert result.output.status == "OVER_BUDGET"
        assert result.output.estimated_total == Decimal("26000.00")
        assert "private response" not in " ".join(result.warnings)
        assert any("Gemini budget commentary" in warning for warning in result.warnings)

        malicious = FakeGemini(
            result={
                "summary": "This is affordable.",
                "status": "WITHIN_BUDGET",
                "budget": "999999",
            }
        )
        invalid_result = await build_agent(malicious).run(
            context(requirements(total_budget=Decimal("25000")))
        )
        assert invalid_result.output.status == "OVER_BUDGET"
        assert invalid_result.output.qualitative_summary is None
        assert any("commentary was unavailable" in w for w in invalid_result.warnings)

        misleading = FakeGemini(
            result=BudgetNarrative(
                summary="This trip is affordable and within budget."
            )
        )
        filtered = await build_agent(misleading).run(
            context(requirements(total_budget=Decimal("25000")))
        )
        assert filtered.output.status == "OVER_BUDGET"
        assert filtered.output.qualitative_summary is None
        assert any("affordability claims" in w for w in filtered.warnings)

    asyncio.run(run())


def test_deterministic_calculation_needs_no_gemini_credentials() -> None:
    async def run() -> None:
        result = await build_agent().run(context())
        assert result.status == "completed"
        assert result.output.status == "WITHIN_BUDGET"
        assert result.output.qualitative_summary is None

    asyncio.run(run())


def test_explicit_optional_activity_reduction_has_grounded_savings() -> None:
    async def run() -> None:
        req = requirements(
            total_budget=Decimal("1000"),
            constraints={
                "budget_assumptions": {
                    "food": {"amount": "100"},
                    "activities": {
                        "amount": "400",
                        "optional": True,
                        "pricing_basis": "Supplied activity budget",
                        "source_references": [
                            {
                                "title": "Example activity costs",
                                "url": "https://example.com/activities",
                                "topic": "travel_tips",
                            }
                        ],
                    },
                }
            },
        )
        output = (await build_agent().run(context(req))).output
        suggestion = next(
            item
            for item in output.reduction_suggestions
            if item.category == "activities"
            and item.estimated_savings is not None
        )
        assert output.status == "OVER_BUDGET"
        assert suggestion.estimated_savings == Decimal("400.00")
        assert suggestion.pricing_basis
        assert str(suggestion.source_references[0].url) == "https://example.com/activities"

    asyncio.run(run())


def test_other_expenses_are_counted_once_and_currency_checked() -> None:
    async def run() -> None:
        req = requirements(
            constraints={
                "budget_assumptions": {
                    "food": {"amount": "0"},
                    "activities": {"amount": "0"},
                    "other_expenses": [
                        {
                            "description": "Parking",
                            "amount": "250",
                            "currency": "INR",
                        }
                    ],
                }
            }
        )
        output = (await build_agent().run(context(req))).output
        assert output.known_cost_subtotal == Decimal("17250.00")
        assert len([x for x in output.line_items if x.category == "other"]) == 1
        assert next(
            item for item in output.category_totals if item.category == "other"
        ).known_subtotal == Decimal("250.00")

    asyncio.run(run())


def test_verified_line_items_require_and_preserve_source_references() -> None:
    async def run() -> None:
        req = requirements(
            constraints={
                "budget_assumptions": {
                    "food": {
                        "amount": "50",
                        "classification": "verified",
                        "source_references": [
                            {
                                "title": "Published food menu",
                                "url": "https://example.com/menu",
                                "topic": "food",
                            }
                        ],
                    },
                    "activities": {"amount": "0"},
                }
            }
        )
        output = (await build_agent().run(context(req))).output
        food = next(item for item in output.line_items if item.category == "food")
        assert food.classification == "verified"
        assert str(food.source_references[0].url) == "https://example.com/menu"

    asyncio.run(run())
