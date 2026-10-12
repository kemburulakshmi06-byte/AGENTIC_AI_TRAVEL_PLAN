"""Allowlisted diagnostics; never include request payloads or exception bodies."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

_trip = ContextVar("diagnostic_trip", default=None)
_agent = ContextVar("diagnostic_agent", default=None)
logger = logging.getLogger("travel.diagnostics")


def configure_diagnostics():
    # HTTP request INFO/DEBUG messages can include query-string API keys.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


@contextmanager
def diagnostic_context(*, trip_id=None, agent=None):
    tokens = []
    if trip_id is not None:
        tokens.append((_trip, _trip.set(str(trip_id))))
    if agent is not None:
        tokens.append((_agent, _agent.set(agent)))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def record_event(provider, operation, outcome, started, *, category=None, retry_count=0, status_code=None):
    event = {
        "trip_id": _trip.get(), "agent": _agent.get(), "provider": provider,
        "operation": operation, "duration_ms": round((perf_counter() - started) * 1000, 2),
        "outcome": outcome, "error_category": category, "retry_count": retry_count,
        "http_status": status_code,
    }
    logger.log(logging.INFO if outcome == "success" else logging.WARNING, json.dumps(event))


def record_requirements_rule(rule, *, field=None, outcome="failed", category=None):
    """Rule IDs and schema field names only; never log compared input values."""
    rules = {
        "minimum_exceeds_maximum", "minimum_exceeds_total", "required_and_excluded",
        "selected_and_excluded", "explicit_text_mismatch", "location_qualification_compatible",
        "soft_style_difference", "llm_response_invalid", "missing_required_field",
        "explicit_value_changed", "unexpected_agent_response",
    }
    fields = {"origin", "destination", "start_date", "end_date", "travelers_count",
              "total_budget", "currency", "travel_style", "interests", "constraints"}
    if rule not in rules or (field is not None and field not in fields):
        raise ValueError("Diagnostic rule or field is not allowlisted.")
    safe_categories = {None, "invalid_schema", "invalid_json", "timeout", "authentication",
                       "rate_limit", "network", "model_unavailable", "not_configured", "truncated",
                       "refusal", "validation", "http_error", "empty_response", "invalid_shape"}
    event = {"trip_id": _trip.get(), "agent": _agent.get() or "requirements",
             "provider": "validation", "operation": "requirements.validate",
             "rule": rule, "field": field, "outcome": outcome,
             "error_category": category if category in safe_categories else "validation"}
    logger.log(logging.INFO if outcome == "success" else logging.WARNING, json.dumps(event))


def record_state_snapshot(state):
    """Structural persistence evidence only: no request text or provider bodies."""
    budget = state.get("budget_breakdown")
    itinerary = state.get("trip_itinerary")
    event = {"trip_id": _trip.get(), "agent": state.get("current_stage"),
        "provider": "database", "operation": "state.snapshot", "outcome": "persisted",
        "requirements_present": state.get("normalized_requirements") is not None,
        "research_present": state.get("destination_research") is not None,
        "accommodation_transport_present": state.get("accommodation_transport_plan") is not None,
        "budget_present": budget is not None,
        "complete_total_present": budget is not None and getattr(budget, "estimated_total", None) is not None,
        "itinerary_present": itinerary is not None,
        "day_count": len(getattr(itinerary, "days", [])) if itinerary else 0,
        "agent_output_count": len(state.get("agent_history", []))}
    logger.info(json.dumps(event))


def safe_failure_reason(error):
    """Expose only a fixed category, never the exception message or body."""
    category = getattr(error, "category", "validation")
    reasons = {
        "not_configured": "provider credentials are not configured",
        "authentication": "provider authentication was rejected",
        "rate_limit": "provider rate limit reached",
        "timeout": "provider request timed out",
        "network": "provider could not be reached",
        "http_error": "provider rejected the request",
        "no_route": "provider could not find a supported route between these locations",
        "invalid_json": "provider returned invalid JSON",
        "invalid_shape": "provider response structure was invalid",
        "invalid_schema": "provider output did not match the required schema",
        "invalid_coordinates": "provider returned invalid coordinates",
        "irrelevant_results": "provider returned places outside the requested area or inaccessible places",
        "empty_response": "provider returned no content",
        "refusal": "AI suggestions were blocked by the provider",
        "truncated": "AI output reached its token limit",
        "model_unavailable": "configured AI model is unavailable",
        "validation": "request or response validation failed",
    }
    return reasons.get(category, "provider is unavailable")
