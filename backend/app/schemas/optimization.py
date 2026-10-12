from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from typing import Literal

from pydantic import Field

from .agent_schemas import AgentSchema
from .budget import BudgetStatus
from .destination_research import SourceReference

IssueSeverity = Literal["critical", "error", "warning", "info"]
ReviewStatus = Literal["APPROVED", "REVISION_REQUIRED", "INSUFFICIENT_DATA"]
SuggestionCategory = Literal[
    "schedule",
    "routing",
    "rest",
    "activities",
    "accommodation",
    "transport",
    "budget",
    "accessibility",
    "weather",
    "general",
]


class OptimizationIssue(AgentSchema):
    code: str
    severity: IssueSeverity
    description: str
    evidence_basis: str
    recommended_action: str
    blocks_approval: bool
    date: Date | None = None
    day_number: int | None = Field(default=None, ge=1)
    activity_title: str | None = None
    place_id: str | None = None


class OptimizationSuggestion(AgentSchema):
    category: SuggestionCategory
    recommendation: str
    date: Date | None = None
    day_number: int | None = Field(default=None, ge=1)
    estimated_savings: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    currency: str | None = None
    savings_basis: str | None = None
    source_references: list[SourceReference] = Field(default_factory=list)


class OptimizationReview(AgentSchema):
    approved: bool
    review_status: ReviewStatus
    issues: list[OptimizationIssue] = Field(default_factory=list)
    suggestions: list[OptimizationSuggestion] = Field(default_factory=list)
    budget_status: BudgetStatus = "INCOMPLETE_ESTIMATE"
    estimated_savings: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    currency: str | None = None
    warnings: list[str] = Field(default_factory=list)
    source_references: list[SourceReference] = Field(default_factory=list)
    qualitative_summary: str | None = None


class OptimizationNarrative(AgentSchema):
    summary: str = Field(min_length=1, max_length=500)
    suggestions: list[str] = Field(default_factory=list, max_length=6)
