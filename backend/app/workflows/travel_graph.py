from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol, cast

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from ..schemas.accommodation_transport import AccommodationTransportPlan
from ..schemas.agent_schemas import (
    AgentContext,
    AgentResult,
    NormalizedTravelRequirements,
    TravelRequest,
)
from ..schemas.budget import BudgetBreakdown
from ..schemas.destination_research import DestinationResearch, SourceReference
from ..schemas.itinerary import TripItinerary
from ..schemas.optimization import OptimizationReview
from ..core.diagnostics import diagnostic_context, record_event, record_requirements_rule
from .state import (
    AgentExecutionRecord,
    AgentExecutionStatus,
    RevisionDirective,
    TravelWorkflowState,
    WorkflowStatus,
)

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|authorization)\b\s*[:=]\s*[^,\s]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+\S+")
_REVISION_ISSUE_CODES = {
    "unsupported_attraction_id",
    "unverified_attraction_coordinates",
    "attraction_coordinate_mismatch",
    "missing_source_reference",
    "unsupported_source_reference",
}


class AgentRunner(Protocol):
    agent_name: str

    async def run(self, context: AgentContext) -> AgentResult[Any]: ...


@dataclass(frozen=True)
class TravelAgentSet:
    requirements: AgentRunner
    destination_research: AgentRunner
    accommodation_transport: AgentRunner
    budget: AgentRunner
    itinerary: AgentRunner
    optimization_review: AgentRunner

    def all(self) -> tuple[AgentRunner, ...]:
        return (
            self.requirements,
            self.destination_research,
            self.accommodation_transport,
            self.budget,
            self.itinerary,
            self.optimization_review,
        )


def create_default_agents() -> TravelAgentSet:
    """Construct agents lazily for one workflow invocation."""
    from ..agents.accommodation_transport_agent import (
        AccommodationTransportAgent,
    )
    from ..agents.budget_agent import BudgetAgent
    from ..agents.destination_research_agent import DestinationResearchAgent
    from ..agents.itinerary_agent import ItineraryAgent
    from ..agents.optimization_agent import OptimizationAgent
    from ..agents.requirements_agent import RequirementsAgent

    return TravelAgentSet(
        requirements=RequirementsAgent(),
        destination_research=DestinationResearchAgent(),
        accommodation_transport=AccommodationTransportAgent(),
        budget=BudgetAgent(),
        itinerary=ItineraryAgent(),
        optimization_review=OptimizationAgent(),
    )


def build_travel_graph(
    agents: TravelAgentSet,
    *,
    max_revisions: int = 2,
) -> Any:
    """Build the compiled async LangGraph state machine."""
    if max_revisions < 0:
        raise ValueError("max_revisions must be zero or greater.")

    graph = StateGraph(TravelWorkflowState)
    graph.add_node(
        "requirements",
        _agent_node(
            agents.requirements,
            "requirements",
            "normalized_requirements",
            NormalizedTravelRequirements,
        ),
    )
    graph.add_node(
        "destination_research",
        _agent_node(
            agents.destination_research,
            "destination_research",
            "destination_research",
            DestinationResearch,
        ),
    )
    graph.add_node(
        "accommodation_transport",
        _agent_node(
            agents.accommodation_transport,
            "accommodation_transport",
            "accommodation_transport_plan",
            AccommodationTransportPlan,
        ),
    )
    graph.add_node(
        "budget",
        _agent_node(
            agents.budget,
            "budget",
            "budget_breakdown",
            BudgetBreakdown,
        ),
    )
    graph.add_node(
        "itinerary",
        _agent_node(
            agents.itinerary,
            "itinerary",
            "trip_itinerary",
            TripItinerary,
        ),
    )
    graph.add_node(
        "optimization_review",
        _agent_node(
            agents.optimization_review,
            "optimization_review",
            "optimization_review",
            OptimizationReview,
        ),
    )
    graph.add_node("revision", _revision_node)

    graph.add_edge(START, "requirements")
    graph.add_conditional_edges(
        "requirements",
        _continue_or_stop("destination_research"),
        {"destination_research": "destination_research", END: END},
    )
    graph.add_conditional_edges(
        "destination_research",
        _continue_or_stop("accommodation_transport"),
        {"accommodation_transport": "accommodation_transport", END: END},
    )
    graph.add_conditional_edges(
        "accommodation_transport",
        _continue_or_stop("budget"),
        {"budget": "budget", END: END},
    )
    graph.add_conditional_edges(
        "budget",
        _continue_or_stop("itinerary"),
        {"itinerary": "itinerary", END: END},
    )
    graph.add_conditional_edges(
        "itinerary",
        _continue_or_stop("optimization_review"),
        {"optimization_review": "optimization_review", END: END},
    )
    graph.add_conditional_edges(
        "optimization_review",
        _after_review,
        {"revision": "revision", END: END},
    )
    graph.add_conditional_edges(
        "revision",
        _after_revision,
        {"budget": "budget", "itinerary": "itinerary", END: END},
    )
    return graph.compile()


