"""Conservative, snapshot-based cost changes. No live calls or invented prices."""
from decimal import Decimal

from ..schemas.budget import BudgetBreakdown, BudgetCostInput, BudgetLineItem
from .cost_estimation_service import CostEstimationService, ReferenceData
from .ai_cost_safety import ai_budget_issue, ai_line_issue, current_ai_scope, has_ai_cost, rate_scope_changed
from ..schemas.ai_cost_estimation import AICostProvenance


def reconciled_snapshot(context):
    try:
        raw = context.budget_breakdown
        budget = BudgetBreakdown.model_validate(raw.model_dump(mode="python") if isinstance(raw, BudgetBreakdown) else raw or {})
    except (ValueError, TypeError):
        return None
    requirements = context.normalized_requirements
    if requirements is None or budget.status == "INVALID_INPUT" or budget.total_budget is None:
        return None
    if (budget.total_budget != requirements.total_budget or budget.currency != requirements.currency
        or budget.total_budget != context.request.total_budget or budget.currency != context.request.currency):
        return None
    ai_lines = [item for item in budget.line_items if has_ai_cost(item)]
    if ai_lines and (budget.estimate_confidence != "low" or budget.booking_readiness != "unverified"
                     or any(item.ai_provenance is None for item in ai_lines)):
        return None
    totals = {total.category: total for total in budget.category_totals if total.included}
    if len(totals) != sum(total.included for total in budget.category_totals):
        return None
    if any(item.currency != budget.currency or item.category not in totals for item in budget.line_items):
        return None
    for category, total in totals.items():
        lines = [item for item in budget.line_items if item.category == category]
        if (total.currency != budget.currency or total.line_item_count != len(lines)
            or total.known_subtotal != sum((item.amount or Decimal("0") for item in lines), Decimal("0"))):
            return None
    subtotal = sum((item.amount or Decimal("0") for item in budget.line_items), Decimal("0"))
    if subtotal != budget.known_cost_subtotal:
        return None
    if budget.estimated_total is not None and (
        budget.estimated_total != subtotal or budget.remaining_budget != budget.total_budget - subtotal
        or budget.over_budget_amount != max(Decimal("0"), subtotal - budget.total_budget)
    ):
        return None
    return budget


class _SnapshotFX:
    def __init__(self, budget):
        self.conversions = {
            (item.currency_conversion.original_currency, item.currency_conversion.target_currency):
            item.currency_conversion for item in budget.line_items if item.currency_conversion
        }

    async def conversion(self, base, quote):
        value = self.conversions[(base, quote)]
        return value.rate, value.rate_date, str(value.source_url)


