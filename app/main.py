"""
app/main.py
===========
FastAPI application factory and entrypoint.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.config import settings
from app.core.exceptions import AppException, app_exception_handler
from app.logger import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager — startup and shutdown events."""
    setup_logging()
    logger.info(
        "Starting DB-ChatBot API...",
        extra={"environment": settings.ENVIRONMENT, "version": settings.APP_VERSION},
    )
    yield
    logger.info("Shutting down DB-ChatBot API...")


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application instance."""
    app_instance = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=settings.APP_DESCRIPTION,
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
        lifespan=lifespan,
    )

    # CORS Configuration
    app_instance.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Global Exception Handlers
    app_instance.add_exception_handler(AppException, app_exception_handler)

    @app_instance.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        logger.warning("Validation error on request.", extra={"errors": exc.errors()})
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request body or query parameters validation failed.",
                    "details": exc.errors(),
                }
            },
        )

    # Mount API Router
    app_instance.include_router(api_router)

    return app_instance


app = create_app()
