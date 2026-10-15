"""Real API/queue/persistence with isolated SQLite and mocked six-agent inputs."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.database.models import AgentRun
from app.main import create_app
from app.schemas.agent_schemas import TravelRequest
from app.services.interactive_budget_revision import RevisionConflict
from app.services.trip_job_service import TripJobService, job_for
from app.services.trip_planning_service import TripPlanningService
from app.worker import process_claim
from app.workflows.travel_graph import plan_trip
from test_budget_revisions import context, build_budget, run_workflow
from test_durable_trip_jobs import sqlite_factory


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('No provider calls permitted')))


@pytest.fixture
def setup_revision(tmp_path):
    factory, engine = sqlite_factory(tmp_path / 'revision.sqlite')
    config = Settings(_env_file=None, APP_ENV='development', TRIP_WORKER_ENABLED=False,
                      AUTOMATIC_COST_ESTIMATION_ENABLED=False, GEMINI_API_KEY=None)
    def setup(optional=True, limit=2, reference=False, budget=None):
        config.TRIP_MAX_REVISIONS = limit
        ctx = context(optional=optional, reserve=True)
        if budget is not None:
            from decimal import Decimal
            ctx.request.total_budget = ctx.normalized_requirements.total_budget = Decimal(budget)
        if reference:
            config.AUTOMATIC_COST_ESTIMATION_ENABLED = True
            from test_budget_revisions import reference_data
            ctx.request.travel_style = ctx.normalized_requirements.travel_style = 'balanced'
            ctx.accommodation_transport_plan['accommodation_cost']['amount'] = None
            ctx.accommodation_transport_plan['accommodation']['estimated_total_cost']['amount'] = None
            agent, _ = build_budget(automatic=True, data=reference_data())
        else:
            agent, _ = build_budget()
        agent.run = AsyncMock(wraps=agent.run)
        initial, agents = run_workflow(ctx, agent, limit=0)
        async def workflow(request, **kwargs):
            if kwargs.get('revision_snapshot') is None:
                return initial
            return await plan_trip(request, agents=agents, **kwargs)
        cache = MagicMock()
        cache.create_task_status = AsyncMock()
        cache.update_task_status = AsyncMock()
        cache.get_task_status = AsyncMock(return_value=None)
        cache.aclose = AsyncMock()
        service = TripPlanningService(factory, cache, settings=config, workflow=workflow)
        ident = asyncio.run(service.create_trip(ctx.request))
        queue = TripJobService(factory, config)
        asyncio.run(process_claim(queue, queue.claim(), service))
        return service, queue, ident, initial, agents
    yield setup, factory, config
    engine.dispose()


def client_for(service, config, **kwargs):
    app = create_app(config)
    app.state.trip_planning_service_factory = lambda: service
    return TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 51000), **kwargs)


HEADERS = {'Origin': 'http://localhost:5174', 'X-Trip-Revision': '1'}


def test_revision_api_queue_real_recalculation_and_saved_reload(setup_revision):
    setup, factory, config = setup_revision
    service, queue, ident, initial, agents = setup()
    before = asyncio.run(service.get_trip(ident))
    assert str(before.total_estimated_cost) == '38500.00'
    with client_for(service, config) as client:
        response = client.post(f'/api/v1/trips/{ident}/revise', json={}, headers=HEADERS)
        assert response.status_code == 202
        assert response.json()['trip_id'] == str(ident)
        assert client.get(f'/api/v1/trips/{ident}/status').json()['status'] == 'queued'
        assert client.post(f'/api/v1/trips/{ident}/revise', json={}, headers=HEADERS).status_code == 409
        claim = queue.claim()
        assert queue.claim() is None
        assert claim.request == TravelRequest.model_validate(before.request)
        asyncio.run(process_claim(queue, claim, service))
        final = client.get(f'/api/v1/trips/{ident}').json()
    assert final['status'] == 'completed', final['errors']
    assert final['budget']['total_budget'] == '27500'
    assert final['budget']['estimated_total'] == '27500.00'
    assert final['optimization_review']['approved'] is True
    assert final['budget']['booking_readiness'] == 'unverified'
    assert final['budget_revision_summary']['can_retry'] is False
    record = final['budget_revision_summary']['history'][0]
    assert record['original_total'] == '38500.00'
    assert record['revised_total'] == '27500.00'
    assert record['savings'] == '11000.00'
    assert record['remaining_over_budget'] == '0'
    assert any('other:' in change for change in record['changes'])
    # Fresh service retrieves persisted results, without browser state.
    fresh = TripPlanningService(factory, service._task_status, settings=config)
    reloaded = asyncio.run(fresh.get_trip(ident))
    assert reloaded.budget_revision_summary.history[0].savings == 11000
    flight = lambda b: next(row for row in b['line_items'] if row['category'] == 'intercity_transport')
    assert flight(final['budget']) == flight(before.budget)
    assert len(agents.requirements.calls) == len(agents.destination_research.calls) == 1
    assert agents.budget.run.call_count == len(agents.itinerary.calls) == len(agents.optimization_review.calls) == 2
    with pytest.raises(RevisionConflict):
        asyncio.run(service.revise_trip(ident))  # Affordable plans are ineligible.


def test_no_feasible_reduction_no_repeated_identical_calls_and_limit(setup_revision):
    setup, _, config = setup_revision
    service, queue, ident, _, agents = setup(optional=False)
    original = asyncio.run(service.get_trip(ident))
    asyncio.run(service.revise_trip(ident))
    asyncio.run(process_claim(queue, queue.claim(), service))
    final = asyncio.run(service.get_trip(ident))
    assert final.status == 'revision_required', final.errors
    assert final.total_estimated_cost == original.total_estimated_cost
    assert not final.optimization_review.approved
    summary = final.budget_revision_summary
    assert summary.history[-1].outcome == 'no_supported_alternative'
    assert summary.history[-1].savings == 0
    assert not summary.requires_economy_permission
    assert not summary.can_retry
    assert not summary.economy_accommodation_available
    assert agents.budget.run.call_count == 1  # No pointless Budget/Gemini rerun.
    with pytest.raises(RevisionConflict, match='identical'):
        asyncio.run(service.revise_trip(ident))
    with pytest.raises(RevisionConflict, match='fixed or already optimized'):
        asyncio.run(service.revise_trip(ident, allow_economy_accommodation=True))
    assert agents.budget.run.call_count == 1



@pytest.mark.parametrize('case', ['production', 'remote', 'wrong_origin', 'missing_header', 'wrong_host'])
def test_revision_access_fails_closed(setup_revision, case):
    setup, _, config = setup_revision
    service, _, ident, _, _ = setup()
    if case == 'production': config.APP_ENV = 'production'
    app = create_app(config)
    app.state.trip_planning_service_factory = lambda: service
    headers = dict(HEADERS)
    if case == 'wrong_origin': headers['Origin'] = 'https://attacker.example'
    if case == 'missing_header': del headers['X-Trip-Revision']
    host = 'https://public.example' if case == 'wrong_host' else 'http://127.0.0.1:8000'
    peer = ('192.0.2.1', 1234) if case == 'remote' else ('127.0.0.1', 1234)
    with TestClient(app, base_url=host, client=peer) as client:
        response = client.post(f'/api/v1/trips/{ident}/revise', json={}, headers=headers)
    assert response.status_code == 403


def test_mutation_payload_cannot_change_budget_dates_or_prices(setup_revision):
    setup, _, config = setup_revision
    service, _, ident, _, _ = setup()
    with client_for(service, config) as client:
        response = client.post(f'/api/v1/trips/{ident}/revise', json={'total_budget': '1', 'travelers_count': 1}, headers=HEADERS)
        assert response.status_code == 422
        assert client.post('/api/v1/trips/00000000-0000-0000-0000-000000000000/revise', json={}, headers=HEADERS).status_code == 404


def test_failed_revision_retains_previous_plan_and_records_error(setup_revision):
    setup, _, _ = setup_revision
    service, queue, ident, _, _ = setup()
    before = asyncio.run(service.get_trip(ident))
    asyncio.run(service.revise_trip(ident))
    service._workflow = AsyncMock(side_effect=RuntimeError('private provider details'))
    asyncio.run(process_claim(queue, queue.claim(), service))
    final = asyncio.run(service.get_trip(ident))
    assert final.status == 'failed'
    assert final.budget['estimated_total'] == before.budget['estimated_total']
    assert final.budget_revision_summary.history[-1].outcome == 'failed'
    assert final.budget_revision_summary.history[-1].savings is None
    assert 'private provider details' not in str(final.model_dump())


def test_ai_rates_and_provenance_survive_actual_interactive_revision_without_new_cost_call():
    from datetime import datetime, timezone
    from decimal import Decimal
    from app.agents.itinerary_agent import ItineraryAgent
    from app.agents.optimization_agent import OptimizationAgent
    from app.schemas.accommodation_transport import AccommodationTransportPlan
    from app.schemas.destination_research import DestinationResearch
    from app.schemas.trip_api import TripDetailsResponse
    from app.services.interactive_budget_revision import snapshot_for
    from app.workflows.travel_graph import TravelAgentSet
    from test_ai_budget_safety import prepared_context, priced_context
    from test_ai_cost_estimation import settings as ai_settings
    from test_itinerary_agent import FakeRouting
    from test_travel_graph import FakeAgent

    async def run():
        ctx = prepared_context(budget='20000')
        ctx.request.constraints = ctx.normalized_requirements.constraints = {
            'budget_assumptions': {'other_expenses': [{'amount': '50000', 'optional': True, 'description': 'Optional premium experience'}]}}
        ctx, budget, agent, gemini, pricing = await priced_context(ctx)
        review_agent = OptimizationAgent(settings=ai_settings(GEMINI_API_KEY=None))
        review = (await review_agent.run(ctx)).output
        details = TripDetailsResponse(id='00000000-0000-0000-0000-000000000001', title='Fictional AI revision', status='revision_required',
            request=ctx.request.model_dump(mode='json'), total_estimated_cost=budget.estimated_total, currency=budget.currency,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc), budget=ctx.budget_breakdown,
            normalized_requirements=ctx.normalized_requirements.model_dump(mode='json'), destination_research=ctx.destination_research,
            accommodation_transport=ctx.accommodation_transport_plan, itinerary=ctx.trip_itinerary, optimization_review=review)
        snapshot = snapshot_for(details, ctx.request, None, False)
        agents = TravelAgentSet(requirements=FakeAgent('requirements', ctx.normalized_requirements),
            destination_research=FakeAgent('destination_research', DestinationResearch.model_validate(ctx.destination_research)),
            accommodation_transport=FakeAgent('accommodation_transport', AccommodationTransportPlan.model_validate(ctx.accommodation_transport_plan)),
            budget=agent, itinerary=ItineraryAgent(settings=ai_settings(GEMINI_API_KEY=None), routing=FakeRouting()), optimization_review=review_agent)
        original = {line.category: line.ai_provenance for line in budget.line_items if line.ai_provenance}
        cost_calls = len(gemini.cost_prompts)
        result = await plan_trip(ctx.request, agents=agents, revision_snapshot=snapshot)
        revised = result['budget_breakdown']
        assert revised.estimated_total < budget.estimated_total
        assert revised.total_budget == ctx.request.total_budget == Decimal('20000')
        assert len(gemini.cost_prompts) == cost_calls == 1
        assert not agents.requirements.calls and not agents.destination_research.calls
        for line in revised.line_items:
            if line.ai_provenance:
                assert line.ai_provenance == original[line.category]
                assert line.confidence == 'low' and line.evidence_kind == 'planning_estimate'
                assert not line.price_sources and not line.source_references and line.flight_reference is None
        assert revised.booking_readiness == 'unverified'
        assert result['execution_status'] == 'COMPLETED'
        assert result['optimization_review'].approved
    asyncio.run(run())


def test_interactive_accommodation_downgrade_requires_explicit_permission():
    from decimal import Decimal
    from app.services.cost_estimation_service import CostEstimationService
    from app.services.budget_revision_service import propose_revision
    from test_budget_revisions import reference_data
    ctx = context(optional=False)
    ctx.request.travel_style = ctx.normalized_requirements.travel_style = 'balanced'
    ctx.request.constraints = ctx.normalized_requirements.constraints = {'budget_assumptions': {'food': {'amount': '6000'}, 'activities': {'amount': '2000'}}}
    agent, estimator = build_budget(automatic=True, data=reference_data())
    # Use the same valid sourced reference fixture as the existing revision tests.
    ctx.accommodation_transport_plan['accommodation_cost']['amount'] = None
    ctx.accommodation_transport_plan['accommodation']['estimated_total_cost']['amount'] = None
    ctx.budget_breakdown = asyncio.run(agent.run(ctx)).output.model_dump(mode='json')
    ctx.normalized_requirements.total_budget = ctx.request.total_budget = Decimal('1000')
    ctx.budget_breakdown['total_budget'] = '1000'
    total = Decimal(ctx.budget_breakdown['estimated_total'])
    ctx.budget_breakdown['remaining_budget'] = str(Decimal('1000') - total)
    ctx.budget_breakdown['over_budget_amount'] = str(total - Decimal('1000'))
    ctx.budget_breakdown['status'] = 'OVER_BUDGET'
    ctx.budget_revision = {'interactive': True, 'allow_economy_accommodation': False}
    assert asyncio.run(propose_revision(ctx, estimator)) is None
    ctx.budget_revision['allow_economy_accommodation'] = True
    directive = asyncio.run(propose_revision(ctx, estimator))
    assert directive and 'accommodation' in directive['replacement_items']


def test_background_mode_reuses_the_revision_snapshot(setup_revision):
    setup, _, config = setup_revision
    service, _, ident, _, _ = setup()
    config.TRIP_EXECUTION_MODE = 'background'
    with client_for(service, config) as client:
        response = client.post(f'/api/v1/trips/{ident}/revise', json={}, headers=HEADERS)
        assert response.status_code == 202
        final = client.get(f'/api/v1/trips/{ident}').json()
    assert final['status'] == 'completed'
    assert final['budget_revision_summary']['history'][0]['savings'] == '11000.00'


def test_final_accommodation_plan_is_persisted_without_rewriting_agent_history(setup_revision):
    setup, factory, _ = setup_revision
    service, queue, ident, _, _ = setup(reference=True)
    asyncio.run(service.revise_trip(ident, allow_economy_accommodation=True))
    asyncio.run(process_claim(queue, queue.claim(), service))
    final = asyncio.run(service.get_trip(ident))
    accommodation = next(row for row in final.budget['line_items'] if row['category'] == 'accommodation')
    assert final.accommodation_transport['accommodation']['recommended_types'] == ['budget_hotel']
    assert final.accommodation_transport['accommodation_cost']['amount'] == accommodation['amount']
    with factory() as session:
        original = session.scalars(select(AgentRun).where(AgentRun.trip_id == ident, AgentRun.agent_name == 'accommodation_transport')).first()
        assert original.output_data['result']['accommodation_cost']['amount'] is None


def test_budget_commentary_cannot_call_a_complete_estimate_incomplete():
    from app.agents.budget_agent import BudgetAgent
    from app.schemas.budget import BudgetNarrative
    class Commentary:
        async def generate_structured(self, *args, **kwargs):
            return BudgetNarrative(summary='The total cost is incomplete and requires additional pricing.')
    config = Settings(_env_file=None, GEMINI_API_KEY=None, AUTOMATIC_COST_ESTIMATION_ENABLED=False)
    result = asyncio.run(BudgetAgent(settings=config, gemini_service=Commentary()).run(context(reserve=True)))
    assert result.output.pricing_status == 'COMPLETE_ESTIMATE'
    assert result.output.qualitative_summary is None
    assert any('completeness' in warning for warning in result.warnings)


def test_recovered_revision_worker_attempt_limit_records_failure(setup_revision):
    from datetime import datetime, timedelta, timezone
    setup, factory, config = setup_revision
    service, queue, ident, _, _ = setup()
    asyncio.run(service.revise_trip(ident))
    queue.claim()
    config.TRIP_JOB_MAX_ATTEMPTS = 1
    with factory() as session, session.begin():
        job = job_for(session, ident)
        job.output_data = {**job.output_data, 'lease_expires_at': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    assert queue.claim() is None
    final = asyncio.run(service.get_trip(ident))
    assert final.status == 'failed'
    assert final.budget_revision_summary.history[-1].outcome == 'failed'
    assert final.budget_revision_summary.can_retry
    asyncio.run(service.revise_trip(ident))
    assert asyncio.run(service.get_trip_status(ident))['status'] == 'queued'


def test_partial_savings_exhaustion_blocks_repeated_revision_and_preserves_budget(setup_revision):
    setup, _, _ = setup_revision
    service, queue, ident, _, agents = setup(budget='15000', limit=5)
    before = asyncio.run(service.get_trip(ident))
    asyncio.run(service.revise_trip(ident))
    asyncio.run(process_claim(queue, queue.claim(), service))
    final = asyncio.run(service.get_trip(ident))
    record = final.budget_revision_summary.history[-1]
    assert final.status == 'revision_required'
    assert final.budget['total_budget'] == '15000'
    assert record.original_total == 38500 and record.revised_total == 27500
    assert record.savings == record.cumulative_savings == 11000
    assert record.remaining_over_budget == 12500
    assert record.outcome_message == 'Best supported plan found — still over budget.'
    assert record.alternatives_exhausted
    assert not final.budget_revision_summary.can_retry
    assert not final.optimization_review.approved
    assert before.request == final.request
    for permission in (False, True):
        with pytest.raises(RevisionConflict):
            asyncio.run(service.revise_trip(ident, allow_economy_accommodation=permission))
    assert agents.budget.run.call_count == 2


def test_only_meaningful_new_accommodation_permission_unlocks_an_exhausted_search(setup_revision):
    setup, _, _ = setup_revision
    service, queue, ident, _, _ = setup(optional=False, reference=True, limit=5)
    asyncio.run(service.revise_trip(ident))
    asyncio.run(process_claim(queue, queue.claim(), service))
    first = asyncio.run(service.get_trip(ident))
    assert first.budget_revision_summary.requires_economy_permission
    assert first.budget_revision_summary.economy_accommodation_available
    with pytest.raises(RevisionConflict, match='identical'):
        asyncio.run(service.revise_trip(ident))
    asyncio.run(service.revise_trip(ident, allow_economy_accommodation=True))
    asyncio.run(process_claim(queue, queue.claim(), service))
    second = asyncio.run(service.get_trip(ident))
    assert second.total_estimated_cost < first.total_estimated_cost
    assert second.budget_revision_summary.history[-1].alternatives_exhausted
    assert not second.budget_revision_summary.economy_accommodation_available
    assert not second.budget_revision_summary.can_retry
    with pytest.raises(RevisionConflict):
        asyncio.run(service.revise_trip(ident, allow_economy_accommodation=True))


def test_success_outcome_uses_existing_decimal_totals_and_no_extra_approval(setup_revision):
    setup, _, _ = setup_revision
    service, queue, ident, _, _ = setup()
    asyncio.run(service.revise_trip(ident))
    asyncio.run(process_claim(queue, queue.claim(), service))
    final = asyncio.run(service.get_trip(ident))
    record = final.budget_revision_summary.history[-1]
    assert record.outcome_message == 'Budget optimized successfully.'
    assert record.original_budget == 27500
    assert record.cumulative_savings == 11000
    assert record.remaining_over_budget == 0
    assert final.budget['booking_readiness'] == 'unverified'


def test_infeasible_with_current_evidence_is_not_claimed_globally_impossible(setup_revision):
    setup, _, _ = setup_revision
    service, queue, ident, _, _ = setup(optional=False, budget='1')
    asyncio.run(service.revise_trip(ident))
    asyncio.run(process_claim(queue, queue.claim(), service))
    final = asyncio.run(service.get_trip(ident))
    record = final.budget_revision_summary.history[-1]
    assert record.outcome_message == 'No further compatible savings available with current pricing information.'
    assert record.savings == record.cumulative_savings == 0
    assert record.remaining_over_budget > 0
    assert not final.optimization_review.approved
    assert final.budget['pricing_status'] == 'COMPLETE_ESTIMATE'
    assert not final.budget_revision_summary.can_retry


def test_exhausted_price_fingerprint_ignores_commentary_but_recognizes_new_pricing(setup_revision):
    import copy
    from app.services.interactive_budget_revision import exhausted_inputs
    setup, factory, _ = setup_revision
    service, queue, ident, _, _ = setup(optional=False)
    asyncio.run(service.revise_trip(ident))
    asyncio.run(process_claim(queue, queue.claim(), service))
    with factory() as session:
        metadata = dict(job_for(session, ident).output_data)
    snapshot = copy.deepcopy(metadata['revision_result_snapshot'])
    assert exhausted_inputs(metadata, snapshot, False)
    snapshot['budget_breakdown']['qualitative_summary'] = 'Different commentary does not provide a new price.'
    assert exhausted_inputs(metadata, snapshot, False)
    snapshot['budget_breakdown']['line_items'][0]['amount'] = '9999.00'
    assert not exhausted_inputs(metadata, snapshot, False)


def test_incomplete_revision_is_distinct_from_over_budget_and_never_supplies_zero(setup_revision):
    from types import SimpleNamespace
    from app.services.interactive_budget_revision import finish_revision
    setup, _, _ = setup_revision
    service, _, ident, state, _ = setup()
    asyncio.run(service.revise_trip(ident))
    metadata = service._revision_metadata(ident)
    budget = state['budget_breakdown'].model_copy(deep=True)
    budget.estimated_total = None
    budget.pricing_status = 'INCOMPLETE'
    budget.unpriced_components = ['intercity_transport']
    job = SimpleNamespace(output_data=metadata)
    finish_revision(job, {'execution_status': 'INSUFFICIENT_DATA', 'budget_breakdown': budget})
    record = job.output_data['revision_history'][-1]
    assert record['outcome'] == 'incomplete'
    assert record['outcome_message'] == 'Pricing incomplete — a final budget is unavailable.'
    assert record['revised_total'] is record['savings'] is record['cumulative_savings'] is None
    assert record['remaining_over_budget'] is None


def test_attempt_limit_still_bounds_partial_savings(setup_revision):
    setup, _, _ = setup_revision
    service, queue, ident, _, _ = setup(budget='15000', limit=1)
    asyncio.run(service.revise_trip(ident))
    asyncio.run(process_claim(queue, queue.claim(), service))
    with pytest.raises(RevisionConflict, match='limit'):
        asyncio.run(service.revise_trip(ident))


def test_final_noop_retains_genuine_savings_from_previous_attempts(setup_revision):
    from types import SimpleNamespace
    from app.schemas.budget import BudgetBreakdown
    from app.services.interactive_budget_revision import finish_revision
    setup, factory, _ = setup_revision
    service, queue, ident, _, _ = setup(budget='15000')
    asyncio.run(service.revise_trip(ident))
    asyncio.run(process_claim(queue, queue.claim(), service))
    with factory() as session:
        metadata = dict(job_for(session, ident).output_data)
    metadata.update(revision_pending=True, interactive_attempts=2,
                    revision_snapshot=metadata['revision_result_snapshot'])
    budget = BudgetBreakdown.model_validate(metadata['revision_result_snapshot']['budget_breakdown'])
    job = SimpleNamespace(output_data=metadata)
    finish_revision(job, {'execution_status': 'REVISION_REQUIRED', 'current_stage': 'budget_revision',
                         'budget_breakdown': budget, 'warnings': ['No further supported cost reduction is available.']})
    record = job.output_data['revision_history'][-1]
    assert record['outcome'] == 'no_supported_alternative'
    assert record['savings'] == '0.00'
    assert record['cumulative_savings'] == '11000.00'
    assert record['original_total'] == record['revised_total'] == '27500.00'
    assert record['alternatives_exhausted']


def test_outcome_cannot_claim_success_by_changing_original_budget(setup_revision):
    from types import SimpleNamespace
    from decimal import Decimal
    from app.services.interactive_budget_revision import finish_revision
    setup, _, _ = setup_revision
    service, _, ident, state, _ = setup()
    asyncio.run(service.revise_trip(ident))
    job = SimpleNamespace(output_data=service._revision_metadata(ident))
    budget = state['budget_breakdown'].model_copy(deep=True)
    budget.total_budget = Decimal('50000')
    finish_revision(job, {'execution_status': 'COMPLETED', 'budget_breakdown': budget})
    record = job.output_data['revision_history'][-1]
    assert record['original_budget'] == '27500'
    assert record['outcome'] == 'incomplete'
    assert record['revised_total'] is None
    assert record['outcome_message'] != 'Budget optimized successfully.'
