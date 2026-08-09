"""
app/api/router.py
=================
Central API router aggregator.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import cache, chat, database, health, query, stream_query
from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)

api_router = APIRouter(prefix=settings.API_V1_PREFIX)

api_router.include_router(health.router)
api_router.include_router(database.router)
api_router.include_router(chat.router)
api_router.include_router(query.router)
api_router.include_router(stream_query.router)
api_router.include_router(cache.router)

logger.debug("API router assembled.", extra={"prefix": settings.API_V1_PREFIX})
