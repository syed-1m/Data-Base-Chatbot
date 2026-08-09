"""app/schemas/query.py"""
from __future__ import annotations

from typing import Any
from uuid import UUID
from pydantic import BaseModel, Field


class NLQueryRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    model: str


class QueryResultSet(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False
    execution_ms: float


class SQLGenerationDetails(BaseModel):
    sql_query: str
    reasoning: str
    confidence: float
    assumptions: list[str] = Field(default_factory=list)
    refinement_attempts: int = 0
    validation_passed: bool
    validation_error: str = ""


class NLQueryResponse(BaseModel):
    session_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    question: str
    answer: str
    sql_details: SQLGenerationDetails
    results: QueryResultSet | None = None
    token_usage: TokenUsage
    pipeline_ms: float