def _agent_node(
    agent: AgentRunner,
    stage: str,
    output_field: str,
    output_model: type[BaseModel],
):
    async def run(state: TravelWorkflowState) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        started_clock = time.perf_counter()
        base = {
            "current_stage": stage,
            "execution_status": "RUNNING",
        }
        try:
            context = _build_context(state)
            timeout_seconds = state.get("agent_timeout_seconds", 60.0)
            with diagnostic_context(agent=stage):
                async with asyncio.timeout(timeout_seconds):
                    raw_result = await agent.run(context)
            result = AgentResult.model_validate(raw_result)
            if result.agent_name != stage:
                raise ValueError("Agent result name did not match the workflow stage.")
            output = output_model.model_validate(result.output)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return _failure_update(
                stage,
                started_at,
                started_clock,
                "timed_out",
                "Agent execution timed out; provider details were suppressed.",
            )
        except (ValidationError, ValueError, TypeError):
            if stage == "requirements":
                record_requirements_rule("unexpected_agent_response", category="invalid_schema")
            return _failure_update(
                stage,
                started_at,
                started_clock,
                "failed",
                "Agent returned an invalid result; sensitive details were suppressed.",
            )
        except Exception:
            return _failure_update(
                stage,
                started_at,
                started_clock,
                "failed",
                "Agent execution failed; sensitive provider details were suppressed.",
            )

        execution_status: WorkflowStatus = "RUNNING"
        error_messages: list[str] = []
        if result.status == "failed":
            execution_status = "FAILED"
            error_messages.append(
                f"{stage} agent reported failure; sensitive details were suppressed."
            )
        elif stage == "requirements":
            missing = getattr(output, "missing_required_fields", [])
            conflicts = getattr(output, "conflicting_requirements", [])
            if missing or conflicts or result.status == "needs_input":
                execution_status = "INVALID_REQUEST"
                if missing:
                    error_messages.append(
                        "Required request fields are missing: "
                        + ", ".join(sorted(missing))
                        + "."
                    )
                if conflicts:
                    error_messages.append(
                        "The request contains conflicting travel requirements."
                    )
            else:
                normalized = cast(NormalizedTravelRequirements, output)
                supplied = cast(TravelRequest, state["request"])
                if any(
                    getattr(supplied, field) is not None
                    and not _same_explicit_value(field, getattr(supplied, field), getattr(normalized, field))
                    for field in (
                        "origin",
                        "destination",
                        "start_date",
                        "end_date",
                        "travelers_count",
                        "total_budget",
                        "currency",
                    )
                ):
                    for field in ("origin", "destination", "start_date", "end_date", "travelers_count", "total_budget", "currency"):
                        if getattr(supplied, field) is not None and not _same_explicit_value(field, getattr(supplied, field), getattr(normalized, field)):
                            record_requirements_rule("explicit_value_changed", field=field)
                    execution_status = "INVALID_REQUEST"
                    error_messages.append(
                        "Normalized requirements changed an explicitly supplied request value."
                    )
        elif stage == "destination_research" and result.status == "needs_input":
            execution_status = "INSUFFICIENT_DATA"
            error_messages.append(
                "Destination research: " + (getattr(output, "resolution_status", None) or "clarification_required")
                + ". " + next(iter(getattr(output, "clarification_questions", [])),
                               next(iter(result.warnings), "Choose a specific destination city and country or retry provider research."))
            )
        elif stage == "optimization_review":
            review = cast(OptimizationReview, output)
            safe_status, safety_warning = _budget_safety_status(state)
            if safety_warning:
                review = review.model_copy(
                    update={
                        "approved": False,
                        "review_status": safe_status,
                        "warnings": [*review.warnings, safety_warning],
                    }
                )
                output = cast(BaseModel, review)
            elif review.approved != (review.review_status == "APPROVED"):
                review = review.model_copy(
                    update={
                        "approved": False,
                        "review_status": "REVISION_REQUIRED",
                        "warnings": [
                            *review.warnings,
                            "Review approval and review status were inconsistent; approval was blocked.",
                        ],
                    }
                )
                output = cast(BaseModel, review)
                safe_status = "REVISION_REQUIRED"
                safety_warning = (
                    "Review approval and review status were inconsistent; approval was blocked."
                )
            if safety_warning:
                execution_status = safe_status
                error_messages.append(safety_warning)
            elif review.approved:
                execution_status = "COMPLETED"
            elif review.review_status == "INSUFFICIENT_DATA":
                execution_status = "INSUFFICIENT_DATA"
            elif state.get("revision_count", 0) >= state.get("max_revisions", 2):
                execution_status = "REVISION_REQUIRED"
                error_messages.append(
                    "The review requires changes and the configured revision limit has been reached."
                )

        record_status: AgentExecutionStatus = result.status
        with diagnostic_context(agent=stage):
            record_event("workflow", "agent.run", record_status, started_clock)
        update: dict[str, Any] = {
            **base,
            output_field: output,
            "execution_status": execution_status,
            "agent_history": [
                _execution_record(
                    stage,
                    record_status,
                    started_at,
                    started_clock,
                    output_data=output.model_dump(mode="json"),
                )
            ],
            "source_references": _new_sources(state, output),
            "warnings": [_sanitize_text(item) for item in result.warnings],
            "errors": error_messages,
        }
        return update

    return run


