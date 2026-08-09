"""
app/api/v1/health.py
====================
Health check endpoints.
"""

from fastapi import APIRouter, status
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["Health"])


class HealthResponse(BaseModel):
    status: str = "ok"


@router.get("", response_model=HealthResponse, status_code=status.HTTP_200_OK)
@router.get("/ready", response_model=HealthResponse, status_code=status.HTTP_200_OK)
async def health_check() -> HealthResponse:
    return HealthResponse(status="ok")
