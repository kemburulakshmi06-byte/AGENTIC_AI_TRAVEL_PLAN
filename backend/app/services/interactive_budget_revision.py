"""Snapshot-only interactive revisions; no fresh pricing or affordability targets."""
from decimal import Decimal
import hashlib
import json

from ..schemas.agent_schemas import AgentContext, TravelRequest
from ..schemas.budget import BudgetBreakdown
from ..schemas.trip_api import TripRevisionSummary
from .budget_revision_service import reconciled_snapshot


class RevisionConflict(ValueError):
    pass


def snapshot_for(details, request: TravelRequest, previous, allow_economy: bool):
    from ..schemas.accommodation_transport import AccommodationTransportPlan
    from ..schemas.agent_schemas import NormalizedTravelRequirements
    from ..schemas.destination_research import DestinationResearch
    from ..schemas.itinerary import TripItinerary
    from ..schemas.optimization import OptimizationReview
    from ..workflows.travel_graph import _budget_safety_status

    context = AgentContext(request=request, normalized_requirements=details.normalized_requirements,
        destination_research=details.destination_research, accommodation_transport_plan=details.accommodation_transport,
        budget_breakdown=details.budget, trip_itinerary=details.itinerary)
    budget = reconciled_snapshot(context)
    if (budget is None or budget.estimated_total is None or budget.estimated_total <= request.total_budget
        or budget.pricing_status == 'INCOMPLETE' or budget.unpriced_components
        or any(not row.complete for row in budget.category_totals if row.included)):
        raise RevisionConflict('Only a complete, reconciled over-budget plan can be revised.')
    snapshot = {'request': request}
    for field, value, model in (
        ('normalized_requirements', details.normalized_requirements, NormalizedTravelRequirements),
        ('destination_research', details.destination_research, DestinationResearch),
        ('accommodation_transport_plan', details.accommodation_transport, AccommodationTransportPlan),
        ('budget_breakdown', details.budget, BudgetBreakdown),
        ('trip_itinerary', details.itinerary, TripItinerary),
        ('optimization_review', details.optimization_review, OptimizationReview),
    ):
        snapshot[field] = model.model_validate(value)
    if _budget_safety_status(snapshot)[0] == 'INSUFFICIENT_DATA':
        raise RevisionConflict('The saved pricing evidence is incomplete or no longer applicable. Obtain matching prices before revision.')
    directive = dict(previous or {})
    directive.update(interactive=True, allow_economy_accommodation=allow_economy)
    snapshot['budget_revision'] = directive
    return {key: value.model_dump(mode='json') if hasattr(value, 'model_dump') else value
            for key, value in snapshot.items() if key != 'request'}


def pricing_fingerprint(snapshot):
    """Identify changed scope/prices/evidence, ignoring commentary and attempt dates."""
    rows = []
    for line in (snapshot.get('budget_breakdown') or {}).get('line_items', []):
        rows.append({key: line.get(key) for key in (
            'category', 'amount', 'currency', 'classification', 'source', 'optional',
            'ai_provenance', 'flight_reference', 'source_references', 'price_sources', 'currency_conversion')})
    raw = {'scope': snapshot.get('normalized_requirements'), 'prices': rows}
    return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()


