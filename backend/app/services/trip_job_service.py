"""Durable jobs in the existing AgentRun table; PostgreSQL is the authority.

The trip row lock serializes claims and writes. Expiring leases allow recovery
after process loss; a new token fences the previous worker out of all writes.
Provider calls may be repeated after recovery; no booking side effects occur.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select

from ..core.config import Settings
from ..database.models import AgentRun, Trip
from ..schemas.agent_schemas import TravelRequest

JOB_AGENT = "planning_job"


@dataclass(frozen=True)
class JobClaim:
    trip_id: UUID
    token: str
    request: TravelRequest


def new_job(trip_id: UUID, request: TravelRequest) -> AgentRun:
    return AgentRun(trip_id=trip_id, agent_name=JOB_AGENT, status="queued",
                    started_at=datetime.now(timezone.utc),
                    input_data=request.model_dump(mode="json"),
                    output_data={"attempts": 0, "current_stage": "queued"})


def job_for(session, trip_id):
    return session.execute(select(AgentRun).where(
        AgentRun.trip_id == trip_id, AgentRun.agent_name == JOB_AGENT
    )).scalar_one_or_none()


def owns_job(session, trip_id, token):
    job = job_for(session, trip_id)
    return job is not None and job.status == "running" and (job.output_data or {}).get("token") == token


class TripJobService:
    def __init__(self, session_factory, settings: Settings):
        self.session_factory = session_factory
        self.settings = settings

    def claim(self) -> JobClaim | None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session, session.begin():
            trip = session.execute(select(Trip).join(AgentRun, AgentRun.trip_id == Trip.id)
                .where(Trip.status.in_(["queued", "running"]), AgentRun.agent_name == JOB_AGENT,
                    or_(AgentRun.status == "queued", and_(AgentRun.status == "running",
                        AgentRun.output_data["lease_expires_at"].as_string() <= now.isoformat())))
                .order_by(Trip.created_at, Trip.id).limit(1)
                .with_for_update(of=Trip, skip_locked=True)).scalar_one_or_none()
            if trip is None:
                return None
            job = job_for(session, trip.id)
            metadata = dict(job.output_data or {})
            attempts = metadata.get("attempts", 0) + 1
            if attempts > self.settings.TRIP_JOB_MAX_ATTEMPTS:
                trip.status = job.status = "failed"
                job.error_message = "The planning worker stopped repeatedly; retry limit reached. Submit a new request after worker recovery."
                job.completed_at = now
                return None
            # Validate the persisted payload, including optional costs and notes.
            # Invalid durable input is a terminal error, never fake success.
            from pydantic import ValidationError
            try:
                request = TravelRequest.model_validate(job.input_data)
            except ValidationError:
                trip.status = job.status = "failed"
                job.error_message = "The persisted planning request failed schema validation."
                job.completed_at = now
                return None
            token = str(uuid4())
            job.status = trip.status = "running"
            job.output_data = {**metadata, "attempts": attempts, "token": token,
                "lease_expires_at": (now + timedelta(seconds=self.settings.TRIP_JOB_LEASE_SECONDS)).isoformat(),
                "current_stage": "requirements"}
            return JobClaim(trip.id, token, request)

    def renew(self, claim: JobClaim) -> bool:
        with self.session_factory() as session, session.begin():
            trip = session.execute(select(Trip).where(Trip.id == claim.trip_id).with_for_update()).scalar_one_or_none()
            if trip is None or trip.status != "running" or not owns_job(session, claim.trip_id, claim.token):
                return False
            job = job_for(session, claim.trip_id)
            job.output_data = {**job.output_data, "lease_expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=self.settings.TRIP_JOB_LEASE_SECONDS)).isoformat()}
            return True

    def release(self, claim: JobClaim) -> bool:
        """Graceful shutdown returns owned work to the durable queue."""
        with self.session_factory() as session, session.begin():
            trip = session.execute(select(Trip).where(Trip.id == claim.trip_id).with_for_update()).scalar_one_or_none()
            if trip is None or not owns_job(session, claim.trip_id, claim.token):
                return False
            job = job_for(session, claim.trip_id)
            trip.status = job.status = "queued"
            job.output_data = {**job.output_data, "token": None, "current_stage": "queued"}
            return True
