"""Real SQL persistence/worker/API tests in isolated SQLite storage.

SQLite validates serialization, restart recovery and fencing, not PostgreSQL's
row-lock concurrency. The separate PostgreSQL tests require TEST_DATABASE_URL.
No production database or real price provider is used by these tests.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.database.base import Base
from app.database.models import AgentRun, Trip
from app.main import create_app
from app.schemas.agent_schemas import TravelRequest
from app.services.trip_job_service import JOB_AGENT, TripJobService, job_for
from app.services.trip_planning_service import TripPlanningService
from app.worker import process_claim
from test_full_trip_request_flow import ALLOWANCES, isolated_service, payload


def sqlite_factory(path):
    engine = create_engine('sqlite:///' + str(path), connect_args={"check_same_thread": False})
    @event.listens_for(engine, 'connect')
    def foreign_keys(connection, _record):
        connection.execute('PRAGMA foreign_keys=ON')
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copy = table.to_metadata(metadata)
        for column in copy.c:
            if isinstance(column.type, JSONB):
                column.type = JSON()
                column.server_default = None  # PostgreSQL ::jsonb defaults only.
    metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False), engine


def service_for(factory, settings, trip_payload=None, **options):
    seeded, _ = isolated_service(trip_payload or payload(destination='Bengaluru', cost_preferences=ALLOWANCES), settings_override=settings, **options)
    cache = MagicMock()
    cache.create_task_status = AsyncMock()
    cache.update_task_status = AsyncMock()
    cache.get_task_status = AsyncMock(return_value=None)
    cache.aclose = AsyncMock()
    return TripPlanningService(factory, cache, workflow=seeded._workflow, settings=settings)


@pytest.fixture
def database(tmp_path):
    factory, engine = sqlite_factory(tmp_path / 'trips.sqlite')
    yield factory, engine
    engine.dispose()


def settings():
    return Settings(_env_file=None, TRIP_WORKER_ENABLED=False, AUTOMATIC_COST_ESTIMATION_ENABLED=False)


def test_post_worker_persistence_and_fresh_backend_retrieval(database):
    factory, _ = database
    config = settings()
    trip_payload = payload(destination='Bengaluru', cost_preferences=ALLOWANCES, user_message='Culture and food')
    app = create_app(config)
    app.state.trip_planning_service_factory = lambda: service_for(factory, config, trip_payload)
    with TestClient(app) as client:
        response = client.post('/api/v1/trips/plan', json=trip_payload)
        assert response.status_code == 202
        ident = response.json()['trip_id']
        assert client.get(f'/api/v1/trips/{ident}/status').json()['status'] == 'queued'
    # API process has closed. A newly constructed worker loads the committed job.
    queue = TripJobService(factory, config)
    claim = queue.claim()
    assert str(claim.trip_id) == ident
    assert claim.request.user_message == 'Culture and food'
    assert claim.request.cost_preferences.room_count == 1
    asyncio.run(process_claim(queue, claim, service_for(factory, config, trip_payload)))
    # New app and fresh sessions: there is no in-memory result to recover from.
    restarted = create_app(config)
    restarted.state.trip_planning_service_factory = lambda: service_for(factory, config, trip_payload)
    with TestClient(restarted) as client:
        status = client.get(f'/api/v1/trips/{ident}/status').json()
        details = client.get(f'/api/v1/trips/{ident}').json()
    assert status['status'] == details['status'] == 'completed'
    assert details['total_estimated_cost'] == '16500.00'
    assert details['budget']['known_cost_subtotal'] == '16500.00'
    assert details['budget']['remaining_budget'] == '3500.00'
    assert len(details['days']) == len(details['itinerary']['days']) == 5
    assert details['accommodation_transport']['accommodation']['nights'] == 4
    assert details['normalized_requirements']['travelers_count'] == 2
    assert details['destination_research']['resolution_status'] == 'verified'
    with factory() as session:
        runs = session.scalars(select(AgentRun).where(AgentRun.trip_id == claim.trip_id)).all()
        assert {r.agent_name for r in runs} == {JOB_AGENT, 'requirements', 'destination_research', 'accommodation_transport', 'budget', 'itinerary', 'optimization_review', 'workflow'}
        assert all(r.output_data['result'] for r in runs if r.agent_name not in {JOB_AGENT, 'workflow'})
    assert queue.claim() is None


def test_interrupted_claim_recovers_and_stale_worker_cannot_write(database):
    factory, _ = database
    config = settings()
    service = service_for(factory, config)
    request = TravelRequest.model_validate(payload(destination='Bengaluru', cost_preferences=ALLOWANCES))
    ident = asyncio.run(service.create_trip(request))
    queue = TripJobService(factory, config)
    first = queue.claim()
    assert first and queue.claim() is None
    with factory() as session, session.begin():
        job = job_for(session, ident)
        job.output_data = {**job.output_data, 'lease_expires_at': (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}
    recovered = TripJobService(factory, config).claim()
    assert recovered.token != first.token
    assert not queue.renew(first) and not queue.release(first)
    assert not service._persist_result(ident, {'execution_status':'FAILED'}, first.token)
    assert not service._persist_failure(ident, 'failed', 'stale worker', first.token)
    asyncio.run(process_claim(queue, recovered, service_for(factory, config)))
    assert asyncio.run(service.get_trip(ident)).status == 'completed'


def test_graceful_worker_shutdown_requeues_request(database):
    factory, _ = database
    config = settings()
    service = service_for(factory, config)
    asyncio.run(service.create_trip(TravelRequest.model_validate(payload())))
    queue = TripJobService(factory, config)
    claim = queue.claim()
    entered = asyncio.Event()
    async def slow(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    service._workflow = slow
    async def cancel():
        task = asyncio.create_task(process_claim(queue, claim, service))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert service._get_status_source(claim.trip_id)['status'] == 'queued'
    assert queue.claim().request.origin == claim.request.origin


def test_recovery_attempt_limit_and_invalid_payload_are_explicit(database):
    factory, _ = database
    config = settings().model_copy(update={'TRIP_JOB_MAX_ATTEMPTS':1})
    service = service_for(factory, config)
    ident = asyncio.run(service.create_trip(TravelRequest.model_validate(payload())))
    queue = TripJobService(factory, config)
    queue.release(queue.claim())
    assert queue.claim() is None
    assert 'retry limit reached' in service._get_status_source(ident)['error']
    second = asyncio.run(service.create_trip(TravelRequest.model_validate(payload())))
    with factory() as session, session.begin():
        job_for(session, second).input_data = {'travelers_count':0}
    assert queue.claim() is None
    assert 'schema validation' in service._get_status_source(second)['error']


def test_failed_agent_remains_failed_and_partial_outputs_survive(database):
    factory, _ = database
    config = settings()
    service = service_for(factory, config, worker_failure=True)
    ident = asyncio.run(service.create_trip(TravelRequest.model_validate(payload())))
    queue = TripJobService(factory, config)
    asyncio.run(process_claim(queue, queue.claim(), service))
    result = asyncio.run(service.get_trip(ident))
    assert result.status == 'failed' and result.errors
    assert result.budget is None and result.days == []
    assert 'PRIVATE' not in result.model_dump_json()


@pytest.mark.parametrize('destination', ['Bengaluru','Goa, India','Dubai, UAE'])
def test_no_amadeus_does_not_disable_supported_independent_reference_estimates(database, monkeypatch, destination):
    import app.services.cost_estimation_service as module
    class Today(date):
        @classmethod
        def today(cls): return cls(2026,10,12)
    monkeypatch.setattr(module, 'date', Today)
    factory, _ = database
    config = settings().model_copy(update={'AUTOMATIC_COST_ESTIMATION_ENABLED':True})
    trip_payload = payload(destination=destination, currency='AED' if 'Dubai' in destination else 'INR')
    service = service_for(factory, config, trip_payload)
    ident = asyncio.run(service.create_trip(TravelRequest.model_validate(trip_payload)))
    queue = TripJobService(factory, config)
    asyncio.run(process_claim(queue, queue.claim(), service))
    result = asyncio.run(service.get_trip(ident))
    items = {item['category']:item for item in result.budget['line_items']}
    for category in ('accommodation','local_transport','food','activities'):
        assert Decimal(items[category]['amount']) > 0
        assert items[category]['source'] == 'reference_data'
        assert items[category]['confidence'] == 'low'
        assert items[category]['minimum_amount'] is None
    # The returned budget is the final economy revision, not the initial
    # domestic reference subtotal (40058). Flights and other provenance stay intact.
    supported_base = Decimal('9698') if 'Dubai' in destination else Decimal('24254')
    if destination == 'Bengaluru':
        assert Decimal(items['intercity_transport']['amount']) == Decimal('30974.00')
        assert items['intercity_transport']['source'] == 'reference_data'
        supported_base += Decimal(items['intercity_transport']['amount'])
    else:
        assert items['intercity_transport']['amount'] is None
    expected_contingency = (supported_base * Decimal('0.10')).quantize(Decimal('0.01'))
    assert Decimal(items['contingency']['amount']) == expected_contingency
    assert items['contingency']['source'] == 'configured_assumption'
    if destination == 'Bengaluru':
        assert Decimal(result.total_estimated_cost) == supported_base + expected_contingency
        assert result.budget['pricing_status'] == 'COMPLETE_ESTIMATE'
    else:
        assert result.total_estimated_cost is None
        assert result.budget['pricing_status'] == 'INCOMPLETE'
    assert result.budget['estimate_confidence'] == 'low'
    assert len(result.days) == 5
    assert result.status in {'insufficient_data','revision_required'}
    assert Decimal(result.budget['known_cost_subtotal']) == supported_base + expected_contingency


def test_dated_hyderabad_dubai_reference_persists_all_six_agent_outputs(
    database, monkeypatch,
):
    import app.services.cost_estimation_service as module

    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 12)

    monkeypatch.setattr(module, 'date', Today)
    factory, _ = database
    config = settings().model_copy(update={'AUTOMATIC_COST_ESTIMATION_ENABLED': True})
    trip_payload = payload(
        destination='Dubai, UAE',
        start_date='2026-10-20',
        end_date='2026-10-24',
        total_budget='150000',
        currency='INR',
    )
    service = service_for(factory, config, trip_payload)
    ident = asyncio.run(service.create_trip(TravelRequest.model_validate(trip_payload)))
    queue = TripJobService(factory, config)
    asyncio.run(process_claim(queue, queue.claim(), service))
    result = asyncio.run(service.get_trip(ident))

    items = {item['category']: item for item in result.budget['line_items']}
    assert Decimal(items['intercity_transport']['amount']) == Decimal('62241.66')
    assert items['intercity_transport']['source'] == 'reference_data'
    assert items['intercity_transport']['evidence_kind'] == 'planning_estimate'
    assert result.total_estimated_cost is not None
    assert result.budget['pricing_status'] == 'COMPLETE_ESTIMATE'
    assert len(result.days) == len(result.itinerary['days']) == 5
    with factory() as session:
        runs = session.scalars(select(AgentRun).where(AgentRun.trip_id == ident)).all()
    assert {run.agent_name for run in runs} == {
        JOB_AGENT,
        'requirements',
        'destination_research',
        'accommodation_transport',
        'budget',
        'itinerary',
        'optimization_review',
        'workflow',
    }


def test_sequential_requests_keep_separate_results(database):
    factory, _ = database
    config = settings()
    ids = []
    for count in (1,2,3):
        trip_payload = payload(destination='Bengaluru', travelers_count=count,
            cost_preferences={**ALLOWANCES,'room_count':count})
        service = service_for(factory, config, trip_payload)
        ids.append(asyncio.run(service.create_trip(TravelRequest.model_validate(trip_payload))))
    queue = TripJobService(factory, config)
    while (claim := queue.claim()) is not None:
        asyncio.run(process_claim(queue, claim, service_for(factory, config, claim.request.model_dump(mode='json'))))
    results = [asyncio.run(service_for(factory, config).get_trip(ident)) for ident in ids]
    assert len(set(ids)) == 3
    assert [result.request['travelers_count'] for result in results] == [1,2,3]
    assert len({result.total_estimated_cost for result in results}) == 3
    assert all(len(result.days)==5 for result in results)


def test_completed_stage_outputs_survive_graph_failure(monkeypatch):
    import app.workflows.travel_graph as module
    from test_travel_graph import agent_set, request, requirements
    class InterruptedGraph:
        async def astream(self, initial, **kwargs):
            yield {**initial, 'current_stage':'requirements', 'normalized_requirements':requirements()}
            raise RuntimeError('PRIVATE provider details')
    monkeypatch.setattr(module, 'build_travel_graph', lambda *args, **kwargs: InterruptedGraph())
    result = asyncio.run(module.plan_trip(request(), agents=agent_set()))
    assert result['execution_status'] == 'FAILED'
    assert result['normalized_requirements'].destination == requirements().destination
    assert 'PRIVATE' not in str(result['errors'])


def test_checkpoint_budget_and_itinerary_survive_next_agent_exception(database):
    factory, _ = database
    config = settings()
    service = service_for(factory, config)
    normal = service._workflow
    async def interrupted(request, **kwargs):
        checkpoint = kwargs.pop('on_state')
        state = await normal(request, **kwargs)
        await checkpoint(state)
        raise RuntimeError('PRIVATE unexpected post-stage error')
    service._workflow = interrupted
    ident = asyncio.run(service.create_trip(TravelRequest.model_validate(payload(destination='Bengaluru',cost_preferences=ALLOWANCES))))
    queue = TripJobService(factory, config)
    asyncio.run(process_claim(queue, queue.claim(), service))
    details = asyncio.run(service.get_trip(ident))
    assert details.status == 'failed'
    assert details.budget['estimated_total'] == '16500.00'
    assert len(details.days) == 5
    assert details.errors and 'PRIVATE' not in str(details.errors)


def test_lost_lease_cancels_attempt_without_stopping_worker():
    from types import SimpleNamespace
    from unittest.mock import Mock
    queue = SimpleNamespace(settings=SimpleNamespace(TRIP_JOB_LEASE_SECONDS=.01),
        renew=Mock(return_value=False),release=Mock(return_value=False))
    from uuid import uuid4
    claim=SimpleNamespace(trip_id=uuid4(),token='old',request=TravelRequest())
    async def waiting(*args,**kwargs):
        await asyncio.Event().wait()
    service=SimpleNamespace(run_trip=waiting,aclose=AsyncMock())
    asyncio.run(process_claim(queue,claim,service))
    queue.renew.assert_called_once_with(claim)
    service.aclose.assert_awaited_once()


def test_concurrent_post_requests_persist_distinct_jobs(database):
    from concurrent.futures import ThreadPoolExecutor
    factory,_=database
    config=settings()
    app=create_app(config)
    app.state.trip_planning_service_factory=lambda:service_for(factory,config)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=3) as pool:
        responses=list(pool.map(lambda count:client.post('/api/v1/trips/plan',
            json=payload(destination='Bengaluru',travelers_count=count,cost_preferences=ALLOWANCES)),(1,2,3)))
    assert all(response.status_code==202 for response in responses)
    assert len({response.json()['trip_id'] for response in responses})==3
    with factory() as session:
        jobs=session.scalars(select(AgentRun).where(AgentRun.agent_name==JOB_AGENT)).all()
        assert len(jobs)==3
        assert {job.input_data['travelers_count'] for job in jobs}=={1,2,3}


def test_new_history_reference_point_estimates_do_not_invent_ranges(monkeypatch):
    import app.services.cost_estimation_service as module
    from test_automatic_cost_estimation import run_budget,req
    class Today(date):
        @classmethod
        def today(cls): return cls(2026,10,12)
    monkeypatch.setattr(module,'date',Today)
    budget=run_budget(req(destination='Bengaluru',total_budget='100000',
        cost_preferences={'intercity_round_trip_fare_per_traveler':'1000'}),
        research={'destination_name':'Bengaluru','country_code':'in','resolution_status':'verified'}).output
    assert budget.estimated_total==Decimal('46263.80')
    assert budget.remaining_budget==Decimal('53736.20')
    assert budget.minimum_estimated_total is None and budget.maximum_estimated_total is None
    assert all(item.evidence_kind!='verified_price' for item in budget.line_items)


def test_restarted_early_stage_cannot_erase_previous_valid_budget_and_days(database):
    factory,_=database
    config=settings()
    service=service_for(factory,config)
    request=TravelRequest.model_validate(payload(destination='Bengaluru',cost_preferences=ALLOWANCES))
    ident=asyncio.run(service.create_trip(request))
    queue=TripJobService(factory,config)
    first=queue.claim()
    state=asyncio.run(service._workflow(request))
    assert service._persist_result(ident,state,first.token,False)
    queue.release(first)
    second=queue.claim()
    # A fresh worker first completes requirements; later-stage state is absent.
    early={**state,'execution_status':'RUNNING','current_stage':'requirements',
        'agent_history':state['agent_history'][:1]}
    for field in ('budget_breakdown','trip_itinerary','optimization_review','destination_research','accommodation_transport_plan'):
        early.pop(field,None)
    assert service._persist_result(ident,early,second.token,False)
    saved=asyncio.run(service.get_trip(ident))
    assert saved.status=='running'
    assert saved.budget['estimated_total']=='16500.00'
    assert len(saved.days)==5 and saved.itinerary