async def propose_revision(context, estimator):
    budget = reconciled_snapshot(context)
    if budget is None or budget.known_cost_subtotal is None or budget.known_cost_subtotal <= budget.total_budget:
        return None
    requirements = context.normalized_requirements
    if ai_budget_issue(budget, requirements, context.destination_research, context.accommodation_transport_plan, context.request):
        return None
    # Unknown hard constraints and free-text standards require user confirmation.
    permitted = {"budget_assumptions", "excluded_attraction_ids"}
    if context.request.user_message or any(value for key, value in requirements.constraints.items() if key not in permitted):
        return None
    assumptions = requirements.constraints.get("budget_assumptions", {})
    if not isinstance(assumptions, dict):
        return None
    previous = context.budget_revision or {}
    removed = set(previous.get("removed_other_indexes", []))
    from ..agents.budget_agent import _entry_from_input, _trip_days
    for index, raw in enumerate(assumptions.get("other_expenses", [])):
        if index in removed:
            continue
        cost = BudgetCostInput.model_validate(raw)
        if not cost.optional:
            continue
        item = _entry_from_input("other", cost, requirements.currency,
                                 requirements.travelers_count, _trip_days(requirements)).item
        if item.amount is not None and item.amount > 0 and any(
            line.category == "other" and line.optional and line.amount == item.amount
            and line.currency == item.currency and line.description == item.description
            and line.source == item.source for line in budget.line_items
        ):
            removed.add(index)

    replacements = dict(previous.get("replacement_items", {}))
    # Economy references are alternative planning allowances, never lower range
    # bounds or hotel offers. Do not alter premium standards or explicit rates.
    if estimator.settings.AUTOMATIC_COST_ESTIMATION_ENABLED:
        try:
            data = (ReferenceData.model_validate(estimator.reference_data) if estimator.reference_data is not None
                    else ReferenceData.model_validate_json(estimator.settings.COST_REFERENCE_FILE.read_text(encoding="utf-8")))
            data = data.model_copy(deep=True)
        except (OSError, ValueError):
            data = None
        style = data.style_aliases.get((requirements.travel_style or "balanced").casefold()) if data else None
        if data and style in {"balanced", "budget"}:
            fx = _SnapshotFX(budget)
            data.rates = [row for row in data.rates if row.currency == requirements.currency
                          or (row.currency, requirements.currency) in fx.conversions]
            preferences = requirements.cost_preferences
            categories = [category for category, explicit in (
                ("accommodation", preferences and preferences.accommodation_nightly_rate_per_room is not None),
                ("food", (preferences and preferences.food_allowance_per_traveler_per_day is not None)
                 or assumptions.get("food") is not None),
                ("local_transport", (preferences and preferences.local_transport_allowance_per_day is not None)
                 or assumptions.get("local_transport") is not None),
                ("activities", (preferences and preferences.activities_allowance_per_traveler_per_trip is not None)
                 or assumptions.get("activities") is not None),
            ) if not explicit and (category in {"accommodation", "food"} or previous.get("interactive"))
                and any(line.category == category and line.amount is not None
                                      and line.source == "reference_data" for line in budget.line_items)]
            if previous.get("interactive") and not previous.get("allow_economy_accommodation") and style != "budget":
                categories = [category for category in categories if category != "accommodation"]
            if categories:
                from ..schemas.accommodation_transport import AccommodationTransportPlan
                plan = AccommodationTransportPlan.model_validate(context.accommodation_transport_plan) if context.accommodation_transport_plan else None
                service = CostEstimationService(estimator.settings, fx=fx, reference_data=data.model_dump(mode="json"))
                alternatives, _, _ = await service.estimate_missing(
                    requirements.model_copy(update={"travel_style": "budget"}), context.destination_research, plan, categories)
                for category, alternative in alternatives.items():
                    current = [line for line in budget.line_items if line.category == category]
                    if (len(current) == 1 and alternative.amount is not None and alternative.amount < current[0].amount
                        and alternative.source == "reference_data" and alternative.price_sources):
                        alternative.assumptions.append(
                            "Economy planning alternative selected to reduce costs; original trip requirements are preserved. No availability or booking price is asserted.")
                        replacements[category] = alternative.model_dump(mode="json")
    directive = {"replacement_items": replacements, "removed_other_indexes": sorted(removed)}
    if directive == {"replacement_items": previous.get("replacement_items", {}),
                     "removed_other_indexes": previous.get("removed_other_indexes", [])}:
        return None
    if previous.get("interactive"):
        directive.update(interactive=True, allow_economy_accommodation=previous.get("allow_economy_accommodation", False))
    return directive


def revised_entries(context, entries):
    """Retain AI rates, recalculate quantities, and invalidate changed rate scope.

    A revision may remove supported optional expenses or replace supported
    references. It cannot replace an AI line or choose its lower scenario rate.
    BudgetAgent retries pricing only for invalidated rate scopes, never merely
    because a total exceeds the user's budget. Reserve arithmetic stays there.
    """
    from ..agents.budget_agent import _BudgetEntry
    budget = reconciled_snapshot(context)
    if budget is None:
        raise ValueError("Cannot revise an unreconciled pricing snapshot.")
    directive = context.budget_revision
    replacements = {key: BudgetLineItem.model_validate(value)
                    for key, value in directive.get("replacement_items", {}).items()}
    removed = set(directive.get("removed_other_indexes", []))
    totals = {total.category: total for total in budget.category_totals}
    result, automatic, actions = [], {}, list(budget.missing_price_actions)
    try:
        expected_scope = current_ai_scope(context.normalized_requirements, context.destination_research, context.accommodation_transport_plan)
    except (ValueError, TypeError):
        expected_scope = None
    other_index = 0
    for entry in entries:
        category = entry.item.category
        if category == "other":
            if other_index not in removed:
                result.append(entry)
            other_index += 1
            continue
        lines = [item for item in budget.line_items if item.category == category]
        if len(lines) != 1:
            raise ValueError("Required categories must have one snapshot line.")
        item = replacements.get(category, lines[0])
        if category in replacements and has_ai_cost(lines[0]):
            raise ValueError("Revision directives cannot replace or manipulate AI assumptions.")
        if category in replacements and (
            category not in {"accommodation", "food", "local_transport", "activities"} or item.source != "reference_data"
            or not item.price_sources or item.currency != budget.currency or item.amount is None
            or lines[0].amount is None or item.amount > lines[0].amount
        ):
            raise ValueError("Unsupported budget replacement.")
        complete = totals[category].complete
        if has_ai_cost(item):
            # An explicit new allowance is authoritative; never claim it was
            # generated by AI. A zero-night stay is a deterministic zero too.
            if entry.category_complete and entry.item.source == "user_assumption":
                item = entry.item
                complete = True
            elif category == "accommodation" and entry.category_complete and entry.item.amount == 0:
                item = entry.item
                complete = True
            else:
                provenance = AICostProvenance.model_validate(item.ai_provenance)
                if expected_scope is None or rate_scope_changed(provenance.scope, expected_scope, category):
                    item = _invalidated_ai_line(category, budget.currency)
                    complete = False
                    actions.append(f"{category}: trip scope changed; obtain applicable provider/reference pricing or a fresh validated AI planning assumption.")
                else:
                    if provenance.scope != expected_scope:
                        provenance = AICostProvenance.model_validate(provenance.model_copy(update={
                            "generation_scope": provenance.generation_scope or provenance.scope,
                            "scope": expected_scope, "quantity": expected_scope.quantity_for(category),
                        }).model_dump(mode="python"))
                        raw = item.model_dump(mode="python")
                        raw["ai_provenance"] = provenance.model_dump(mode="json")
                        for rate_field, amount_field in (("typical", "amount"), ("minimum", "minimum_amount"), ("maximum", "maximum_amount")):
                            rate = getattr(provenance.unit_rate, rate_field)
                            raw[amount_field] = (rate * provenance.quantity).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP") if rate is not None else None
                        raw["pricing_basis"] = f"Preserved AI {provenance.unit_rate.unit} rate × {provenance.quantity}; Python recalculated quantities."
                        raw["assumptions"] = [*item.assumptions, "Applied scope/quantities were updated using preserved AI unit rates; no new Gemini price or booking verification was requested."]
                        item = BudgetLineItem.model_validate(raw)
                    if ai_line_issue(item, context.normalized_requirements, context.destination_research, context.accommodation_transport_plan):
                        item = _invalidated_ai_line(category, budget.currency)
                        complete = False
                        actions.append(f"{category}: current constraints require matching prices or a user allowance; the saved AI assumption is inapplicable.")
        result.append(_BudgetEntry(item, complete))
        if (item.source in {"reference_data", "verified_provider"} or item.ai_provenance is not None) and item.amount is not None:
            automatic[category] = item
    return result, automatic, actions


