"""Run with python -m app.worker from backend; also used by the API lifespan.

One job per worker, bounded provider calls, short database transactions. Multiple
PostgreSQL workers coordinate using SKIP LOCKED and fenced, expiring leases.
"""
import asyncio
from time import perf_counter

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError

from .core.config import get_settings
from .core.diagnostics import configure_diagnostics, diagnostic_context, record_event
from .database.session import get_session_factory
from .services.task_status_service import TaskStatusService
from .services.trip_job_service import TripJobService
from .services.trip_planning_service import TripPlanningService


async def process_claim(queue, claim, service):
    with diagnostic_context(trip_id=claim.trip_id):
        record_event("worker", "job.claim", "success", perf_counter())
        work = asyncio.create_task(service.run_trip(claim.trip_id, claim.request, lease_token=claim.token))

        async def heartbeat():
            while not work.done():
                await asyncio.sleep(queue.settings.TRIP_JOB_LEASE_SECONDS / 3)
                if work.done():
                    return
                if not await run_in_threadpool(queue.renew, claim):
                    record_event("worker", "job.renew", "failed", perf_counter(), category="ownership_lost")
                    work.cancel()
                    return

        beat = asyncio.create_task(heartbeat())
        try:
            # Heartbeat errors must cancel processing; never write without a lease.
            done, _ = await asyncio.wait([work, beat], return_when=asyncio.FIRST_COMPLETED)
            if beat in done:
                try:
                    await beat
                finally:
                    if not work.done():
                        work.cancel()
            await work
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            # Losing ownership cancels this attempt, not the worker process.
            record_event("worker", "job.process", "failed", perf_counter(), category="ownership_lost")
        finally:
            work.cancel()
            beat.cancel()
            await asyncio.gather(work, beat, return_exceptions=True)
            # No-op after terminal persistence or when a new worker owns the job.
            try:
                await run_in_threadpool(queue.release, claim)
            except SQLAlchemyError:
                record_event("worker", "job.release", "failed", perf_counter(), category="storage_unavailable")
            try:
                await service.aclose()
            except Exception as exc:
                record_event("worker", "resources.close", "failed", perf_counter(), category=type(exc).__name__)


async def run_worker(settings=None, *, session_factory=None, service_factory=None):
    settings = settings or get_settings()
    factory = session_factory or get_session_factory()
    queue = TripJobService(factory, settings)
    while True:
        try:
            claim = await run_in_threadpool(queue.claim)
            if claim is not None:
                service = (service_factory() if service_factory else TripPlanningService(
                    factory, TaskStatusService(settings=settings), settings=settings))
                await process_claim(queue, claim, service)
                continue
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, ConnectionError, TimeoutError):
            record_event("worker", "job.process", "failed", perf_counter(), category="storage_unavailable")
        except Exception as exc:
            # Process boundary only; category is visible, body may contain secrets.
            record_event("worker", "job.process", "failed", perf_counter(), category=type(exc).__name__)
        await asyncio.sleep(settings.TRIP_WORKER_POLL_SECONDS)


if __name__ == "__main__":
    configure_diagnostics()
    asyncio.run(run_worker())
