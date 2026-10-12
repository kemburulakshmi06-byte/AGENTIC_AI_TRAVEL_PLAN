from fastapi import APIRouter, Request

from .trips import router as trips_router

router = APIRouter()
router.include_router(trips_router)


@router.get("/status")
def get_status(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    return {
        "service": settings.APP_NAME,
        "status": "healthy",
        "environment": settings.APP_ENV,
    }
