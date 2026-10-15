"""Internal planning-assumption contracts, separate from provider price evidence."""
from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .agent_schemas import AgentSchema, NormalizedTravelRequirements, PlanningTransportMode
from .budget import BudgetLineItem

AICostCategory = Literal["intercity_transport", "accommodation", "food", "local_transport", "activities"]
AICostUnit = Literal["traveler_round_trip", "room_night", "traveler_day", "party_day", "traveler_trip"]
CATEGORY_UNITS: dict[AICostCategory, AICostUnit] = {
    "intercity_transport": "traveler_round_trip",
    "accommodation": "room_night",
    "food": "traveler_day",
    "local_transport": "party_day",
    "activities": "traveler_trip",
}


class AICostScope(AgentSchema):
    origin: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    start_date: date
    end_date: date
    travelers_count: int = Field(ge=1, le=100, strict=True)
    room_count: int = Field(ge=1, le=50, strict=True)
    travel_style: str = Field(min_length=1, max_length=100)
    transport_mode: PlanningTransportMode
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @field_validator("origin", "destination", "travel_style", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def valid_dates(self):
        if not 0 <= (self.end_date - self.start_date).days < 365:
            raise ValueError("Planning scope must contain 1 to 365 inclusive days.")
        return self

    def quantity_for(self, category: AICostCategory) -> int:
        days = (self.end_date - self.start_date).days + 1
        return {"intercity_transport": self.travelers_count,
                "accommodation": self.room_count * (days - 1),
                "food": self.travelers_count * days,
                "local_transport": days,
                "activities": self.travelers_count}[category]


class AICostEstimationRequest(AICostScope):
    missing_categories: list[AICostCategory] = Field(max_length=5)

    @model_validator(mode="after")
    def eligible_categories(self):
        if len(set(self.missing_categories)) != len(self.missing_categories):
            raise ValueError("Missing categories must be unique.")
        if self.start_date == self.end_date and "accommodation" in self.missing_categories:
            raise ValueError("Zero-night accommodation needs no AI price assumption.")
        return self

    @classmethod
    def from_requirements(cls, requirements: NormalizedTravelRequirements, *, room_count: int,
                          missing_categories: list[AICostCategory]):
        """Whitelist cost scope; never serialize budgets or arbitrary constraints."""
        preferences = requirements.cost_preferences
        return cls(
            origin=requirements.origin, destination=requirements.destination,
            start_date=requirements.start_date, end_date=requirements.end_date,
            travelers_count=requirements.travelers_count, room_count=room_count,
            travel_style=requirements.travel_style or "balanced",
            transport_mode=(preferences.transport_mode if preferences else None)
            or requirements.constraints.get("intercity_routing_mode") or "flight",
            currency=requirements.currency, missing_categories=missing_categories,
        )


class AIUnitRate(AgentSchema):
    category: AICostCategory
    unit: AICostUnit
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    typical: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=14, decimal_places=4)
    minimum: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False, max_digits=14, decimal_places=4)
    maximum: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False, max_digits=14, decimal_places=4)

    @field_validator("typical", "minimum", "maximum", mode="before")
    @classmethod
    def reject_boolean_rate(cls, value):
        if isinstance(value, bool):
            raise ValueError("A boolean is not a unit rate.")
        return value

    @model_validator(mode="after")
    def valid_unit_and_range(self):
        if self.unit != CATEGORY_UNITS[self.category]:
            raise ValueError("Unit does not match the cost category.")
        if (self.minimum is None) != (self.maximum is None):
            raise ValueError("Provide both scenario bounds or neither.")
        if self.minimum is not None and not self.minimum <= self.typical <= self.maximum:
            raise ValueError("Typical rate must lie within its scenario range.")
        return self


class AICostEstimationResponse(AgentSchema):
    # No free-text explanations: provider claims, citations and timestamps have
    # no accepted output field. Python supplies the planning caveats separately.
    scope: AICostScope
    estimates: list[AIUnitRate] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def unique_categories(self):
        if len({item.category for item in self.estimates}) != len(self.estimates):
            raise ValueError("Duplicate estimate categories are not allowed.")
        return self


class AIUnitRateBounds(AgentSchema):
    minimum: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=14, decimal_places=4)
    maximum: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=14, decimal_places=4)

    @model_validator(mode="after")
    def ordered(self):
        if self.maximum < self.minimum:
            raise ValueError("Unit-rate sanity bounds must be ordered.")
        return self


class AICostProvenance(AgentSchema):
    method: Literal["gemini_planning_assumption"] = "gemini_planning_assumption"
    confidence: Literal["low"] = "low"
    model: str = Field(min_length=1, max_length=200)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    scope: AICostScope
    # On quantity recalculation, scope is the applied scope while this retains
    # the original generation scope. It is never an observed-provider timestamp.
    generation_scope: AICostScope | None = None
    unit_rate: AIUnitRate
    quantity: int = Field(gt=0, strict=True)
    live_availability_checked: Literal[False] = False

    @field_validator("generated_at")
    @classmethod
    def real_generation_timestamp(cls, value):
        if value.utcoffset() is None or value > datetime.now(UTC):
            raise ValueError("AI generation time must be timezone-aware and cannot be in the future.")
        return value

    def validate_line_item(self, line: BudgetLineItem) -> None:
        _validate_planning_evidence(line)
        rate = self.unit_rate
        if self.generation_scope is not None:
            from ..services.ai_cost_safety import rate_scope_changed
            if rate_scope_changed(self.generation_scope, self.scope, rate.category):
                raise ValueError("AI rates cannot be reused across a rate-relevant scope change.")
        if (rate.category != line.category or rate.currency != line.currency
            or rate.currency != self.scope.currency
            or self.quantity != self.scope.quantity_for(rate.category)):
            raise ValueError("AI provenance must match the line's scope and units.")
        for unit_field, line_field in (("typical", "amount"), ("minimum", "minimum_amount"), ("maximum", "maximum_amount")):
            value = getattr(rate, unit_field)
            expected = (value * self.quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if value is not None else None
            if getattr(line, line_field) != expected:
                raise ValueError("AI line amounts must reconcile to unit rates and Python quantities.")


def _validate_planning_evidence(line: BudgetLineItem) -> None:
    if (line.amount is None or line.amount <= 0 or line.classification != "estimated"
        or line.source != "configured_assumption" or line.evidence_kind != "planning_estimate"
        or line.confidence != "low" or line.source_references or line.price_sources
        or line.flight_reference is not None or line.currency_conversion is not None or line.optional):
        raise ValueError("AI assumptions cannot contain verified/provider price evidence.")


class AIPlanningCostEstimate(AgentSchema):
    line_item: BudgetLineItem
    provenance: AICostProvenance | None = None

    @model_validator(mode="after")
    def planning_evidence_only(self):
        line = self.line_item
        _validate_planning_evidence(line)
        if self.provenance is not None:
            self.provenance.validate_line_item(line)
        return self


class AICostEstimationResult(AgentSchema):
    estimates: dict[AICostCategory, AIPlanningCostEstimate] = Field(default_factory=dict)
    unresolved_categories: list[AICostCategory] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
