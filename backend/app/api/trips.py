from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Callable
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

from ..core.config import Settings
from ..database.session import get_session_factory
from ..schemas.trip_api import (
    TripCreatedResponse,
    TripDetailsResponse,
    TripListResponse,
    TripPlanRequest,
    TripStatusResponse,
)
from ..services.task_status_service import TaskStatusService
from ..services.trip_planning_service import (
    TripNotFoundError,
    TripPersistenceError,
    TripPlanningService,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/trips", tags=["trips"])


def get_trip_planning_service(request: Request) -> TripPlanningService:
    factory: Callable[[], TripPlanningService] | None = getattr(
        request.app.state, "trip_planning_service_factory", None
    )
    if factory is not None:
        return factory()
    settings: Settings = request.app.state.settings
    try:
        return TripPlanningService(
            session_factory=get_session_factory(),
            task_status=TaskStatusService(settings=settings),
            settings=settings,
        )
    except Exception as exc:
        logger.error("Trip planning dependencies could not be initialized.")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "trip_service_unavailable",
                "message": "Trip planning storage is not configured or unavailable.",
            },
        ) from exc


TripService = Annotated[TripPlanningService, Depends(get_trip_planning_service)]


def get_admin_trip_planning_service(request: Request) -> TripPlanningService:
    _require_admin_api(request)
    return get_trip_planning_service(request)


AdminTripService = Annotated[
    TripPlanningService,
    Depends(get_admin_trip_planning_service),
]


def get_history_trip_planning_service(request: Request) -> TripPlanningService:
    settings: Settings = request.app.state.settings
    if not (settings.TRIP_HISTORY_API_ENABLED or settings.TRIP_ADMIN_API_ENABLED):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "trip_history_api_disabled",
                "message": "Trip history is disabled in this environment.",
            },
        )
    return get_trip_planning_service(request)


HistoryTripService = Annotated[
    TripPlanningService, Depends(get_history_trip_planning_service)
]


@router.post(
    "/plan",
    response_model=TripCreatedResponse,
    status_code=202,
    summary="Queue an asynchronous trip plan",
)
async def create_trip_plan(
    payload: TripPlanRequest,
    background_tasks: BackgroundTasks,
    service: TripService,
    request: Request,
) -> TripCreatedResponse:
    try:
        trip_id = await service.create_trip(payload)
    except TripPersistenceError as exc:
        await service.aclose()
        raise HTTPException(
            status_code=503,
            detail={
                "code": "trip_persistence_unavailable",
                "message": "The trip request could not be saved.",
            },
        ) from exc
    if request.app.state.settings.TRIP_EXECUTION_MODE == "database_worker":
        # Job and complete request were committed atomically with the trip.
        await service.aclose()
    else:
        background_tasks.add_task(
            _run_and_close,
            service,
            trip_id,
            payload.model_dump(exclude={"user_message"}) | (
                {"user_message": payload.user_message}
                if payload.user_message is not None
                else {}
            ),
        )
    return TripCreatedResponse(
        trip_id=trip_id,
        task_id=trip_id,
        status="queued",
        message="Trip request was accepted for asynchronous planning.",
    )


async def _run_and_close(
    service: TripPlanningService,
    trip_id: UUID,
    raw_request: dict[str, object],
) -> None:
    from ..schemas.agent_schemas import TravelRequest

    try:
        await service.run_trip(trip_id, TravelRequest.model_validate(raw_request))
    except asyncio.CancelledError:
        await service.fail_trip(trip_id, "Travel planning was cancelled.")
        raise
    except Exception:
        logger.error("Background trip planning task failed.")
        try:
            await service.fail_trip(
                trip_id,
                "Travel planning failed; sensitive details were suppressed.",
            )
        except Exception:
            logger.error("Unable to record background workflow failure.")
    finally:
        try:
            await service.aclose()
        except Exception:
            logger.warning("Trip task resources could not be closed cleanly.")


@router.get(
    "/{trip_id}/status",
    response_model=TripStatusResponse,
    summary="Get trip planning status",
)
async def get_trip_status(
    trip_id: UUID,
    service: TripService,
) -> TripStatusResponse:
    try:
        value = await service.get_trip_status(trip_id)
    except TripNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "trip_not_found", "message": "Trip was not found."},
        ) from exc
    except TripPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "trip_status_unavailable",
                "message": "Trip status is temporarily unavailable.",
            },
        ) from exc
    finally:
        await service.aclose()
    return TripStatusResponse.model_validate(value)


@router.get(
    "",
    response_model=TripListResponse,
    summary="List saved trips (restricted)",
)
async def list_trips(
    service: HistoryTripService,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> TripListResponse:
    try:
        items, total = await service.list_trips(limit, offset)
        return TripListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )
    except TripPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "trip_list_unavailable",
                "message": "Saved trips are temporarily unavailable.",
            },
        ) from exc
    finally:
        await service.aclose()


@router.get(
    "/{trip_id}",
    response_model=TripDetailsResponse,
    summary="Get persisted trip details",
)
async def get_trip(
    trip_id: UUID,
    service: TripService,
) -> TripDetailsResponse:
    try:
        return await service.get_trip(trip_id)
    except TripNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "trip_not_found", "message": "Trip was not found."},
        ) from exc
    except TripPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "trip_details_unavailable",
                "message": "Trip details are temporarily unavailable.",
            },
        ) from exc
    finally:
        await service.aclose()


@router.delete(
    "/{trip_id}",
    status_code=204,
    summary="Delete a saved trip (restricted)",
)
async def delete_trip(
    trip_id: UUID,
    service: AdminTripService,
) -> None:
    try:
        deleted = await service.delete_trip(trip_id)
    except TripPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "trip_delete_unavailable",
                "message": "The trip could not be deleted.",
            },
        ) from exc
    finally:
        await service.aclose()
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"code": "trip_not_found", "message": "Trip was not found."},
        )


def _require_admin_api(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if not settings.TRIP_ADMIN_API_ENABLED:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "trip_admin_api_disabled",
                "message": "Trip listing and deletion are disabled.",
            },
        )