def _invalidated_ai_line(category, currency):
    return BudgetLineItem(category=category, description=category.replace("_", " ").capitalize(),
        currency=currency, classification="unknown", source="unknown",
        pricing_basis="Saved AI planning assumption is inapplicable to the current scope.")


def ai_rates_requiring_refresh(context):
    """Only saved AI rates whose pricing context changed qualify for a retry."""
    budget = reconciled_snapshot(context)
    if budget is None:
        return []
    try:
        scope = current_ai_scope(context.normalized_requirements, context.destination_research, context.accommodation_transport_plan)
    except (ValueError, TypeError):
        return []
    return [item.category for item in budget.line_items if item.ai_provenance is not None
            and rate_scope_changed(AICostProvenance.model_validate(item.ai_provenance).scope, scope, item.category)
            and not (item.category == "accommodation" and scope.start_date == scope.end_date)]


def revised_plan(context, directive):
    """Keep the stay allowance consistent with the selected budget alternative."""
    raw = directive.get("replacement_items", {}).get("accommodation")
    if raw is None or context.accommodation_transport_plan is None:
        return None
    snapshot = reconciled_snapshot(context)
    if snapshot is None or any(has_ai_cost(item) for item in snapshot.line_items if item.category == "accommodation"):
        raise ValueError("A stay revision cannot replace AI assumptions with a provider/reference claim.")
    from ..schemas.accommodation_transport import AccommodationTransportPlan, CostEstimate
    from ..schemas.destination_research import SourceReference
    plan = AccommodationTransportPlan.model_validate(context.accommodation_transport_plan)
    item = BudgetLineItem.model_validate(raw)
    cost = CostEstimate(amount=item.amount, currency=item.currency, classification="estimate",
                        pricing_basis=item.pricing_basis, assumptions=item.assumptions)
    stay = plan.accommodation.model_copy(update={
        "recommended_types": ["budget_hotel"], "estimated_total_cost": cost,
        "estimated_nightly_rate": CostEstimate(currency=item.currency, classification="unknown",
            uncertainty_notes=["The alternative is a whole-trip reference allowance, not a property quote."]),
        "assumptions": [*plan.accommodation.assumptions, *item.assumptions],
    })
    priced = [cost, plan.local_transport_cost, plan.intercity_transport_cost]
    complete = all(value.amount is not None and value.currency == item.currency for value in priced)
    combined = CostEstimate(amount=sum((value.amount for value in priced), Decimal("0")) if complete else None,
        currency=item.currency, classification="estimate" if complete else "unknown",
        pricing_basis="Selected accommodation allowance plus original transport estimates; no live availability checked.")
    sources = [SourceReference(title=source.name, url=source.url) for source in item.price_sources if source.url]
    return plan.model_copy(update={"accommodation": stay, "accommodation_cost": cost,
                                  "combined_estimated_cost": combined,
                                  "source_references": [*plan.source_references, *sources]})