def _same_explicit_value(field, supplied, normalized):
    if field in {"origin", "destination"} and isinstance(supplied, str) and isinstance(normalized, str):
        return " ".join(supplied.split()) == " ".join(normalized.split())
    return supplied == normalized


def _failure_update(
    stage: str,
    started_at: datetime,
    started_clock: float,
    status: AgentExecutionStatus,
    message: str,
) -> dict[str, Any]:
    with diagnostic_context(agent=stage):
        record_event("workflow", "agent.run", "failed", started_clock, category=status)
    return {
        "current_stage": stage,
        "execution_status": "FAILED",
        "agent_history": [
            _execution_record(stage, status, started_at, started_clock, message)
        ],
        "errors": [message],
    }


def _execution_record(
    name: str,
    status: AgentExecutionStatus,
    started_at: datetime,
    started_clock: float,
    error: str | None = None,
    output_data: dict | None = None,
) -> AgentExecutionRecord:
    finished_at = datetime.now(timezone.utc)
    return AgentExecutionRecord(
        agent_name=name,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=max(0, round((time.perf_counter() - started_clock) * 1000)),
        error=error,
        output_data=output_data,
    )


def _build_context(state: TravelWorkflowState) -> AgentContext:
    def json_dict(field: str) -> dict[str, Any] | None:
        value = state.get(field)
        return value.model_dump(mode="json") if isinstance(value, BaseModel) else None

    return AgentContext(
        request=state["request"],
        normalized_requirements=state.get("normalized_requirements"),
        destination_research=json_dict("destination_research"),
        accommodation_transport_plan=json_dict("accommodation_transport_plan"),
        budget_breakdown=json_dict("budget_breakdown"),
        trip_itinerary=json_dict("trip_itinerary"),
    )


def _continue_or_stop(next_stage: str):
    def route(state: TravelWorkflowState) -> str:
        return (
            next_stage
            if state.get("execution_status") == "RUNNING"
            else END
        )

    return route


def _after_review(state: TravelWorkflowState) -> str:
    if state.get("execution_status") != "RUNNING":
        return END
    return "revision"


