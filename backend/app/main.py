import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from .api.router import router as api_router
from .core.config import Settings, get_settings
from .core.diagnostics import configure_diagnostics

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_diagnostics()
    @asynccontextmanager
    async def lifespan(_app):
        task = None
        if app_settings.TRIP_EXECUTION_MODE == "database_worker" and app_settings.TRIP_WORKER_ENABLED and app_settings.DATABASE_URL:
            from .worker import run_worker
            task = asyncio.create_task(run_worker(app_settings))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    application = FastAPI(
        title=app_settings.APP_NAME,
        debug=app_settings.APP_DEBUG,
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(api_router, prefix=app_settings.API_V1_PREFIX)

    if app_settings.APP_DEBUG:

        @application.middleware("http")
        async def hide_debug_tracebacks(
            request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            try:
                return await call_next(request)
            except Exception:
                logger.error("Unhandled application error")
                return _internal_error_response()

    @application.get("/health")
    def health_check() -> dict[str, str]:
        return {"status": "healthy"}

    @application.exception_handler(Exception)
    async def unexpected_error_handler(
        _request: Request, _exc: Exception
    ) -> JSONResponse:
        logger.error("Unhandled application error")
        return _internal_error_response()

    return application


def _internal_error_response() -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_server_error",
                "message": "An unexpected error occurred.",
            }
        },
    )


app = create_app()
