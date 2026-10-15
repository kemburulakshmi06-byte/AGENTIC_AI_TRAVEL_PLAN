"""Shared applicability checks for non-live AI planning assumptions.

Travelers/rooms are quantities: reuse unit rates and retain generation scope.
Dates, destination, currency and style affect rates for every category. Origin
affects intercity fares; transport mode affects intercity/local transport.
Budget amounts and excluded attraction IDs never justify cheaper AI rates.
"""
from ..schemas.accommodation_transport import AccommodationTransportPlan
from ..schemas.ai_cost_estimation import AICostEstimationRequest, AICostScope, AICostProvenance
from ..schemas.budget import BudgetLineItem
from ..schemas.destination_research import DestinationResearch


def has_ai_cost(item):
    return (item.ai_provenance is not None
            or item.description.startswith("AI planning allowance:")
            or any(text.startswith("AI-generated planning assumption") for text in item.assumptions))


def current_ai_scope(requirements, research_data=None, plan_data=None):
    research = DestinationResearch.model_validate(research_data) if research_data is not None else None
    plan = AccommodationTransportPlan.model_validate(plan_data) if plan_data is not None else None
    if research and (research.resolution_status not in {None, "verified"} or len(research.city_segments) > 1):
        raise ValueError("AI costs require a resolved single-destination scope.")
    preferences = requirements.cost_preferences
    rooms = preferences.room_count if preferences else None
    if rooms is None and plan is not None:
        rooms = plan.accommodation.room_count
    if rooms is None and requirements.travelers_count is not None:
        occupancy = plan.accommodation.assumed_occupancy_per_room if plan else 2
        rooms = (requirements.travelers_count + occupancy - 1) // occupancy
    scoped = requirements.model_copy(update={"destination": research.destination_name
        if research and research.destination_name else requirements.destination})
    request = AICostEstimationRequest.from_requirements(scoped, room_count=rooms, missing_categories=[])
    if requirements.travel_duration_days is not None and requirements.travel_duration_days != (request.end_date - request.start_date).days + 1:
        raise ValueError("Trip duration does not match the requested dates.")
    return AICostScope.model_validate(request.model_dump(exclude={"missing_categories"}))


def rate_scope_changed(old_scope, new_scope, category):
    fields = ["destination", "start_date", "end_date", "currency", "travel_style"]
    if category == "intercity_transport":
        fields.append("origin")
    if category in {"intercity_transport", "local_transport"}:
        fields.append("transport_mode")
    return any(getattr(old_scope, field) != getattr(new_scope, field) for field in fields)


def ai_line_issue(item, requirements, research_data=None, plan_data=None):
    if not has_ai_cost(item):
        return None
    if item.ai_provenance is None:
        return "missing_ai_provenance"
    try:
        provenance = AICostProvenance.model_validate(item.ai_provenance)
        # Validate original attributes too: model_copy can bypass validators,
        # and the general budget schema derives evidence_kind on normal parsing.
        provenance.validate_line_item(item)
        BudgetLineItem.model_validate(item.model_dump(mode="python"))
        expected = current_ai_scope(requirements, research_data, plan_data)
        if provenance.scope != expected:
            return "invalid_ai_trip_scope"
        plan = AccommodationTransportPlan.model_validate(plan_data) if plan_data is not None else None
        if item.category == "intercity_transport" and plan and plan.origin_resolution_status == "broad":
            return "invalid_ai_trip_scope"
        if item.category == "accommodation" and expected.room_count * (plan.accommodation.assumed_occupancy_per_room if plan else 2) < expected.travelers_count:
            return "invalid_ai_trip_scope"
        if item.category == "activities" and (requirements.constraints.get("must_visit_attraction_ids")
            or any(term in " ".join(requirements.interests).casefold() for term in ("theme park", "guided tour", "premium"))):
            return "invalid_ai_activity_scope"
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        return "invalid_ai_provenance"
    return None


def ai_budget_issue(budget, requirements, research_data=None, plan_data=None, original_request=None):
    ai_items = [item for item in budget.line_items if has_ai_cost(item)]
    if not ai_items:
        return None
    if budget.booking_readiness != "unverified" or budget.pricing_status == "LIVE_PRICE_VERIFIED":
        return "invalid_ai_booking_evidence"
    if budget.estimate_confidence != "low":
        return "invalid_ai_budget_confidence"
    if original_request is not None:
        for field in ("origin", "destination", "start_date", "end_date", "travelers_count", "currency", "travel_style"):
            supplied = getattr(original_request, field)
            # Match the existing Requirements Agent's textual normalization,
            # without permitting a different city, style or travel scope.
            if supplied is not None and field in {"origin", "destination", "travel_style"}:
                supplied = " ".join(supplied.split())
                if field == "travel_style":
                    supplied = supplied.casefold()
            if supplied is not None and supplied != getattr(requirements, field):
                return "invalid_ai_trip_scope"
        supplied_mode = original_request.constraints.get("intercity_routing_mode")
        if supplied_mode and supplied_mode != requirements.constraints.get("intercity_routing_mode"):
            return "invalid_ai_trip_scope"
        supplied = original_request.cost_preferences
        if supplied is not None:
            actual = requirements.cost_preferences
            for field in ("room_count", "transport_mode"):
                if getattr(supplied, field) is not None and (actual is None or getattr(supplied, field) != getattr(actual, field)):
                    return "invalid_ai_trip_scope"
    for item in ai_items:
        issue = ai_line_issue(item, requirements, research_data, plan_data)
        if issue:
            return issue
    subtotal = sum(item.amount for item in budget.line_items if item.amount is not None)
    if budget.known_cost_subtotal != subtotal:
        return "invalid_ai_budget_arithmetic"
    if budget.estimated_total is not None and (
        budget.estimated_total != subtotal or budget.total_budget is None
        or budget.remaining_budget != budget.total_budget - subtotal
        or budget.over_budget_amount != max(0, subtotal - budget.total_budget)
    ):
        return "invalid_ai_budget_arithmetic"
    minimum, maximum = budget.minimum_estimated_total, budget.maximum_estimated_total
    if minimum is not None or maximum is not None:
        if minimum is None or maximum is None or budget.estimated_total is None or any(item.amount is None for item in budget.line_items):
            return "invalid_ai_budget_ranges"
        for field, total in (("minimum_amount", minimum), ("maximum_amount", maximum)):
            if sum((getattr(item, field) if getattr(item, field) is not None else item.amount
                    for item in budget.line_items)) != total:
                return "invalid_ai_budget_ranges"
    return None