def _budget_safety_status(
    state: TravelWorkflowState,
) -> tuple[WorkflowStatus, str | None]:
    budget = state.get("budget_breakdown")
    requirements = state.get("normalized_requirements")
    if budget is None or requirements is None:
        return (
            "INSUFFICIENT_DATA",
            "Budget or normalized requirements are missing; affordability cannot be approved.",
        )
    if (
        budget.status in {"INCOMPLETE_ESTIMATE", "INVALID_INPUT"}
        or budget.estimated_total is None
        or budget.total_budget is None
        or budget.known_cost_subtotal is None
        or budget.unpriced_components
        or sum(
            (
                category.known_subtotal
                for category in budget.category_totals
                if category.included
            ),
            Decimal("0"),
        )
        != budget.known_cost_subtotal
        or any(
            item.amount is None
            or item.classification == "unknown"
            or item.currency != budget.currency
            for item in budget.line_items
        )
        or any(
            category.included
            and (
                not category.complete
                or category.currency != budget.currency
                or category.line_item_count
                != sum(
                    item.category == category.category
                    for item in budget.line_items
                )
                or category.known_subtotal
                != sum(
                    (
                        item.amount
                        for item in budget.line_items
                        if item.category == category.category
                        and item.amount is not None
                    ),
                    Decimal("0"),
                )
            )
            for category in budget.category_totals
        )
        or any(
            not any(
                category.category == item.category and category.included
                for category in budget.category_totals
            )
            for item in budget.line_items
        )
        or any(
            not any(
                category.category == required_category
                and category.included
                and category.complete
                for category in budget.category_totals
            )
            or not any(
                item.category == required_category
                and item.amount is not None
                and item.currency == budget.currency
                for item in budget.line_items
            )
            for required_category in (
                "accommodation",
                "local_transport",
                "intercity_transport",
                "food",
                "activities",
            )
        )
    ):
        return (
            "INSUFFICIENT_DATA",
            "Required costs are incomplete or unpriced; affordability cannot be approved.",
        )
    if (
        budget.currency != requirements.currency
        or budget.total_budget != requirements.total_budget
        or budget.estimated_total > budget.total_budget
    ):
        status: WorkflowStatus = (
            "REVISION_REQUIRED"
            if budget.estimated_total > budget.total_budget
            else "INSUFFICIENT_DATA"
        )
        return (
            status,
            "Budget output conflicts with the original request or exceeds its hard limit; approval was blocked.",
        )
    if budget.status != "WITHIN_BUDGET":
        return (
            "REVISION_REQUIRED",
            "Budget status is not within budget; approval was blocked.",
        )
    expected_total = sum(
        (item.amount for item in budget.line_items if item.amount is not None),
        Decimal("0"),
    )
    if (
        expected_total != budget.known_cost_subtotal
        or expected_total != budget.estimated_total
        or budget.remaining_budget
        != budget.total_budget - budget.estimated_total
        or budget.over_budget_amount
        != max(Decimal("0"), budget.estimated_total - budget.total_budget)
    ):
        return (
            "INSUFFICIENT_DATA",
            "Budget line items do not reconcile to the reported total; approval was blocked.",
        )
    requested_budget = state["request"].total_budget
    requested_currency = state["request"].currency
    if (
        requested_budget is not None
        and requested_budget != budget.total_budget
    ) or (
        requested_currency is not None
        and requested_currency != budget.currency
    ):
        return (
            "INSUFFICIENT_DATA",
            "The budget agent result does not preserve the user's original budget.",
        )
    return "RUNNING", None


def _revision_node(state: TravelWorkflowState) -> dict[str, Any]:
    review = state.get("optimization_review")
    requirements = state.get("normalized_requirements")
    if review is None or requirements is None:
        return {
            "current_stage": "revision",
            "execution_status": "INSUFFICIENT_DATA",
            "errors": ["Revision inputs are incomplete; no changes were applied."],
        }

    previous = set(state.get("excluded_attraction_ids", []))
    research_result = state.get("destination_research")
    researched_ids = (
        {
            item.place_id
            for item in research_result.attractions
            if item.place_id
        }
        if research_result is not None
        else set()
    )
    candidates = {
        issue.place_id
        for issue in review.issues
        if issue.blocks_approval
        and issue.code in _REVISION_ISSUE_CODES
        and issue.place_id
        and issue.place_id in researched_ids
    }
    new_ids = sorted(candidates - previous)[:50]
    if not new_ids:
        return {
            "current_stage": "revision",
            "execution_status": "REVISION_REQUIRED",
            "revision_directive": None,
            "warnings": [
                "No new, supported itinerary revision could address the blocking review issues."
            ],
        }

    directive = RevisionDirective(
        excluded_attraction_ids=new_ids,
        reason="Exclude attraction entries that failed deterministic evidence checks.",
    )
    constraints = dict(requirements.constraints)
    constraints["excluded_attraction_ids"] = sorted(previous | set(new_ids))
    revised_requirements = requirements.model_copy(
        update={"constraints": constraints}
    )
    return {
        "current_stage": "revision",
        "execution_status": "RUNNING",
        "normalized_requirements": revised_requirements,
        "excluded_attraction_ids": sorted(previous | set(new_ids)),
        "revision_directive": directive,
        "revision_count": state.get("revision_count", 0) + 1,
        "budget_inputs_changed": False,
        "warnings": [
            "Applied a validated itinerary-only revision; original budget assumptions and total were preserved."
        ],
    }


