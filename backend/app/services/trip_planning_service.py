from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from ..core.config import Settings, get_settings
from ..core.diagnostics import diagnostic_context, record_event, record_state_snapshot
from time import perf_counter
from ..database.models import (
    AgentRun,
    ItineraryActivity,
    ItineraryDay,
    TravelPreference,
    Trip,
)
from ..schemas.agent_schemas import TravelRequest
from ..schemas.budget import BudgetBreakdown
from ..schemas.itinerary import TripItinerary
from ..schemas.trip_api import (
    PersistedActivityResponse,
    PersistedDayResponse,
    TripDetailsResponse,
    TripSummaryResponse,
)
from ..workflows.state import TravelWorkflowState
from ..workflows.travel_graph import plan_trip
from .task_status_service import TaskStatusName, TaskStatusService
from .trip_job_service import JOB_AGENT, job_for, new_job, owns_job

logger = logging.getLogger(__name__)


class TripNotFoundError(LookupError):
    pass


class TripPersistenceError(RuntimeError):
    pass


class TripPlanningService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        task_status: TaskStatusService,
        *,
        workflow: Callable[..., Any] = plan_trip,
        settings: Settings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._task_status = task_status
        self._workflow = workflow
        self._settings = settings or get_settings()

    async def create_trip(self, request: TravelRequest) -> UUID:
        trip_id = uuid4()
        try:
            await run_in_threadpool(self._create_trip, trip_id, request)
        except SQLAlchemyError as exc:
            logger.error("Unable to persist a new trip request.")
            raise TripPersistenceError from exc
        await self._safe_redis_create(trip_id)
        return trip_id

    async def run_trip(self, trip_id: UUID, request: TravelRequest, *, lease_token: str | None = None) -> None:
        with diagnostic_context(trip_id=trip_id):
            await self._run_trip(trip_id, request, lease_token=lease_token)

    async def _run_trip(self, trip_id: UUID, request: TravelRequest, *, lease_token: str | None = None) -> None:
        if lease_token is None and not await run_in_threadpool(self._mark_running, trip_id):
            return
        async def checkpoint(state):
            saved = await run_in_threadpool(self._persist_result, trip_id, state, lease_token, False)
            if not saved:
                raise RuntimeError("Job ownership was lost before checkpoint persistence.")
        options = {}
        if lease_token is not None:
            options["on_state"] = checkpoint
        await self._safe_redis_update(
            trip_id,
            status="running",
            current_agent="planning",
            progress_percentage=5,
            message="Travel planning is in progress.",
        )
        try:
            async with asyncio.timeout(
                self._settings.TRIP_WORKFLOW_TIMEOUT_SECONDS + 5
            ):
                result = await self._workflow(
                    request,
                    max_revisions=self._settings.TRIP_MAX_REVISIONS,
                    timeout_seconds=self._settings.TRIP_WORKFLOW_TIMEOUT_SECONDS,
                    agent_timeout_seconds=self._settings.TRIP_AGENT_TIMEOUT_SECONDS,
                    **options,
                )
        except asyncio.CancelledError:
            if lease_token is None:
                await self.fail_trip(trip_id, "Travel planning was cancelled.")
            raise
        except TimeoutError:
            await self.fail_trip(
                trip_id, "The travel planning workflow timed out.", lease_token=lease_token
            )
            return
        except Exception as exc:
            record_event("workflow", "workflow.execute", "failed", perf_counter(), category=type(exc).__name__)
            logger.error("Travel planning workflow failed.")
            await self.fail_trip(
                trip_id,
                "The travel planning workflow failed; provider details were suppressed.",
                lease_token=lease_token,
            )
            return

        status = result.get("execution_status", "FAILED")
        try:
            args = (trip_id, result) if lease_token is None else (trip_id, result, lease_token)
            saved = await run_in_threadpool(self._persist_result, *args)
        except Exception as exc:
            record_event("database", "result.persist", "failed", perf_counter(), category=type(exc).__name__)
            logger.error("Unable to persist the completed trip workflow.")
            await self.fail_trip(trip_id, "The workflow result could not be saved.", lease_token=lease_token)
            return
        if not saved:
            return

        redis_status = _redis_status(status)
        terminal_message = _status_message(status)
        await self._safe_redis_update(
            trip_id,
            status=redis_status,
            current_agent=result.get("current_stage"),
            progress_percentage=100,
            message=terminal_message,
            error_message=(
                _first_safe_error(result.get("errors", []))
                if status in {"FAILED", "INVALID_REQUEST"}
                else None
            ),
        )

    async def fail_trip(self, trip_id: UUID, message: str, *, lease_token: str | None = None) -> None:
        args = (trip_id, "failed", message) if lease_token is None else (trip_id, "failed", message, lease_token)
        changed = await run_in_threadpool(self._persist_failure, *args)
        if changed is not False:
            await self._safe_redis_update(
                trip_id,
                status="failed",
                error_message=message,
            )

    async def get_trip(self, trip_id: UUID) -> TripDetailsResponse:
        try:
            value = await run_in_threadpool(self._get_trip, trip_id)
        except SQLAlchemyError as exc:
            logger.error("Unable to retrieve trip details.")
            raise TripPersistenceError from exc
        if value is None:
            raise TripNotFoundError
        return value

    async def get_trip_status(self, trip_id: UUID) -> dict[str, Any]:
        try:
            trip_data = await run_in_threadpool(self._get_status_source, trip_id)
        except SQLAlchemyError as exc:
            logger.error("Unable to retrieve trip status.")
            raise TripPersistenceError from exc
        if trip_data is None:
            raise TripNotFoundError
        if not trip_data.get("durable_job") and _api_status(trip_data["status"]) in {"queued", "running"}:
            updated = trip_data["updated_at"]
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - updated).total_seconds() > self._settings.TRIP_WORKFLOW_TIMEOUT_SECONDS + 60:
                await self.fail_trip(trip_id, "Planning exceeded its execution window or the background worker stopped. Submit a new request to try again.")
                # Re-read after the row lock: a completed worker can win this race.
                try:
                    trip_data = await run_in_threadpool(self._get_status_source, trip_id)
                except SQLAlchemyError as exc:
                    raise TripPersistenceError from exc
                if trip_data is None:
                    raise TripNotFoundError
                if _api_status(trip_data["status"]) in {"queued", "running"}:
                    # Both storage writes may be unavailable. The execution
                    # window has still expired; never keep the client polling.
                    trip_data = {**trip_data, "status": "failed", "error": "Planning exceeded its execution window; recovery storage is unavailable. Submit a new request after service recovery."}
        try:
            redis_value = await self._task_status.get_task_status(trip_id)
        except Exception:
            logger.warning("Redis status lookup was unavailable.")
            redis_value = None
        status = _api_status(trip_data["status"])
        if redis_value is not None and status in {"queued", "running"} and not trip_data.get("durable_job"):
            status = _api_status(redis_value.status)
            current_stage = redis_value.current_agent
            progress = redis_value.progress_percentage
            message = redis_value.message
            error = redis_value.error_message
            updated_at = redis_value.updated_at
        else:
            current_stage = trip_data.get("current_stage")
            progress = trip_data.get("progress_percentage") or (
                100
                if status
                in {
                    "completed",
                    "failed",
                    "revision_required",
                    "insufficient_data",
                }
                else 0
            )
            message = trip_data.get("message") or _status_message(trip_data["status"])
            error = trip_data["error"]
            updated_at = trip_data["updated_at"]
        return {
            "trip_id": trip_id,
            "task_id": trip_id,
            "status": status,
            "current_stage": current_stage,
            "progress_percentage": progress,
            "message": message,
            "error_message": error,
            "created_at": trip_data["created_at"],
            "updated_at": updated_at,
        }

    async def list_trips(self, limit: int, offset: int) -> tuple[list[TripSummaryResponse], int]:
        try:
            return await run_in_threadpool(self._list_trips, limit, offset)
        except SQLAlchemyError as exc:
            logger.error("Unable to list saved trips.")
            raise TripPersistenceError from exc

    async def delete_trip(self, trip_id: UUID) -> bool:
        try:
            deleted = await run_in_threadpool(self._delete_trip, trip_id)
        except SQLAlchemyError as exc:
            logger.error("Unable to delete trip.")
            raise TripPersistenceError from exc
        if deleted:
            await self._task_status.delete_task_status(trip_id)
        return deleted

    def aclose(self) -> Any:
        return self._task_status.aclose()

    def _create_trip(self, trip_id: UUID, request: TravelRequest) -> None:
        if request.currency is None:
            raise TripPersistenceError("A requested currency is required before persistence.")
        with self._session_factory() as session:
            with session.begin():
                constraints = dict(request.constraints)
                if request.cost_preferences is not None:
                    constraints["cost_preferences"] = request.cost_preferences.model_dump(
                        mode="json", exclude_none=True
                    )
                preference = TravelPreference(
                    origin=request.origin,
                    destination=request.destination,
                    start_date=request.start_date,
                    end_date=request.end_date,
                    travelers_count=request.travelers_count,
                    total_budget=request.total_budget,
                    currency=request.currency,
                    travel_style=request.travel_style,
                    interests=request.interests,
                    constraints=constraints,
                )
                session.add(preference)
                session.flush()
                session.add(
                    Trip(
                        id=trip_id,
                        preference_id=preference.id,
                        title=f"{request.destination} trip",
                        status="queued",
                        currency=request.currency,
                    )
                )
                if self._settings.TRIP_EXECUTION_MODE == "database_worker":
                    session.flush()
                    session.add(new_job(trip_id, request))
        with diagnostic_context(trip_id=trip_id):
            record_event("database", "request.persist", "success", perf_counter())

    def _mark_running(self, trip_id: UUID) -> bool:
        with self._session_factory() as session:
            with session.begin():
                trip = session.execute(
                    select(Trip).where(Trip.id == trip_id).with_for_update()
                ).scalar_one_or_none()
                if trip is None or trip.status != "queued":
                    return False
                trip.status = "running"
            return True

    def _persist_result(
        self, trip_id: UUID, result: TravelWorkflowState, lease_token: str | None = None, terminal: bool = True
    ) -> bool:
        workflow_status = str(result.get("execution_status", "FAILED"))
        trip_status = _database_status(workflow_status)
        budget = result.get("budget_breakdown")
        itinerary = result.get("trip_itinerary")

        with self._session_factory() as session:
            with session.begin():
                trip = session.execute(
                    select(Trip).where(Trip.id == trip_id).with_for_update()
                ).scalar_one_or_none()
                if trip is None or trip.status not in {"queued", "running"}:
                    return False
                if lease_token is not None and not owns_job(session, trip_id, lease_token):
                    return False
                if isinstance(itinerary, TripItinerary):
                    session.execute(delete(ItineraryDay).where(ItineraryDay.trip_id == trip_id))
                valid_stages = {record.agent_name for record in result.get("agent_history", [])
                    if record.output_data is not None}
                # Backward-compatible execution records can hold their output
                # only in the final state rather than individual snapshots.
                for stage, field in {"requirements":"normalized_requirements", "destination_research":"destination_research",
                    "accommodation_transport":"accommodation_transport_plan", "budget":"budget_breakdown",
                    "itinerary":"trip_itinerary", "optimization_review":"optimization_review"}.items():
                    if result.get(field) is not None:
                        valid_stages.add(stage)
                session.execute(delete(AgentRun).where(AgentRun.trip_id == trip_id,
                    AgentRun.agent_name.in_(valid_stages | {"workflow"})))
                trip.status = trip_status if terminal else "running"
                if isinstance(budget, BudgetBreakdown):
                    trip.total_estimated_cost = budget.estimated_total
                    trip.currency = budget.currency or trip.currency
                elif isinstance(itinerary, TripItinerary):
                    trip.total_estimated_cost = itinerary.estimated_total_cost
                    trip.currency = itinerary.currency or trip.currency

                if isinstance(itinerary, TripItinerary):
                    self._save_itinerary(session, trip_id, itinerary)
                self._save_agent_runs(session, trip_id, result)
                self._save_workflow_summary(session, trip_id, result)
                if lease_token is not None:
                    job = job_for(session, trip_id)
                    job.output_data = {**job.output_data, "current_stage": result.get("current_stage")}
                    if terminal:
                        job.status = trip_status
                        job.completed_at = datetime.now(timezone.utc)
                        job.error_message = _first_safe_error(result.get("errors", []))
            record_event("database", "result.persist" if terminal else "stage.persist", "success", perf_counter())
            record_state_snapshot(result)
            return True

    def _save_itinerary(
        self,
        session: Session,
        trip_id: UUID,
        itinerary: TripItinerary,
    ) -> None:
        for day in itinerary.days:
            daily_amount = day.daily_cost.amount
            summary = day.theme or None
            if daily_amount is None:
                summary = f"{summary or ''} [daily cost estimate unknown]".strip()
            day_record = ItineraryDay(
                trip_id=trip_id,
                day_number=day.day_number,
                itinerary_date=day.date,
                summary=summary,
                estimated_daily_cost=daily_amount or 0,
            )
            session.add(day_record)
            session.flush()
            for activity in day.activities:
                activity_metadata = {
                    "activity_type": activity.activity_type,
                    "place_id": activity.place_id,
                    "latitude": activity.latitude,
                    "longitude": activity.longitude,
                    "coordinates_verified": activity.coordinates_verified,
                    "travel_time_from_previous_minutes": (
                        str(activity.travel_time_from_previous_minutes)
                        if activity.travel_time_from_previous_minutes is not None
                        else None
                    ),
                    "travel_time_verified": activity.travel_time_verified,
                    "travel_time_provider": activity.travel_time_provider,
                    "cost_currency": activity.cost_currency,
                    "cost_classification": activity.cost_classification,
                    "source_references": [
                        item.model_dump(mode="json")
                        for item in activity.source_references
                    ],
                    "warnings": activity.warnings,
                    "estimated_cost_unknown": activity.estimated_cost is None,
                    "intercity_transfer": activity.intercity_transfer.model_dump(mode="json") if activity.intercity_transfer else None,
                }
                session.add(
                    ItineraryActivity(
                        itinerary_day_id=day_record.id,
                        title=activity.title,
                        description=activity.description,
                        location_name=activity.location,
                        start_time=activity.start_time,
                        end_time=activity.end_time,
                        estimated_cost=activity.estimated_cost or 0,
                        category=activity.activity_type,
                        metadata_json=activity_metadata,
                    )
                )

    def _save_agent_runs(
        self,
        session: Session,
        trip_id: UUID,
        result: TravelWorkflowState,
    ) -> None:
        outputs = {
            "requirements": "normalized_requirements",
            "destination_research": "destination_research",
            "accommodation_transport": "accommodation_transport_plan",
            "budget": "budget_breakdown",
            "itinerary": "trip_itinerary",
            "optimization_review": "optimization_review",
        }
        for record in result.get("agent_history", []):
            output_field = outputs.get(record.agent_name)
            # Preserve each revision's own output, rather than rewriting history
            # with the last result. Older execution records retain compatibility.
            output = record.output_data if record.output_data is not None else (result.get(output_field) if output_field else None)
            session.add(
                AgentRun(
                    trip_id=trip_id,
                    agent_name=record.agent_name,
                    status=record.status,
                    input_data={
                        "workflow_stage": record.agent_name,
                        **(
                            {"user_message": result["request"].user_message}
                            if record.agent_name == "requirements"
                            and result["request"].user_message
                            else {}
                        ),
                    },
                    output_data=(
                        {"result": _json_value(output)}
                        if output is not None
                        else None
                    ),
                    error_message=record.error,
                    started_at=record.started_at,
                    completed_at=record.finished_at,
                )
            )

    def _save_workflow_summary(
        self,
        session: Session,
        trip_id: UUID,
        result: TravelWorkflowState,
    ) -> None:
        session.add(
            AgentRun(
                trip_id=trip_id,
                agent_name="workflow",
                status=str(result.get("execution_status", "FAILED")).lower(),
                input_data={"revision_count": result.get("revision_count", 0)},
                output_data={
                    "current_stage": result.get("current_stage"),
                    "revision_count": result.get("revision_count", 0),
                    "warnings": result.get("warnings", []),
                    "errors": result.get("errors", []),
                    "source_references": [
                        item.model_dump(mode="json")
                        for item in result.get("source_references", [])
                    ],
                },
                error_message=_first_safe_error(result.get("errors", [])),
                started_at=_workflow_started(result),
                completed_at=_workflow_finished(result),
            )
        )

    def _persist_failure(
        self, trip_id: UUID, status: str, message: str, lease_token: str | None = None
    ) -> bool | None:
        try:
            with self._session_factory() as session:
                with session.begin():
                    trip = session.execute(
                        select(Trip).where(Trip.id == trip_id).with_for_update()
                    ).scalar_one_or_none()
                    if trip is None:
                        return False
                    if trip.status not in {"queued", "running"}:
                        return False
                    if lease_token is not None and not owns_job(session, trip_id, lease_token):
                        return False
                    trip.status = status
                    if lease_token is not None:
                        job = job_for(session, trip_id)
                        job.status = status
                        job.error_message = message
                        job.completed_at = datetime.now(timezone.utc)
                    session.add(
                        AgentRun(
                            trip_id=trip_id,
                            agent_name="workflow",
                            status=status,
                            input_data={},
                            output_data={"errors": [message]},
                            error_message=message,
                            started_at=trip.created_at,
                            completed_at=None,
                        )
                    )
                return True
        except SQLAlchemyError:
            logger.error("Unable to persist workflow failure status.")
            return None

    def _get_trip(self, trip_id: UUID) -> TripDetailsResponse | None:
        with self._session_factory() as session:
            trip = session.execute(
                select(Trip)
                .where(Trip.id == trip_id)
                .options(
                    selectinload(Trip.preference),
                    selectinload(Trip.itinerary_days).selectinload(
                        ItineraryDay.activities
                    ),
                    selectinload(Trip.agent_runs),
                )
            ).scalar_one_or_none()
            if trip is None:
                return None
            run_output = {}
            for item in sorted(trip.agent_runs, key=lambda run: (run.started_at, str(run.id))):
                # A failed later revision must not erase an earlier valid output.
                if item.output_data and (item.agent_name == "workflow" or item.output_data.get("result") is not None):
                    run_output[item.agent_name] = item.output_data
            itinerary_data = (
                run_output.get("itinerary", {}).get("result")
            )
            budget_data = run_output.get("budget", {}).get("result")
            review_data = run_output.get("optimization_review", {}).get("result")
            accommodation_transport_data = run_output.get("accommodation_transport", {}).get("result")
            workflow = run_output.get("workflow", {})
            warnings = list((workflow or {}).get("warnings", []))
            errors = list((workflow or {}).get("errors", []))
            if trip.status == "failed" and not errors:
                errors = [item.error_message for item in trip.agent_runs if item.agent_name == JOB_AGENT and item.error_message]
            days: list[PersistedDayResponse] = []
            source_days = (
                itinerary_data.get("days", [])
                if isinstance(itinerary_data, dict)
                else []
            )
            daily_costs = {
                item.get("day_number"): item.get("daily_cost", {})
                for item in source_days
                if isinstance(item, dict)
            }
            for day in sorted(trip.itinerary_days, key=lambda item: item.day_number):
                daily = daily_costs.get(day.day_number, {})
                daily_amount = daily.get("amount")
                activities = []
                for item in day.activities:
                    metadata = item.metadata_json or {}
                    estimated_amount = (
                        None
                        if metadata.get("estimated_cost_unknown")
                        else item.estimated_cost
                    )
                    activities.append(
                        PersistedActivityResponse(
                            title=item.title,
                            description=item.description,
                            location_name=item.location_name,
                            start_time=item.start_time,
                            end_time=item.end_time,
                            estimated_cost=estimated_amount,
                            category=item.category,
                            metadata=metadata,
                        )
                    )
                days.append(
                    PersistedDayResponse(
                        day_number=day.day_number,
                        itinerary_date=day.itinerary_date,
                        summary=day.summary,
                        estimated_daily_cost=daily_amount,
                        activities=activities,
                    )
                )
            return TripDetailsResponse(
                id=trip.id,
                title=trip.title,
                status=_api_status(trip.status),
                request={
                    "origin": trip.preference.origin,
                    "destination": trip.preference.destination,
                    "start_date": trip.preference.start_date,
                    "end_date": trip.preference.end_date,
                    "travelers_count": trip.preference.travelers_count,
                    "total_budget": trip.preference.total_budget,
                    "currency": trip.preference.currency,
                    "travel_style": trip.preference.travel_style,
                    "interests": trip.preference.interests,
                    "constraints": trip.preference.constraints,
                    "cost_preferences": (trip.preference.constraints or {}).get("cost_preferences"),
                },
                total_estimated_cost=trip.total_estimated_cost,
                currency=trip.currency,
                created_at=trip.created_at,
                updated_at=trip.updated_at,
                itinerary=itinerary_data,
                budget=budget_data,
                optimization_review=review_data,
                accommodation_transport=accommodation_transport_data,
                normalized_requirements=run_output.get("requirements", {}).get("result"),
                destination_research=run_output.get("destination_research", {}).get("result"),
                days=days,
                warnings=warnings,
                errors=errors,
            )

    def _get_status_source(self, trip_id: UUID) -> dict[str, Any] | None:
        with self._session_factory() as session:
            trip = session.execute(
                select(Trip)
                .where(Trip.id == trip_id)
                .options(selectinload(Trip.agent_runs))
            ).scalar_one_or_none()
            if trip is None:
                return None
            runs = sorted(
                trip.agent_runs,
                key=lambda run: (run.started_at, str(run.id)),
                reverse=True,
            )
            error = next(
                (
                    item.error_message
                    for item in runs
                    if item.error_message
                ),
                None,
            )
            job = next((item for item in runs if item.agent_name == JOB_AGENT), None)
            metadata = (job.output_data or {}) if job else {}
            stage = metadata.get("current_stage") if job and trip.status in {"queued", "running"} else next(
                (item.agent_name for item in runs if item.agent_name not in {"workflow", JOB_AGENT}), None)
            stages = ["requirements", "destination_research", "accommodation_transport", "budget", "itinerary", "optimization_review"]
            return {
                "status": trip.status,
                "created_at": trip.created_at,
                "updated_at": trip.updated_at,
                "error": error,
                "current_stage": stage,
                "durable_job": job is not None,
                "progress_percentage": min(95, (stages.index(stage) + 1) * 15) if trip.status == "running" and stage in stages else None,
                "message": "Trip is persisted and waiting for a planning worker." if job and trip.status == "queued" else (
                    "Worker lease expired; waiting for automatic recovery." if job and trip.status == "running" and metadata.get("lease_expires_at", "") < datetime.now(timezone.utc).isoformat() else None),
            }

    def _list_trips(
        self, limit: int, offset: int
    ) -> tuple[list[TripSummaryResponse], int]:
        with self._session_factory() as session:
            total = session.query(Trip).count()
            records = session.execute(
                select(Trip)
                .options(selectinload(Trip.preference))
                .order_by(Trip.created_at.desc(), Trip.id.desc())
                .limit(limit)
                .offset(offset)
            ).scalars()
            summaries = [
                TripSummaryResponse(
                    id=item.id,
                    title=item.title,
                    status=_api_status(item.status),
                    origin=item.preference.origin,
                    destination=item.preference.destination,
                    start_date=item.preference.start_date,
                    end_date=item.preference.end_date,
                    travelers_count=item.preference.travelers_count,
                    total_budget=item.preference.total_budget,
                    currency=item.preference.currency,
                    total_estimated_cost=item.total_estimated_cost,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in records
            ]
            return summaries, total

    def _delete_trip(self, trip_id: UUID) -> bool:
        with self._session_factory() as session:
            with session.begin():
                trip = session.execute(
                    select(Trip).where(Trip.id == trip_id).with_for_update()
                ).scalar_one_or_none()
                if trip is None:
                    return False
                preference_id = trip.preference_id
                session.execute(
                    delete(Trip).where(Trip.id == trip_id),
                    execution_options={"synchronize_session": False},
                )
                remaining = session.query(Trip.id).filter(
                    Trip.preference_id == preference_id
                ).first()
                if remaining is None:
                    session.execute(
                        delete(TravelPreference).where(
                            TravelPreference.id == preference_id
                        )
                    )
            return True

    async def _safe_redis_create(self, trip_id: UUID) -> None:
        try:
            await self._task_status.create_task_status(
                trip_id,
                trip_id,
                current_agent="queued",
                message="Travel plan is queued.",
            )
        except Exception:
            logger.warning("Redis task initialization was unavailable.")

    async def _safe_redis_update(
        self,
        trip_id: UUID,
        *,
        status: TaskStatusName,
        current_agent: str | None = None,
        progress_percentage: int | None = None,
        message: str | None = None,
        error_message: str | None = None,
    ) -> None:
        try:
            await self._task_status.update_task_status(
                trip_id,
                status=status,
                current_agent=current_agent,
                progress_percentage=progress_percentage,
                message=message,
                error_message=error_message,
            )
        except Exception:
            logger.warning("Redis task status update was unavailable.")


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return json.loads(value.model_dump_json())
    return value


def _database_status(status: str) -> str:
    return {
        "COMPLETED": "completed",
        "INVALID_REQUEST": "failed",
        "FAILED": "failed",
        "INSUFFICIENT_DATA": "insufficient_data",
        "REVISION_REQUIRED": "revision_required",
    }.get(status, "failed")


def _api_status(status: str) -> str:
    if status == "pending":
        return "queued"
    if status in {
        "queued",
        "planned",
        "running",
        "completed",
        "failed",
        "revision_required",
        "insufficient_data",
    }:
        return status
    return "failed"


def _redis_status(status: str) -> TaskStatusName:
    return {
        "COMPLETED": "completed",
        "FAILED": "failed",
        "INVALID_REQUEST": "failed",
        "INSUFFICIENT_DATA": "insufficient_data",
        "REVISION_REQUIRED": "revision_required",
    }.get(status, "failed")  # type: ignore[return-value]


def _status_message(status: str) -> str:
    return {
        "queued": "Travel plan is queued.",
        "planned": "Trip is saved; asynchronous planning has not been run.",
        "running": "Travel planning is in progress.",
        "COMPLETED": "Travel plan completed.",
        "completed": "Travel plan completed.",
        "INVALID_REQUEST": "The request did not contain valid planning inputs.",
        "INSUFFICIENT_DATA": "Planning ended because required information is incomplete.",
        "insufficient_data": "Planning ended because required information is incomplete.",
        "REVISION_REQUIRED": "The plan requires revisions before approval.",
        "revision_required": "The plan requires revisions before approval.",
        "FAILED": "Travel planning failed.",
        "failed": "Travel planning failed.",
    }.get(status, "Travel plan status is available.")


def _first_safe_error(errors: Any) -> str | None:
    if not isinstance(errors, list):
        return None
    for item in errors:
        if isinstance(item, str) and item:
            return item[:1000]
    return None


def _workflow_started(result: TravelWorkflowState):
    history = result.get("agent_history", [])
    return min(
        (item.started_at for item in history),
        default=datetime.now(timezone.utc),
    )


def _workflow_finished(result: TravelWorkflowState):
    history = result.get("agent_history", [])
    return max(
        (item.finished_at for item in history),
        default=datetime.now(timezone.utc),
    )
