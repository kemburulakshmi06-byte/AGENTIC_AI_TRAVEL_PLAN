from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, HttpUrl, field_validator, model_validator

from .agent_schemas import AgentSchema
from .destination_research import SourceReference

BudgetCategory = Literal[
    "accommodation",
    "local_transport",
    "intercity_transport",
    "food",
    "activities",
    "contingency",
    "other",
]
BudgetClassification = Literal["estimated", "verified", "unknown"]
BudgetSource = Literal[
    "verified_provider",
    "user_assumption",
    "configured_assumption",
    "reference_data",
    "unknown",
]

EstimateConfidence = Literal["high", "medium", "low", "unassessed", "unknown"]


class PriceSource(AgentSchema):
    name: str
    url: HttpUrl | None = None
    observed_on: date
    currency: str
    # Observation date is not the publication date of an undated web guide.
    published_on: date | None = None


class CurrencyConversion(AgentSchema):
    original_currency: str
    original_amount: Decimal = Field(ge=0)
    target_currency: str
    rate: Decimal = Field(gt=0)
    rate_date: date
    source: str
    source_url: HttpUrl
BudgetStatus = Literal[
    "INCOMPLETE_ESTIMATE",
    "WITHIN_BUDGET",
    "OVER_BUDGET",
    "INVALID_INPUT",
]


class BudgetLineItem(AgentSchema):
    category: BudgetCategory
    description: str
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str
    classification: BudgetClassification
    source: BudgetSource = "unknown"
    pricing_basis: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(default_factory=list)
    optional: bool = False
    evidence_kind: Literal["planning_estimate", "verified_price", "unknown"] = "unknown"
    minimum_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    maximum_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    confidence: EstimateConfidence = "unassessed"
    estimated_on: date = Field(default_factory=date.today)
    price_sources: list[PriceSource] = Field(default_factory=list)
    currency_conversion: CurrencyConversion | None = None

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized != "UNKNOWN" and (
            len(normalized) != 3 or not normalized.isalpha()
        ):
            raise ValueError("currency must be a three-letter code or UNKNOWN.")
        return normalized

    @model_validator(mode="after")
    def validate_amount_classification(self) -> BudgetLineItem:
        if self.amount is None and self.classification != "unknown":
            raise ValueError("An unpriced line item must have unknown classification.")
        if self.amount is not None and self.classification == "unknown":
            raise ValueError("A priced line item cannot have unknown classification.")
        self.evidence_kind = (
            "unknown" if self.amount is None else "verified_price"
            if self.classification == "verified" and self.source == "verified_provider"
            and self.source_references else "planning_estimate"
        )
        if self.amount is None and (self.minimum_amount is not None or self.maximum_amount is not None):
            raise ValueError("Unknown prices cannot have a cost range.")
        if self.amount is not None and (
            (self.minimum_amount is not None and self.minimum_amount > self.amount)
            or (self.maximum_amount is not None and self.maximum_amount < self.amount)
        ):
            raise ValueError("The typical amount must lie within its cost range.")
        return self


class BudgetCategoryTotal(AgentSchema):
    category: BudgetCategory
    known_subtotal: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    currency: str
    complete: bool
    included: bool = True
    line_item_count: int = Field(ge=0)


class BudgetValidationResult(AgentSchema):
    total_budget: Decimal | None = Field(
        default=None, gt=0, max_digits=14, decimal_places=2
    )
    currency: str | None = None
    known_cost_subtotal: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    estimated_total: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    remaining_budget: Decimal | None = Field(
        default=None, max_digits=14, decimal_places=2
    )
    over_budget_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    status: BudgetStatus


class BudgetReductionSuggestion(AgentSchema):
    category: BudgetCategory
    recommendation: str
    estimated_savings: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    currency: str | None = None
    pricing_basis: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(default_factory=list)


class BudgetBreakdown(BudgetValidationResult):
    minimum_estimated_total: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    maximum_estimated_total: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    estimate_confidence: EstimateConfidence = "unassessed"
    booking_readiness: Literal["unverified"] = "unverified"
    missing_price_actions: list[str] = Field(default_factory=list)
    pricing_status: Literal["COMPLETE_ESTIMATE", "LIVE_PRICE_VERIFIED", "INCOMPLETE"] = "INCOMPLETE"
    missing_price_inputs: list[str] = Field(default_factory=list)
    line_items: list[BudgetLineItem] = Field(default_factory=list)
    category_totals: list[BudgetCategoryTotal] = Field(default_factory=list)
    unpriced_components: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    reduction_suggestions: list[BudgetReductionSuggestion] = Field(
        default_factory=list
    )
    qualitative_summary: str | None = None
    qualitative_tradeoffs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def classify_price_evidence(self) -> BudgetBreakdown:
        if self.estimated_total is None and (self.minimum_estimated_total is not None or self.maximum_estimated_total is not None):
            raise ValueError("An incomplete budget cannot have complete-trip range totals.")
        if self.estimated_total is not None and (
            (self.minimum_estimated_total is not None and self.minimum_estimated_total > self.estimated_total)
            or (self.maximum_estimated_total is not None and self.maximum_estimated_total < self.estimated_total)
        ):
            raise ValueError("Typical total must lie within the scenario range.")
        if (
            self.estimated_total is None or self.unpriced_components or not self.line_items
            or any(item.amount is None or item.classification == "unknown" or item.currency != self.currency for item in self.line_items)
            or any(category.included and not category.complete for category in self.category_totals)
        ):
            self.pricing_status = "INCOMPLETE"
        elif all(
            item.amount is not None and item.classification == "verified"
            and item.source == "verified_provider" and item.source_references
            for item in self.line_items
        ):
            self.pricing_status = "LIVE_PRICE_VERIFIED"
        else:
            self.pricing_status = "COMPLETE_ESTIMATE"
        return self


class BudgetCostInput(AgentSchema):
    description: str
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    per_traveler_per_day: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    currency: str | None = None
    pricing_basis: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(default_factory=list)
    optional: bool = False
    classification: Literal["estimated", "verified"] = "estimated"

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be a three-letter code.")
        return normalized

    @model_validator(mode="after")
    def validate_pricing_shape(self) -> BudgetCostInput:
        if self.amount is not None and self.per_traveler_per_day is not None:
            raise ValueError(
                "Specify either a total amount or a per-traveler-per-day amount."
            )
        if self.classification == "verified" and (
            (self.amount is None and self.per_traveler_per_day is None)
            or not self.source_references
        ):
            raise ValueError(
                "Verified costs require a priced amount and source reference."
            )
        return self


class ContingencyInput(AgentSchema):
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    percentage: Decimal | None = Field(default=None, ge=0, le=100)
    currency: str | None = None
    pricing_basis: str | None = None
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be a three-letter code.")
        return normalized

    @model_validator(mode="after")
    def validate_contingency_shape(self) -> ContingencyInput:
        if (self.amount is None) == (self.percentage is None):
            raise ValueError("Specify exactly one contingency amount or percentage.")
        return self


class BudgetNarrative(AgentSchema):
    summary: str = Field(min_length=1, max_length=600)
    tradeoffs: list[str] = Field(default_factory=list, max_length=8)
