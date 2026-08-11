"""
app/services/stream_service.py
================================
Streaming pipeline orchestrator for the Query Execution Engine.

Architecture
------------
This module owns the *generator* that drives the full NL-to-SQL pipeline
and yields ``StreamEvent`` objects.  The FastAPI route wraps this generator
in a ``StreamingResponse`` (SSE format).

Pipeline stages (each yields a ``StreamEvent`` before starting work):

  1. received          – validate the incoming request
  2. extracting_schema – pull live schema from the target DB (with TTL cache)
  3. generating_sql    – call the configured LLM provider
  4. validating_sql    – run the 8-layer SQL safety validator
  5. executing         – run SQL with timeout + read-only transaction
  6. complete          – emit the full result payload
  error               – any unrecoverable failure at any stage

Design decisions
----------------
* **``AsyncGenerator``** – the generator is ``async def`` so it can ``await``
  IO operations between yields.  FastAPI's ``StreamingResponse`` accepts any
  async iterable.
* **SSE wire format** – each frame is emitted as ``data: <json>\\n\\n``.  This
  is the standard Server-Sent Events format that every modern browser and HTTP
  client understands natively.
* **Error isolation** – every stage is wrapped in a try/except.  Errors do NOT
  crash the stream; they are serialised as an ``error`` SSE frame so the client
  always receives a well-formed response.
* **Optional session persistence** – if ``session_id`` is present in the
  request, user + assistant messages are written to ``chat_messages`` using the
  repository layer.  If absent, execution is fully ephemeral (no DB writes).
* **Token usage logging** – input/output token counts from the LLM are
  persisted on the ``ChatMessage`` record for cost tracking.
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
import uuid
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm_client import get_llm_client
from app.ai.prompt_templates import NL_TO_SQL_SYSTEM_PROMPT, build_nl_to_sql_prompt, build_error_refinement_prompt
from app.ai.schema_extractor import SchemaExtractor
from app.ai.sql_validator import SQLValidator
from app.cache.cache_service import CacheLookupResult, cache_service
from app.config import settings
from app.core.exceptions import BadRequestException, NotFoundException
from app.logger import get_logger
from app.models.chat_session import MessageRole
from app.repositories.chat_repository import ChatMessageRepository, ChatSessionRepository
from app.schemas.query import SQLGenerationDetails, TokenUsage
from app.schemas.stream import (
    CompletePayload,
    ErrorPayload,
    ExecutingPayload,
    ExtractingSchemaPayload,
    GeneratingSQLPayload,
    PipelineStage,
    ReceivedPayload,
    StreamEvent,
    StreamQueryRequest,
    ValidatingSQLPayload,
)
from app.services.execution_service import ExecutionError, sql_execution_service
from app.utils.connection_manager import ConnectionEntry, connection_manager

logger = get_logger(__name__)

# Module-level singletons (stateless, thread-safe)
_schema_extractor = SchemaExtractor()
_validator = SQLValidator()
_session_repo = ChatSessionRepository()
_message_repo = ChatMessageRepository()


# ---------------------------------------------------------------------------
# SSE serialisation helper
# ---------------------------------------------------------------------------

def _sse_frame(event: StreamEvent) -> str:
    """
    Encode a ``StreamEvent`` as a valid SSE data frame.

    Format::

        data: {"stage": "...", "elapsed_ms": 42.1, "data": {...}}\n\n

    The double newline is required by the SSE spec to delimit frames.
    """
    payload = event.model_dump(mode="json")
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# Pipeline generator
# ---------------------------------------------------------------------------

async def run_query_pipeline(
    request: StreamQueryRequest,
    app_db: AsyncSession,
) -> AsyncGenerator[str, None]:
    """
    Async generator that drives the NL-to-SQL pipeline and yields SSE frames.

    Each yield is a UTF-8 string in SSE ``data: ...\\n\\n`` format.

    Parameters
    ----------
    request : StreamQueryRequest
        Validated incoming request (connection_id, message, optional session_id).
    app_db : AsyncSession
        SQLAlchemy async session for the *application* database (chat_sessions,
        chat_messages).  NOT the target user database.
    """
    pipeline_start = time.perf_counter()
    error_stage = PipelineStage.RECEIVED

    def elapsed() -> float:
        return round((time.perf_counter() - pipeline_start) * 1000, 1)

    # ------------------------------------------------------------------
    # Stage 1 – RECEIVED: validate connection exists
    # ------------------------------------------------------------------
    error_stage = PipelineStage.RECEIVED
    try:
        entry: ConnectionEntry | None = await connection_manager.get(request.connection_id)
        if entry is None:
            raise NotFoundException(
                message=f"Connection {request.connection_id} is not active. "
                        "Please reconnect via POST /api/v1/database/connect."
            )

        yield _sse_frame(StreamEvent(
            stage=PipelineStage.RECEIVED,
            elapsed_ms=elapsed(),
            data=ReceivedPayload(
                session_id=request.session_id or uuid.UUID(int=0),
                connection_id=request.connection_id,
                message=request.message,
            ).model_dump(mode="json"),
        ))

    except NotFoundException as exc:
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.ERROR,
            elapsed_ms=elapsed(),
            data=ErrorPayload(
                stage=error_stage,
                code="CONNECTION_NOT_FOUND",
                message=exc.message,
            ).model_dump(mode="json"),
        ))
        return

    # ------------------------------------------------------------------
    # Stage 1b – SESSION VALIDATION (when session_id is supplied)
    # ------------------------------------------------------------------
    error_stage = PipelineStage.RECEIVED
    if request.session_id is not None:
        try:
            chat_session = await _session_repo.get_active_by_id(app_db, request.session_id)
            if chat_session is None:
                yield _sse_frame(StreamEvent(
                    stage=PipelineStage.ERROR,
                    elapsed_ms=elapsed(),
                    data=ErrorPayload(
                        stage=error_stage,
                        code="SESSION_NOT_FOUND",
                        message=(
                            f"Chat session {request.session_id} does not exist or has been deleted. "
                            "Create a session via POST /api/v1/chat/sessions first."
                        ),
                    ).model_dump(mode="json"),
                ))
                return
        except Exception as exc:
            logger.error("Session validation failed.", exc_info=exc)
            yield _sse_frame(StreamEvent(
                stage=PipelineStage.ERROR,
                elapsed_ms=elapsed(),
                data=ErrorPayload(
                    stage=error_stage,
                    code="SESSION_VALIDATION_ERROR",
                    message="An error occurred while validating the chat session.",
                    detail=str(exc) if settings.DEBUG else "",
                ).model_dump(mode="json"),
            ))
            return

    # ------------------------------------------------------------------
    # Stage 1c – CACHE LOOKUP (before hitting the LLM)
    # ------------------------------------------------------------------
    if settings.CACHE_ENABLED:
        try:
            cache_result: CacheLookupResult = await cache_service.lookup(
                question=request.message,
                connection_id=str(request.connection_id),
                db=app_db,
            )

            if cache_result.is_hit and cache_result.entry is not None:
                entry_data = cache_result.entry
                pipeline_ms = elapsed()

                logger.info(
                    "Cache HIT — skipping LLM pipeline.",
                    extra={
                        "similarity": round(cache_result.similarity, 4),
                        "backend": cache_result.backend,
                        "candidates_searched": cache_result.candidates_searched,
                        "search_ms": cache_result.search_ms,
                    },
                )

                # Reconstruct result objects from cached data
                from app.schemas.query import QueryResultSet
                cached_result_set = QueryResultSet(
                    columns=entry_data.columns,
                    rows=entry_data.result_preview,
                    row_count=entry_data.row_count,
                    truncated=entry_data.truncated,
                    execution_ms=entry_data.execution_ms,
                )
                cached_sql_details = SQLGenerationDetails(
                    sql_query=entry_data.sql_query,
                    reasoning=entry_data.sql_reasoning,
                    confidence=entry_data.sql_confidence,
                    assumptions=[],
                    refinement_attempts=0,
                    validation_passed=True,
                    validation_error="",
                )
                cached_token_usage = TokenUsage(
                    input_tokens=0,   # Zero — LLM was NOT called
                    output_tokens=0,
                    total_tokens=0,
                    model=entry_data.llm_model,
                )

                row_note = f"{entry_data.row_count} row(s)"
                if entry_data.truncated:
                    row_note += f" (truncated)"
                cached_answer = (
                    f"[Cached] {row_note} returned.\n\n"
                    f"```sql\n{entry_data.sql_query}\n```"
                )

                yield _sse_frame(StreamEvent(
                    stage=PipelineStage.COMPLETE,
                    elapsed_ms=pipeline_ms,
                    data={
                        **CompletePayload(
                            session_id=request.session_id or uuid.UUID(int=0),
                            user_message_id=uuid.uuid4(),
                            assistant_message_id=uuid.uuid4(),
                            question=request.message,
                            answer=cached_answer,
                            sql_details=cached_sql_details,
                            results=cached_result_set,
                            token_usage=cached_token_usage,
                            pipeline_ms=pipeline_ms,
                        ).model_dump(mode="json"),
                        # Extra cache metadata for the client
                        "cache_hit": True,
                        "cache_key": cache_result.cache_key,
                        "cache_similarity": round(cache_result.similarity, 4),
                        "cache_backend": cache_result.backend,
                        "cache_search_ms": cache_result.search_ms,
                        "cache_original_pipeline_ms": entry_data.pipeline_ms,
                    },
                ))
                return   # Short-circuit — pipeline complete via cache

        except Exception as exc:
            # Cache failures are NEVER fatal — fall through to full pipeline
            logger.warning("Cache lookup failed (continuing without cache): %s", exc)

    # ------------------------------------------------------------------
    # Stage 2 – EXTRACTING_SCHEMA
    # ------------------------------------------------------------------
    error_stage = PipelineStage.EXTRACTING_SCHEMA
    schema_info: dict = {}
    try:
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.EXTRACTING_SCHEMA,
            elapsed_ms=elapsed(),
            data=ExtractingSchemaPayload(
                table_count=0,
                dialect=entry.db_type.value if hasattr(entry.db_type, "value") else str(entry.db_type),
                cached=False,
            ).model_dump(mode="json"),
        ))
        # give the client a moment to render the stage before the await
        await asyncio.sleep(0)

        schema_info = await _schema_extractor.get_schema(request.connection_id)
        table_count = len(schema_info.get("tables", {}))
        cached = schema_info.get("_cached", False)

        # Re-emit with real table count (overwrite in-place on client side)
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.EXTRACTING_SCHEMA,
            elapsed_ms=elapsed(),
            data=ExtractingSchemaPayload(
                table_count=table_count,
                dialect=schema_info.get("dialect", "postgresql"),
                cached=cached,
            ).model_dump(mode="json"),
        ))

    except Exception as exc:
        logger.error("Schema extraction failed.", exc_info=exc)
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.ERROR,
            elapsed_ms=elapsed(),
            data=ErrorPayload(
                stage=error_stage,
                code="SCHEMA_EXTRACTION_FAILED",
                message="Could not retrieve database schema.",
                detail=str(exc),
            ).model_dump(mode="json"),
        ))
        return

    # ------------------------------------------------------------------
    # Stage 3 – GENERATING_SQL (LLM call)
    # ------------------------------------------------------------------
    error_stage = PipelineStage.GENERATING_SQL
    llm_response = None
    raw_sql = ""
    reasoning = ""
    confidence = 0.0
    parsed_json: dict = {}

    try:
        llm_client = get_llm_client()
        provider = settings.AI_PROVIDER
        model = settings.AI_MODEL

        # Fetch recent conversation history for context if session supplied
        history: list[dict] = []
        if request.session_id is not None:
            recent_msgs, _ = await _message_repo.list_by_session(
                app_db, request.session_id, offset=0, limit=6
            )
            history = [{"role": m.role, "content": m.content[:500]} for m in recent_msgs]

        user_prompt = build_nl_to_sql_prompt(
            question=request.message,
            schema_info=schema_info,
            dialect=schema_info.get("dialect", "postgresql"),
            conversation_history=history,
        )

        yield _sse_frame(StreamEvent(
            stage=PipelineStage.GENERATING_SQL,
            elapsed_ms=elapsed(),
            data=GeneratingSQLPayload(
                provider=provider,
                model=model,
                prompt_tokens_estimate=len(user_prompt) // 4,  # ~4 chars/token
            ).model_dump(mode="json"),
        ))
        await asyncio.sleep(0)

        llm_response = await llm_client.generate(
            user_prompt=user_prompt,
            system_prompt=NL_TO_SQL_SYSTEM_PROMPT,
        )

        if llm_response.parsed_json is None:
            raise ValueError("LLM returned an invalid / non-JSON response.")

        parsed_json = llm_response.parsed_json
        raw_sql = parsed_json.get("sql", "").strip()
        reasoning = parsed_json.get("reasoning", "")
        confidence = float(parsed_json.get("confidence", 0.0))

        if not raw_sql:
            err_msg = reasoning or "The AI model could not generate SQL for this question because the required tables/columns do not exist in the connected database."
            yield _sse_frame(StreamEvent(
                stage=PipelineStage.ERROR,
                elapsed_ms=elapsed(),
                data=ErrorPayload(
                    stage=error_stage,
                    code="UNANSWERABLE_QUERY",
                    message=err_msg,
                    detail=reasoning,
                ).model_dump(mode="json"),
            ))
            return

    except Exception as exc:
        logger.error("SQL generation failed.", exc_info=exc)
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.ERROR,
            elapsed_ms=elapsed(),
            data=ErrorPayload(
                stage=error_stage,
                code="SQL_GENERATION_FAILED",
                message="The AI model failed to generate a SQL query.",
                detail=str(exc),
            ).model_dump(mode="json"),
        ))
        return

    # ------------------------------------------------------------------
    # Stage 4 – VALIDATING_SQL
    # ------------------------------------------------------------------
    error_stage = PipelineStage.VALIDATING_SQL
    validated_sql = ""
    validation_error = ""
    refinement_attempts = 0

    try:
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.VALIDATING_SQL,
            elapsed_ms=elapsed(),
            data=ValidatingSQLPayload(
                sql_preview=raw_sql[:200],
                checks_run=8,
            ).model_dump(mode="json"),
        ))
        await asyncio.sleep(0)

        validation_result = _validator.validate(raw_sql)

        # Self-correction loop (up to 2 refinement attempts)
        while not validation_result.is_valid and refinement_attempts < 2:
            refinement_attempts += 1
            logger.info(
                "SQL validation failed; attempting self-correction.",
                extra={"attempt": refinement_attempts, "error": validation_result.error},
            )
            refinement_prompt = build_error_refinement_prompt(
                original_question=request.message,
                failed_sql=raw_sql,
                error_message=validation_result.error,
                schema_info=schema_info,
                dialect=schema_info.get("dialect", "postgresql"),
            )
            retry_resp = await llm_client.generate(
                user_prompt=refinement_prompt,
                system_prompt=NL_TO_SQL_SYSTEM_PROMPT,
            )
            if retry_resp.parsed_json:
                raw_sql = retry_resp.parsed_json.get("sql", raw_sql).strip()
                validation_result = _validator.validate(raw_sql)

        if not validation_result.is_valid:
            validation_error = validation_result.error
            raise BadRequestException(
                message=f"SQL failed safety validation after {refinement_attempts} "
                        f"correction attempt(s): {validation_error}"
            )

        validated_sql = validation_result.clean_sql

    except BadRequestException as exc:
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.ERROR,
            elapsed_ms=elapsed(),
            data=ErrorPayload(
                stage=error_stage,
                code="SQL_VALIDATION_FAILED",
                message=exc.message,
                detail=validation_error,
            ).model_dump(mode="json"),
        ))
        return
    except Exception as exc:
        logger.error("SQL validation stage error.", exc_info=exc)
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.ERROR,
            elapsed_ms=elapsed(),
            data=ErrorPayload(
                stage=error_stage,
                code="SQL_VALIDATION_ERROR",
                message="An unexpected error occurred during SQL validation.",
                detail=str(exc),
            ).model_dump(mode="json"),
        ))
        return

    # ------------------------------------------------------------------
    # Stage 5 – EXECUTING
    # ------------------------------------------------------------------
    error_stage = PipelineStage.EXECUTING
    execution_result = None

    try:
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.EXECUTING,
            elapsed_ms=elapsed(),
            data=ExecutingPayload(
                sql_preview=validated_sql[:200],
                timeout_seconds=request.timeout_seconds,
            ).model_dump(mode="json"),
        ))
        await asyncio.sleep(0)

        db_type_str = (
            entry.db_type.value if hasattr(entry.db_type, "value") else str(entry.db_type)
        )

        execution_result = await sql_execution_service.execute(
            engine=entry.engine,
            db_type=db_type_str,
            sql=validated_sql,
            max_rows=request.max_rows,
            timeout_seconds=request.timeout_seconds,
        )

    except ExecutionError as exc:
        logger.error("SQL execution error.", extra={"code": exc.code, "error_msg": exc.message})
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.ERROR,
            elapsed_ms=elapsed(),
            data=ErrorPayload(
                stage=error_stage,
                code=exc.code,
                message=exc.message,
            ).model_dump(mode="json"),
        ))
        return
    except Exception as exc:
        logger.error("Unexpected execution error.", exc_info=exc)
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.ERROR,
            elapsed_ms=elapsed(),
            data=ErrorPayload(
                stage=error_stage,
                code="EXECUTION_FAILED",
                message="An unexpected error occurred during SQL execution.",
                detail=traceback.format_exc()[-500:] if settings.DEBUG else "",
            ).model_dump(mode="json"),
        ))
        return

    # ------------------------------------------------------------------
    # Stage 6 – COMPLETE: persist messages + emit final payload
    # ------------------------------------------------------------------
    error_stage = PipelineStage.COMPLETE
    user_message_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()

    try:
        row_summary = f"{execution_result.row_count} row(s) returned"
        if execution_result.truncated:
            row_summary += f" (truncated at {request.max_rows})"

        answer = (
            f"{row_summary}.\n\n"
            f"```sql\n{validated_sql}\n```"
        )

        token_usage = TokenUsage(
            input_tokens=llm_response.input_tokens if llm_response else 0,
            output_tokens=llm_response.output_tokens if llm_response else 0,
            total_tokens=llm_response.total_tokens if llm_response else 0,
            model=llm_response.model if llm_response else settings.AI_MODEL,
        )

        sql_details = SQLGenerationDetails(
            sql_query=validated_sql,
            reasoning=reasoning,
            confidence=confidence,
            assumptions=parsed_json.get("assumptions", []),
            refinement_attempts=refinement_attempts,
            validation_passed=True,
            validation_error="",
        )

        # Persist to chat history only when a session is linked
        if request.session_id is not None:
            try:
                user_msg = await _message_repo.create(
                    app_db,
                    session_id=request.session_id,
                    role=MessageRole.USER,
                    content=request.message,
                    token_count=token_usage.input_tokens,
                )
                asst_msg = await _message_repo.create(
                    app_db,
                    session_id=request.session_id,
                    role=MessageRole.ASSISTANT,
                    content=answer,
                    token_count=token_usage.output_tokens,
                )
                user_message_id = user_msg.message_id
                assistant_message_id = asst_msg.message_id
                await app_db.commit()
            except Exception as exc:
                # Non-fatal: log the error but still return the result to the user
                logger.error("Failed to persist chat messages.", exc_info=exc)
                await app_db.rollback()

        pipeline_ms = elapsed()

        yield _sse_frame(StreamEvent(
            stage=PipelineStage.COMPLETE,
            elapsed_ms=pipeline_ms,
            data={
                **CompletePayload(
                    session_id=request.session_id or uuid.UUID(int=0),
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    question=request.message,
                    answer=answer,
                    sql_details=sql_details,
                    results=execution_result,
                    token_usage=token_usage,
                    pipeline_ms=pipeline_ms,
                ).model_dump(mode="json"),
                "cache_hit": False,
            },
        ))

        # Cache write-back — store this result so future similar questions hit cache
        if settings.CACHE_ENABLED and execution_result is not None:
            try:
                cache_key = await cache_service.store(
                    question=request.message,
                    connection_id=str(request.connection_id),
                    sql_details=sql_details,
                    query_result=execution_result,
                    token_usage=token_usage,
                    pipeline_ms=pipeline_ms,
                    db=app_db,
                )
                logger.debug("Cache write-back completed.", extra={"cache_key": cache_key})
            except Exception as exc:
                logger.warning("Cache write-back failed (non-fatal): %s", exc)

        logger.info(
            "Query pipeline completed.",
            extra={
                "pipeline_ms": pipeline_ms,
                "row_count": execution_result.row_count,
                "execution_ms": execution_result.execution_ms,
                "refinement_attempts": refinement_attempts,
            },
        )

    except Exception as exc:
        logger.error("Failed to build final response.", exc_info=exc)
        yield _sse_frame(StreamEvent(
            stage=PipelineStage.ERROR,
            elapsed_ms=elapsed(),
            data=ErrorPayload(
                stage=error_stage,
                code="RESPONSE_BUILD_FAILED",
                message="Query succeeded but the response could not be assembled.",
                detail=str(exc),
            ).model_dump(mode="json"),
        ))
