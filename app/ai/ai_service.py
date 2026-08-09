"""
app/ai/ai_service.py
=====================
Full NL-to-SQL pipeline orchestrator.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm_client import LLMResponse, get_llm_client
from app.ai.prompt_templates import (
    NL_TO_SQL_SYSTEM_PROMPT,
    build_error_refinement_prompt,
    build_nl_to_sql_prompt,
)
from app.ai.schema_extractor import SchemaExtractor
from app.ai.sql_validator import SQLValidator
from app.config import settings
from app.core.exceptions import (
    BadRequestException,
    InternalServerException,
    NotFoundException,
)
from app.logger import get_logger
from app.models.chat_session import MessageRole
from app.repositories.chat_repository import ChatMessageRepository, ChatSessionRepository
from app.schemas.query import (
    NLQueryRequest,
    NLQueryResponse,
    QueryResultSet,
    SQLGenerationDetails,
    TokenUsage,
)
from app.utils.connection_manager import ConnectionEntry, connection_manager

logger = get_logger(__name__)

_session_repo = ChatSessionRepository()
_message_repo = ChatMessageRepository()
_schema_extractor = SchemaExtractor()
_validator = SQLValidator()


async def _execute_sql(entry: ConnectionEntry, sql: str) -> QueryResultSet:
    start = time.perf_counter()
    if entry.engine is None:
        raise RuntimeError("No engine available.")

    async with entry.engine.connect() as conn:
        if entry.db_type == "postgresql":
            await conn.execute(text("SET TRANSACTION READ ONLY"))
        result = await conn.execute(text(sql))
        columns = list(result.keys())
        raw_rows = result.fetchmany(settings.MAX_QUERY_RESULTS)

        rows = []
        for row in raw_rows:
            s_row = [str(val) if val is not None else None for val in row]
            rows.append(s_row)

        execution_ms = (time.perf_counter() - start) * 1000
        return QueryResultSet(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=len(raw_rows) == settings.MAX_QUERY_RESULTS,
            execution_ms=round(execution_ms, 2),
        )


class AIQueryService:
    async def process_query(
        self,
        app_db: AsyncSession,
        session_id: uuid.UUID,
        req: NLQueryRequest,
    ) -> NLQueryResponse:
        pipeline_start = time.perf_counter()

        chat_session = await _session_repo.get_active_by_id(app_db, session_id)
        if chat_session is None:
            raise NotFoundException(message=f"Chat session {session_id} not found.")

        if chat_session.connection_id is None:
            raise BadRequestException(message="This chat session has no database connection linked.")

        connection_id = chat_session.connection_id
        entry: ConnectionEntry | None = await connection_manager.get(connection_id)
        if entry is None:
            raise NotFoundException(message=f"Database connection {connection_id} is not active.")

        schema_info = await _schema_extractor.get_schema(connection_id)
        recent_messages, _ = await _message_repo.list_by_session(app_db, session_id, offset=0, limit=6)
        history = [{"role": m.role, "content": m.content[:500]} for m in recent_messages]

        llm_client = get_llm_client()
        user_prompt = build_nl_to_sql_prompt(
            question=req.message,
            schema_info=schema_info,
            dialect=schema_info.get("dialect", "postgresql"),
            conversation_history=history,
        )

        llm_response = await llm_client.generate(user_prompt=user_prompt, system_prompt=NL_TO_SQL_SYSTEM_PROMPT)

        if llm_response.parsed_json is None:
            raise InternalServerException(message="Invalid LLM response format.")

        parsed = llm_response.parsed_json
        raw_sql = parsed.get("sql", "").strip()
        reasoning = parsed.get("reasoning", "")
        confidence = float(parsed.get("confidence", 0.0))

        validation_result = _validator.validate(raw_sql)
        if not validation_result.is_valid:
            raise BadRequestException(message=f"Unsafe SQL generated: {validation_result.error}")

        validated_sql = validation_result.clean_sql
        execution_result = await _execute_sql(entry, validated_sql)

        user_msg = await _message_repo.create(
            app_db,
            session_id=session_id,
            role=MessageRole.USER,
            content=req.message,
            token_count=llm_response.input_tokens,
        )

        answer = f"Found {execution_result.row_count} results.\n\n```sql\n{validated_sql}\n```"
        assistant_msg = await _message_repo.create(
            app_db,
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=answer,
            token_count=llm_response.output_tokens,
        )

        total_ms = round((time.perf_counter() - pipeline_start) * 1000, 1)

        return NLQueryResponse(
            session_id=session_id,
            user_message_id=user_msg.message_id,
            assistant_message_id=assistant_msg.message_id,
            question=req.message,
            answer=answer,
            sql_details=SQLGenerationDetails(
                sql_query=validated_sql,
                reasoning=reasoning,
                confidence=confidence,
                assumptions=parsed.get("assumptions", []),
                refinement_attempts=0,
                validation_passed=True,
                validation_error="",
            ),
            results=execution_result,
            token_usage=TokenUsage(
                input_tokens=llm_response.input_tokens,
                output_tokens=llm_response.output_tokens,
                total_tokens=llm_response.total_tokens,
                model=llm_response.model,
            ),
            pipeline_ms=total_ms,
        )


def get_ai_service() -> AIQueryService:
    return AIQueryService()