def _after_revision(state: TravelWorkflowState) -> str:
    if state.get("execution_status") != "RUNNING":
        return END
    if state.get("budget_inputs_changed"):
        return "budget"
    return "itinerary"


def _new_sources(
    state: TravelWorkflowState,
    output: BaseModel,
) -> list[SourceReference]:
    existing = {
        str(item.url)
        for item in state.get("source_references", [])
    }
    found: list[SourceReference] = []

    def visit(value: Any) -> None:
        if isinstance(value, BaseModel):
            visit(value.model_dump(mode="python"))
        elif isinstance(value, dict):
            for key, nested in value.items():
                if key == "source_references" and isinstance(nested, list):
                    for item in nested:
                        try:
                            reference = SourceReference.model_validate(item)
                        except ValidationError:
                            continue
                        url = str(reference.url)
                        if url not in existing:
                            existing.add(url)
                            found.append(reference)
                else:
                    visit(nested)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(output)
    return found


def _sanitize_text(message: str) -> str:
    without_assignments = _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", message)
    return _BEARER_TOKEN.sub("Bearer [REDACTED]", without_assignments)


def _invalid_request_result() -> TravelWorkflowState:
    return {
        "current_stage": "validation",
        "execution_status": "INVALID_REQUEST",
        "warnings": [],
        "errors": ["The travel request is invalid; correct its fields and try again."],
        "revision_count": 0,
        "agent_history": [],
        "source_references": [],
    }


async def _close_agents(agents: TravelAgentSet) -> bool:
    failed = False
    closed: set[int] = set()
    for agent in agents.all():
        if id(agent) in closed:
            continue
        closed.add(id(agent))
        close = getattr(agent, "aclose", None)
        if not callable(close):
            continue
        try:
            await close()
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True
    return failed


async def plan_trip(
    request: TravelRequest | dict[str, Any],
    *,
    agents: TravelAgentSet | None = None,
    max_revisions: int = 2,
    timeout_seconds: float = 180.0,
    agent_timeout_seconds: float = 60.0,
    on_state=None,
) -> TravelWorkflowState:
    """Run the planning workflow asynchronously and return its final state."""
    if max_revisions < 0:
        raise ValueError("max_revisions must be zero or greater.")
    if timeout_seconds <= 0 or agent_timeout_seconds <= 0:
        raise ValueError("Workflow and agent timeouts must be positive.")
    try:
        parsed_request = TravelRequest.model_validate(request)
    except ValidationError:
        return _invalid_request_result()

    resolved_agents = agents or create_default_agents()
    initial: TravelWorkflowState = {
        "request": parsed_request,
        "current_stage": "requirements",
        "execution_status": "RUNNING",
        "warnings": [],
        "errors": [],
        "revision_count": 0,
        "max_revisions": max_revisions,
        "agent_timeout_seconds": agent_timeout_seconds,
        "agent_history": [],
        "source_references": [],
        "excluded_attraction_ids": [],
        "revision_directive": None,
        "budget_inputs_changed": False,
    }
    result = initial
    try:
        graph = build_travel_graph(
            resolved_agents,
            max_revisions=max_revisions,
        )
        async with asyncio.timeout(timeout_seconds):
            # Retain every completed stage if a later node or provider fails.
            async for snapshot in graph.astream(initial, stream_mode="values"):
                result = cast(TravelWorkflowState, snapshot)
                if on_state is not None and result.get("agent_history"):
                    await on_state(result)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        result = {
            **result,
            "current_stage": result.get("current_stage", "workflow"),
            "execution_status": "FAILED",
            "errors": [
                *result.get("errors", []),
                "The workflow exceeded its configured timeout.",
            ],
        }
    except Exception:
        result = {
            **result,
            "current_stage": result.get("current_stage", "workflow"),
            "execution_status": "FAILED",
            "errors": [
                *result.get("errors", []),
                "Workflow execution failed; sensitive details were suppressed.",
            ],
        }
    finally:
        cleanup_failed = await _close_agents(resolved_agents)
        if cleanup_failed:
            result = {
                **result,
                "warnings": [
                    *result.get("warnings", []),
                    "One or more workflow agent resources could not be closed cleanly.",
                ],
            }
    return result
