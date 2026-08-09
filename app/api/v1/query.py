"""
app/api/v1/query.py
====================
FastAPI router for the NL-to-SQL query endpoint.
"""

from __future__ import annotations

import uuid
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.ai_service import AIQueryService, get_ai_service
from app.db.session import get_db_session
from app.schemas.query import NLQueryRequest, NLQueryResponse

router = APIRouter(prefix="/chat", tags=["NL-to-SQL Query"])


@router.post("/sessions/{session_id}/query", response_model=NLQueryResponse, status_code=status.HTTP_200_OK)
async def nl_to_sql_query(
    session_id: uuid.UUID,
    req: NLQueryRequest,
    service: AIQueryService = Depends(get_ai_service),
    db: AsyncSession = Depends(get_db_session),
) -> NLQueryResponse:
    return await service.process_query(db, session_id, req)
