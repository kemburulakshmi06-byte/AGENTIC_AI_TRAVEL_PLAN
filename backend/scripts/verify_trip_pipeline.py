"""Opt-in API -> real agents -> isolated SQL -> fresh-process retrieval proof.

No production PostgreSQL/Redis writes. Requires an explicit request JSON and
stores test trips in a new local SQLite file under .pytest-tmp. Production uses
PostgreSQL; this harness does not verify PostgreSQL SKIP LOCKED concurrency.
Provider calls consume quota. No provider prices or geographic results are mocked.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import UUID

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine, event, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from app.core.config import Settings
from app.database.base import Base
from app.database.models import AgentRun
from app.main import create_app
from app.schemas.trip_api import TripPlanRequest
from app.services.cache_service import CACHE_MISS_VALUE
from app.services.trip_job_service import TripJobService
from app.services.trip_planning_service import TripPlanningService
from app.workflows.travel_graph import TravelAgentSet, plan_trip
from app.worker import process_claim


class NoStorage:
    async def get_cached(self, *args, **kwargs): return CACHE_MISS_VALUE
    async def set_cached(self, *args, **kwargs): return False
    async def create_task_status(self, *args, **kwargs): pass
    async def update_task_status(self, *args, **kwargs): pass
    async def get_task_status(self, *args, **kwargs): return None
    async def aclose(self): pass


def database(path, create):
    engine = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread':False})
    @event.listens_for(engine,'connect')
    def foreign_keys(connection, _record): connection.execute('PRAGMA foreign_keys=ON')
    if create:
        metadata=MetaData()
        for table in Base.metadata.sorted_tables:
            copied=table.to_metadata(metadata)
            for column in copied.c:
                if isinstance(column.type,JSONB):
                    column.type=JSON()
                    column.server_default=None
        metadata.create_all(engine)
    return engine,sessionmaker(engine,expire_on_commit=False)


def workflow(settings):
    async def run(request, **kwargs):
        from app.agents.requirements_agent import RequirementsAgent
        from app.agents.destination_research_agent import DestinationResearchAgent
        from app.agents.accommodation_transport_agent import AccommodationTransportAgent
        from app.agents.budget_agent import BudgetAgent
        from app.agents.itinerary_agent import ItineraryAgent
        from app.agents.optimization_agent import OptimizationAgent
        from app.tools.geoapify import GeoapifyTool
        from app.tools.openrouteservice import OpenRouteServiceTool
        from app.tools.open_meteo import OpenMeteoTool
        from app.tools.tavily import TavilyTool
        from app.services.cost_estimation_service import CostEstimationService,ReferenceFXTool
        from app.tools.travel_pricing import AmadeusPricingTool
        cache=NoStorage()
        geo=GeoapifyTool(settings,cache=cache)
        route=OpenRouteServiceTool(settings,cache=cache)
        agents=TravelAgentSet(
            requirements=RequirementsAgent(settings=settings),
            destination_research=DestinationResearchAgent(settings=settings,geoapify=geo,routing=route,
                weather=OpenMeteoTool(settings,cache=cache),tavily=TavilyTool(settings,cache=cache)),
            accommodation_transport=AccommodationTransportAgent(settings=settings,geoapify=geo,routing=route),
            budget=BudgetAgent(settings=settings,cost_estimation_service=CostEstimationService(settings,
                fx=ReferenceFXTool(settings,cache=cache),pricing=AmadeusPricingTool(settings,cache=cache))),
            itinerary=ItineraryAgent(settings=settings,routing=route),
            optimization_review=OptimizationAgent(settings=settings))
        return await plan_trip(request,agents=agents,**kwargs)
    return run


def main(args):
    root=BACKEND.parent/'.pytest-tmp'
    path=Path(args.storage_path).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix!='.sqlite':
        raise ValueError('Verification storage must be a .sqlite file under project .pytest-tmp.')
    retrieving=bool(args.retrieve_trip_id)
    resuming=args.resume_queued
    creating=not retrieving and not resuming
    if creating and (not args.request_json or path.exists()):
        raise ValueError('Supply an explicit request and a new isolated storage file.')
    if (retrieving or resuming) and not path.is_file():
        raise ValueError('Verification storage does not exist.')
    path.parent.mkdir(parents=True,exist_ok=True)
    settings=Settings().model_copy(update={'DATABASE_URL':None,'TEST_DATABASE_URL':None,
        'UPSTASH_REDIS_REST_URL':None,'UPSTASH_REDIS_REST_TOKEN':None,'TRIP_WORKER_ENABLED':False,
        'TRIP_EXECUTION_MODE':'database_worker'})
    engine,factory=database(path,creating)
    service_factory=lambda:TripPlanningService(factory,NoStorage(),workflow=workflow(settings),settings=settings)
    app=create_app(settings)
    app.state.trip_planning_service_factory=service_factory
    try:
        with TestClient(app) as client:
            if retrieving:
                ident=args.retrieve_trip_id
            elif resuming:
                queue=TripJobService(factory,settings)
                claim=queue.claim()
                if claim is None:
                    raise RuntimeError('No recoverable job; its previous lease may still be active.')
                ident=str(claim.trip_id)
                print(json.dumps({'operation':'worker.resume','trip_id':ident}),flush=True)
                asyncio.run(process_claim(queue,claim,service_factory()))
            else:
                parsed=TripPlanRequest.model_validate_json(Path(args.request_json).read_text(encoding='utf-8'))
                submitted=client.post('/api/v1/trips/plan',json=parsed.model_dump(mode='json'))
                if submitted.status_code!=202:
                    raise RuntimeError('Isolated trip submission failed.')
                ident=submitted.json()['trip_id']
                print(json.dumps({'operation':'POST','http':202,'trip_id':ident,'status':submitted.json()['status']}),flush=True)
                queue=TripJobService(factory,settings)
                claim=queue.claim()
                if args.claim_only:
                    # Intentionally exit with an active lease: simulates a worker
                    # process disappearing before it releases/completes its job.
                    print(json.dumps({'operation':'worker.exit_before_execution','trip_id':ident,
                        'lease_seconds':settings.TRIP_JOB_LEASE_SECONDS}),flush=True)
                    return
                print(json.dumps({'operation':'status.before','response':client.get(f'/api/v1/trips/{ident}/status').json()}),flush=True)
                asyncio.run(process_claim(queue,claim,service_factory()))
            response=client.get(f'/api/v1/trips/{ident}')
            status=client.get(f'/api/v1/trips/{ident}/status')
            if response.status_code!=200 or status.status_code!=200:
                raise RuntimeError('Isolated result retrieval failed.')
            details=response.json()
            with factory() as session:
                runs=session.scalars(select(AgentRun).where(AgentRun.trip_id==UUID(details['id']))).all()
                stages=[{'agent':r.agent_name,'status':r.status,'output_present':bool(r.output_data),
                    'started_at':str(r.started_at),'completed_at':str(r.completed_at)} for r in runs]
            budget=details.get('budget') or {}
            proof={'storage':'isolated SQLite, no production writes','fresh_process_retrieval':retrieving,
                'api_response':details,
                'trip_id':ident,'http':response.status_code,'status':details['status'],
                'request':details['request'],'days':len(details['days']),
                'accommodation_transport_present':bool(details.get('accommodation_transport')),
                'budget':budget,'errors':details['errors'],'warnings':details['warnings'],'agents':stages,
                'status_response':status.json()}
            output=path.with_suffix('.proof.json') if not retrieving else path.with_suffix('.restart-proof.json')
            output.write_text(json.dumps(proof,indent=2,ensure_ascii=True),encoding='utf-8')
            print(json.dumps({'operation':'GET.final','trip_id':ident,'http':200,'status':details['status'],
                'days':proof['days'],'known_subtotal':budget.get('known_cost_subtotal'),
                'estimated_total':budget.get('estimated_total'),'confidence':budget.get('estimate_confidence'),
                'missing':budget.get('missing_price_actions'),'proof_path':str(output)}),flush=True)
    finally:
        engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage-path',required=True)
    parser.add_argument('--request-json')
    parser.add_argument('--retrieve-trip-id')
    parser.add_argument('--claim-only',action='store_true')
    parser.add_argument('--resume-queued',action='store_true')
    main(parser.parse_args())
