"""
app/schemas/stream.py
======================
Pydantic models for Server-Sent Events (SSE) streaming.

Every SSE frame is a JSON-encoded StreamEvent. The `stage` field tells the
client which pipeline step just completed; the `data` field carries
stage-specific payload.

Stages (in order):
  received        – message accepted, pipeline starting
  extracting_schema – fetching live DB schema
  generating_sql  – calling the LLM
  validating_sql  – running 8-layer SQL validator
  executing       – running the SQL against the target DB
  complete        – final response (mirrors NLQueryResponse)
  error           – unrecoverable pipeline failure
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.query import QueryResultSet, SQLGenerationDetails, TokenUsage


# ---------------------------------------------------------------------------
# Stage enumeration
# ---------------------------------------------------------------------------

class PipelineStage(str, Enum):
    RECEIVED = "received"
    EXTRACTING_SCHEMA = "extracting_schema"
    GENERATING_SQL = "generating_sql"
    VALIDATING_SQL = "validating_sql"
    EXECUTING = "executing"
    COMPLETE = "complete"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Per-stage data payloads (kept lean – no secrets, no raw passwords)
# ---------------------------------------------------------------------------

class ReceivedPayload(BaseModel):
    session_id: UUID
    connection_id: UUID
    message: str


class ExtractingSchemaPayload(BaseModel):
    table_count: int
    dialect: str
    cached: bool = False


class GeneratingSQLPayload(BaseModel):
    provider: str
    model: str
    prompt_tokens_estimate: int = 0


class ValidatingSQLPayload(BaseModel):
    sql_preview: str          # First 200 chars of generated SQL
    checks_run: int = 8


class ExecutingPayload(BaseModel):
    sql_preview: str          # First 200 chars of validated SQL
    timeout_seconds: int


class CompletePayload(BaseModel):
    session_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    question: str
    answer: str
    sql_details: SQLGenerationDetails
    results: QueryResultSet | None = None
    token_usage: TokenUsage
    pipeline_ms: float


class ErrorPayload(BaseModel):
    stage: PipelineStage      # Which stage raised the error
    code: str                 # Machine-readable error code
    message: str              # Human-readable description
    detail: str = ""          # Optional stack-trace fragment (debug only)


# ---------------------------------------------------------------------------
# Unified SSE frame
# ---------------------------------------------------------------------------

class StreamEvent(BaseModel):
    """
    A single SSE data frame.  Serialised as:

        data: {"stage": "...", "elapsed_ms": ..., "data": {...}}\n\n
    """

    stage: PipelineStage
    elapsed_ms: float = Field(default=0.0, description="Wall-clock ms since the pipeline started")
    data: Any = None          # One of the *Payload models above, or None


# ---------------------------------------------------------------------------
# Streaming query request
# ---------------------------------------------------------------------------

class StreamQueryRequest(BaseModel):
    """
    POST /api/v1/chat/query

    Unlike session-scoped queries, this endpoint accepts a standalone
    connection_id alongside the message so the client can drive the
    execution engine independently of a persisted chat session.
    A session_id is optional – when supplied, the conversation is persisted
    to chat_messages; when omitted, execution is ephemeral.
    """

    connection_id: UUID = Field(
        ...,
        description="Active connection UUID obtained from POST /database/connect",
        examples=["35589cbd-73e0-4ca2-b429-ed800f6f2903"],
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="Natural language question about the connected database",
        examples=["Show me the top 10 customers by total order value"],
    )
    session_id: UUID | None = Field(
        default=None,
        description="Optional chat session UUID. If provided, messages are persisted.",
    )
    timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=120,
        description="Maximum seconds to wait for SQL execution",
    )
    max_rows: int = Field(
        default=500,
        ge=1,
        le=5000,
        description="Maximum rows to return",
    )
