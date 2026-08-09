"""
app/api/v1/database.py
=======================
FastAPI router for Database Connection Management.
"""

from __future__ import annotations

import uuid
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.schemas.database import (
    ConnectionRequest,
    ConnectionResponse,
    DisconnectResponse,
    ValidationResponse,
)
from app.services.database_service import DatabaseConnectionService, get_database_service

router = APIRouter(prefix="/database", tags=["Database Connections"])


@router.post("/connect", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
async def connect(
    request: ConnectionRequest,
    service: DatabaseConnectionService = Depends(get_database_service),
    db: AsyncSession = Depends(get_db_session),
) -> ConnectionResponse:
    return await service.connect(request, db)


@router.get("/validate/{connection_id}", response_model=ValidationResponse, status_code=status.HTTP_200_OK)
async def validate(
    connection_id: uuid.UUID,
    service: DatabaseConnectionService = Depends(get_database_service),
    db: AsyncSession = Depends(get_db_session),
) -> ValidationResponse:
    return await service.validate(connection_id, db)


@router.delete("/disconnect/{connection_id}", response_model=DisconnectResponse, status_code=status.HTTP_200_OK)
async def disconnect(
    connection_id: uuid.UUID,
    service: DatabaseConnectionService = Depends(get_database_service),
    db: AsyncSession = Depends(get_db_session),
) -> DisconnectResponse:
    return await service.disconnect(connection_id, db)