def economy_permission_available(snapshot, settings):
    """A consent change must unlock a category this strategy can actually revise."""
    if not snapshot or settings is None or not settings.AUTOMATIC_COST_ESTIMATION_ENABLED:
        return False
    requirements = snapshot.get('normalized_requirements') or {}
    if (snapshot.get('request') or {}).get('user_message'):
        return False
    constraints = requirements.get('constraints') or {}
    if any(value for key, value in constraints.items() if key not in {'budget_assumptions', 'excluded_attraction_ids'}):
        return False
    preferences = requirements.get('cost_preferences') or {}
    if preferences.get('accommodation_nightly_rate_per_room') is not None:
        return False
    try:
        from .cost_estimation_service import ReferenceData
        data = ReferenceData.model_validate_json(settings.COST_REFERENCE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    if data.style_aliases.get((requirements.get('travel_style') or 'balanced').casefold()) != 'balanced':
        return False
    if ((snapshot.get('budget_revision') or {}).get('replacement_items') or {}).get('accommodation'):
        return False
    stays = [line for line in (snapshot.get('budget_breakdown') or {}).get('line_items', [])
             if line.get('category') == 'accommodation' and line.get('source') == 'reference_data'
             and line.get('amount') is not None and not line.get('ai_provenance')]
    # A historically selected economy reference does not become a fresh option
    # just because older persisted records omitted the revision directive.
    return any(not any('budget' in row.styles and 'balanced' not in row.styles
                       and (line.get('pricing_basis') or '').startswith(f'Reference {row.id}:')
                       for row in data.rates) for line in stays)


def exhausted_inputs(metadata, snapshot, allow_economy):
    fingerprint = pricing_fingerprint(snapshot)
    for item in metadata.get('exhausted_revision_inputs', []):
        if item['fingerprint'] == fingerprint and (item['allow_economy_accommodation'] or not allow_economy):
            return True
    # Older saved jobs do not carry fingerprints; keep their no-op protection.
    if not metadata.get('exhausted_revision_inputs'):
        history = metadata.get('revision_history', [])
        return bool(history and (history[-1]['outcome'] == 'no_supported_alternative'
                    or history[-1].get('alternatives_exhausted'))
                    and (metadata.get('last_economy_permission') or not allow_economy))
    return False


def revision_summary(metadata, limit, *, snapshot=None, settings=None):
    history = metadata.get('revision_history', [])
    attempts = metadata.get('interactive_attempts', 0)
    pending = metadata.get('revision_pending', False)
    snapshot = snapshot or metadata.get('revision_result_snapshot') or metadata.get('revision_snapshot') or {}
    exhausted = exhausted_inputs(metadata, snapshot, False)
    permission = bool(metadata.get('last_economy_permission'))
    affordable = bool(history and history[-1]['outcome'] == 'within_budget')
    incomplete = bool(history and history[-1]['outcome'] == 'incomplete')
    economy = economy_permission_available(snapshot, settings)
    new_permission = exhausted and not permission and economy
    return TripRevisionSummary(attempts=attempts, max_attempts=limit,
        can_retry=not pending and not affordable and not incomplete and attempts < limit and (not exhausted or new_permission),
        requires_economy_permission=new_permission, economy_accommodation_available=economy,
        history=history)


def finish_revision(job, result):
    metadata = dict(job.output_data or {})
    if not metadata.get('revision_pending'):
        return
    baseline = metadata['revision_snapshot']['budget_breakdown']
    before = Decimal(baseline['estimated_total'])
    original_budget = Decimal(baseline['total_budget'])
    budget = result.get('budget_breakdown')
    failed = result.get('execution_status') in {'FAILED', 'INVALID_REQUEST'}
    incomplete = (result.get('execution_status') == 'INSUFFICIENT_DATA' or budget is None
        or budget.estimated_total is None or budget.pricing_status == 'INCOMPLETE'
        or budget.total_budget != original_budget or budget.currency != baseline['currency']
        or bool(budget.unpriced_components) or any(not row.complete for row in budget.category_totals if row.included))
    after = budget.estimated_total if budget is not None and not failed and not incomplete else None
    changes = []
    if budget is not None:
        for category in ('accommodation', 'local_transport', 'intercity_transport', 'food', 'activities', 'contingency', 'other'):
            old_rows = [row for row in baseline['line_items'] if row['category'] == category]
            new_rows = [row for row in budget.line_items if row.category == category]
            if any(row.amount is None for row in new_rows):
                changes.append(f'{category}: pricing unavailable')
                continue
            if not new_rows and category != 'other':
                continue
            old = sum((Decimal(row['amount']) for row in old_rows if row.get('amount') is not None), Decimal(0))
            new = sum((row.amount for row in new_rows), Decimal(0))
            if old != new:
                changes.append(f'{category}: {old:.2f} → {new:.2f} {budget.currency}')
    old_itinerary = metadata['revision_snapshot'].get('trip_itinerary', {})
    itinerary = result.get('trip_itinerary')
    if itinerary is not None:
        old_titles = [activity.get('title') for day in old_itinerary.get('days', []) for activity in day.get('activities', [])]
        new_titles = [activity.title for day in itinerary.days for activity in day.activities]
        if old_titles != new_titles:
            changes.append('Day-by-day activities were revised using researched places.')
    outcome = ('failed' if failed else 'incomplete' if incomplete else 'within_budget' if after <= original_budget
               else 'improved' if after < before else 'no_supported_alternative')
    exhausted = not failed and not incomplete and (
        outcome == 'no_supported_alternative' or (result.get('current_stage') == 'budget_revision'
        and any('No further supported cost reduction' in warning for warning in result.get('warnings', []))))
    message = {
        'failed': 'Budget revision could not be completed.',
        'incomplete': 'Pricing incomplete — a final budget is unavailable.',
        'within_budget': 'Budget optimized successfully.',
        'improved': 'Best supported plan found — still over budget.',
        'no_supported_alternative': 'No further compatible savings available with current pricing information.',
    }[outcome]
    explanation = {
        'failed': 'The previous plan is retained; review the error before retrying.',
        'incomplete': 'Required pricing or applicable evidence is missing; affordability cannot be confirmed.',
        'within_budget': 'Supported reductions fit the original budget; planning approval remains subject to review.',
        'improved': 'No further compatible savings available with current pricing information.' if exhausted
                    else 'Compatible costs were reduced, but the original budget is still exceeded.',
        'no_supported_alternative': 'Current evidence does not support a further reduction without new prices or approved changes.',
    }[outcome]
    history = metadata.get('revision_history', [])
    first_total = Decimal(history[0]['original_total']) if history else before
    record = {'attempt': metadata['interactive_attempts'], 'original_total': str(before),
        'revised_total': str(after) if after is not None else None,
        'savings': str(before - after) if after is not None else None,
        'remaining_over_budget': str(max(Decimal(0), after - original_budget)) if after is not None else None,
        'currency': baseline['currency'], 'outcome': outcome, 'changes': changes, 'explanation': explanation,
        'outcome_message': message, 'original_budget': str(baseline['total_budget']),
        'cumulative_savings': str(first_total - after) if after is not None else None,
        'alternatives_exhausted': exhausted}
    final_snapshot = dict(metadata['revision_snapshot'])
    if budget is not None:
        final_snapshot['budget_breakdown'] = budget.model_dump(mode='json')
    final_snapshot['budget_revision'] = result.get('budget_revision') or final_snapshot.get('budget_revision')
    exhausted_history = list(metadata.get('exhausted_revision_inputs', []))
    if exhausted:
        exhausted_history.append({'fingerprint': pricing_fingerprint(final_snapshot),
            'allow_economy_accommodation': bool(metadata.get('last_economy_permission'))})
    job.output_data = {**metadata, 'revision_pending': False, 'revision_result_snapshot': final_snapshot,
                       'exhausted_revision_inputs': exhausted_history,
                       'revision_history': [*history, record]}
