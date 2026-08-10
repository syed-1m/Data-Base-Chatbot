"""
tests/test_stream_query.py
===========================
Tests for the Query Execution Engine streaming endpoint and its services.

Coverage
--------
* ``POST /api/v1/chat/query`` — SSE framing, all pipeline stages,
  error handling for unknown connection, timeout, and SQL validation failure.
* ``SQLExecutionService`` — timeout guard, JSON serialisation, read-only check.
* ``_to_json_safe`` — cell-level serialiser for edge-case Python types.

Test approach
-------------
* The full streaming endpoint is tested via the FastAPI ``TestClient``
  (synchronous) which collects all SSE frames into a list.
* The AI layer (LLM client, schema extractor) is mocked so tests are
  deterministic and run offline.
* The connection_manager singleton is patched using ``unittest.mock``.
* ``SQLExecutionService.execute`` is patched to return a fixed result set.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, date, time as dt_time
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.query import QueryResultSet
from app.schemas.stream import PipelineStage, StreamEvent
from app.services.execution_service import (
    ExecutionError,
    _to_json_safe,
    SQLExecutionService,
)


# ---------------------------------------------------------------------------
# _to_json_safe unit tests
# ---------------------------------------------------------------------------

class TestToJsonSafe:
    """Cell-level serialiser correctness."""

    def test_none(self):
        assert _to_json_safe(None) is None

    def test_bool_true(self):
        assert _to_json_safe(True) is True

    def test_bool_false(self):
        assert _to_json_safe(False) is False

    def test_int(self):
        assert _to_json_safe(42) == 42

    def test_float(self):
        assert _to_json_safe(3.14) == pytest.approx(3.14)

    def test_decimal(self):
        result = _to_json_safe(Decimal("19.99"))
        assert isinstance(result, float)
        assert result == pytest.approx(19.99)

    def test_string(self):
        assert _to_json_safe("hello") == "hello"

    def test_datetime(self):
        dt = datetime(2024, 1, 15, 12, 30, 0)
        result = _to_json_safe(dt)
        assert "2024-01-15" in result
        assert "12:30:00" in result

    def test_date(self):
        d = date(2024, 6, 1)
        assert _to_json_safe(d) == "2024-06-01"

    def test_time(self):
        t = dt_time(9, 5, 30)
        assert "09:05:30" in _to_json_safe(t)

    def test_uuid(self):
        u = uuid.uuid4()
        result = _to_json_safe(u)
        assert result == str(u)

    def test_bytes(self):
        result = _to_json_safe(b"\xde\xad\xbe\xef")
        assert result == "0xdeadbeef"

    def test_list_recursive(self):
        result = _to_json_safe([1, Decimal("2.5"), None])
        assert result == [1, 2.5, None]

    def test_dict_recursive(self):
        result = _to_json_safe({"key": Decimal("9.99")})
        assert result == {"key": 9.99}

    def test_unknown_type_fallback(self):
        class Weird:
            def __repr__(self):
                return "WeirdObject"
        result = _to_json_safe(Weird())
        assert "WeirdObject" in result


# ---------------------------------------------------------------------------
# SQLExecutionService unit tests
# ---------------------------------------------------------------------------

class TestSQLExecutionService:
    """Tests for the execution service timeout and engine checks."""

    @pytest.mark.asyncio
    async def test_raises_on_no_engine(self):
        svc = SQLExecutionService()
        with pytest.raises(ExecutionError) as exc_info:
            await svc.execute(engine=None, db_type="postgresql", sql="SELECT 1")
        assert exc_info.value.code == "NO_ENGINE"

    @pytest.mark.asyncio
    async def test_timeout_raises_execution_error(self):
        import asyncio
        svc = SQLExecutionService()
        mock_engine = MagicMock()

        async def slow_query(*args, **kwargs):
            await asyncio.sleep(10)

        with patch(
            "app.services.execution_service._execute_query_inner",
            side_effect=slow_query,
        ):
            with pytest.raises(ExecutionError) as exc_info:
                await svc.execute(
                    engine=mock_engine,
                    db_type="postgresql",
                    sql="SELECT pg_sleep(10)",
                    timeout_seconds=1,
                )
        assert exc_info.value.code == "EXECUTION_TIMEOUT"

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        svc = SQLExecutionService()
        mock_engine = MagicMock()
        expected = QueryResultSet(
            columns=["id", "name"],
            rows=[[1, "Alice"], [2, "Bob"]],
            row_count=2,
            truncated=False,
            execution_ms=5.2,
        )

        with patch(
            "app.services.execution_service._execute_query_inner",
            new_callable=AsyncMock,
            return_value=expected,
        ):
            result = await svc.execute(
                engine=mock_engine,
                db_type="postgresql",
                sql="SELECT id, name FROM users LIMIT 2",
                max_rows=500,
                timeout_seconds=30,
            )
        assert result.row_count == 2
        assert result.columns == ["id", "name"]
        assert result.truncated is False


# ---------------------------------------------------------------------------
# Stream pipeline tests (via async generator)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_connection_entry():
    """A minimal ConnectionEntry-like object for mocking."""
    entry = MagicMock()
    entry.connection_id = uuid.uuid4()
    entry.db_type = MagicMock()
    entry.db_type.value = "postgresql"
    entry.engine = MagicMock()
    return entry


@pytest.fixture
def mock_query_result():
    return QueryResultSet(
        columns=["count"],
        rows=[[42]],
        row_count=1,
        truncated=False,
        execution_ms=12.5,
    )


@pytest.fixture
def mock_llm_response():
    resp = MagicMock()
    resp.parsed_json = {
        "sql": "SELECT COUNT(*) AS count FROM orders",
        "reasoning": "User asked for count of orders.",
        "confidence": 0.95,
        "assumptions": [],
    }
    resp.input_tokens = 100
    resp.output_tokens = 50
    resp.total_tokens = 150
    resp.model = "gemini-1.5-flash"
    return resp


async def collect_events(gen: AsyncGenerator) -> list[dict]:
    """Collect all SSE frames from an async generator into a list of dicts."""
    events = []
    async for frame in gen:
        # Each frame: "data: {...}\n\n"
        for line in frame.split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


class TestStreamPipeline:
    """Integration tests for the streaming pipeline generator."""

    @pytest.mark.asyncio
    async def test_unknown_connection_emits_error(self):
        from app.schemas.stream import StreamQueryRequest
        from app.services.stream_service import run_query_pipeline

        request = StreamQueryRequest(
            connection_id=uuid.uuid4(),
            message="Show all orders",
        )
        mock_db = AsyncMock()

        with patch(
            "app.services.stream_service.connection_manager.get",
            new_callable=AsyncMock,
            return_value=None,  # Not found
        ):
            events = await collect_events(run_query_pipeline(request, mock_db))

        stages = [e["stage"] for e in events]
        assert PipelineStage.ERROR in stages
        error_event = next(e for e in events if e["stage"] == PipelineStage.ERROR)
        assert error_event["data"]["code"] == "CONNECTION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_full_pipeline_emits_complete(
        self,
        mock_connection_entry,
        mock_llm_response,
        mock_query_result,
    ):
        from app.schemas.stream import StreamQueryRequest
        from app.services.stream_service import run_query_pipeline

        conn_id = mock_connection_entry.connection_id
        request = StreamQueryRequest(
            connection_id=conn_id,
            message="How many orders do we have?",
        )
        mock_db = AsyncMock()

        schema = {
            "dialect": "postgresql",
            "database_name": "chatbot_db",
            "tables": [
                {"name": "orders", "columns": [{"name": "id", "type": "integer", "nullable": True}]}
            ],
        }

        with (
            patch(
                "app.services.stream_service.connection_manager.get",
                new_callable=AsyncMock,
                return_value=mock_connection_entry,
            ),
            patch(
                "app.services.stream_service._schema_extractor.get_schema",
                new_callable=AsyncMock,
                return_value=schema,
            ),
            patch(
                "app.services.stream_service.get_llm_client",
                return_value=MagicMock(
                    generate=AsyncMock(return_value=mock_llm_response)
                ),
            ),
            patch(
                "app.services.stream_service.sql_execution_service.execute",
                new_callable=AsyncMock,
                return_value=mock_query_result,
            ),
        ):
            events = await collect_events(run_query_pipeline(request, mock_db))

        stages = [e["stage"] for e in events]
        assert PipelineStage.RECEIVED in stages
        assert PipelineStage.EXTRACTING_SCHEMA in stages
        assert PipelineStage.GENERATING_SQL in stages
        assert PipelineStage.VALIDATING_SQL in stages
        assert PipelineStage.EXECUTING in stages
        assert PipelineStage.COMPLETE in stages
        assert PipelineStage.ERROR not in stages

        complete_event = next(e for e in events if e["stage"] == PipelineStage.COMPLETE)
        data = complete_event["data"]
        assert data["results"]["row_count"] == 1
        assert "SELECT" in data["sql_details"]["sql_query"].upper()

    @pytest.mark.asyncio
    async def test_execution_timeout_emits_error(
        self,
        mock_connection_entry,
        mock_llm_response,
    ):
        from app.schemas.stream import StreamQueryRequest
        from app.services.stream_service import run_query_pipeline

        request = StreamQueryRequest(
            connection_id=mock_connection_entry.connection_id,
            message="How many orders?",
            timeout_seconds=1,
        )
        mock_db = AsyncMock()

        schema = {
            "dialect": "postgresql",
            "database_name": "chatbot_db",
            "tables": [
                {"name": "orders", "columns": [{"name": "id", "type": "integer", "nullable": True}]}
            ],
        }

        with (
            patch(
                "app.services.stream_service.connection_manager.get",
                new_callable=AsyncMock,
                return_value=mock_connection_entry,
            ),
            patch(
                "app.services.stream_service._schema_extractor.get_schema",
                new_callable=AsyncMock,
                return_value=schema,
            ),
            patch(
                "app.services.stream_service.get_llm_client",
                return_value=MagicMock(
                    generate=AsyncMock(return_value=mock_llm_response)
                ),
            ),
            patch(
                "app.services.stream_service.sql_execution_service.execute",
                new_callable=AsyncMock,
                side_effect=ExecutionError(
                    "Query exceeded the 1s execution timeout.",
                    code="EXECUTION_TIMEOUT",
                ),
            ),
        ):
            events = await collect_events(run_query_pipeline(request, mock_db))

        stages = [e["stage"] for e in events]
        assert PipelineStage.ERROR in stages
        error = next(e for e in events if e["stage"] == PipelineStage.ERROR)
        assert error["data"]["code"] == "EXECUTION_TIMEOUT"


# ---------------------------------------------------------------------------
# SSE frame format test
# ---------------------------------------------------------------------------

class TestSSEFrameFormat:
    """Verify the SSE wire format produced by _sse_frame."""

    def test_frame_format(self):
        from app.services.stream_service import _sse_frame
        event = StreamEvent(stage=PipelineStage.RECEIVED, elapsed_ms=0.5)
        frame = _sse_frame(event)
        assert frame.startswith("data: ")
        assert frame.endswith("\n\n")
        payload = json.loads(frame[6:].strip())
        assert payload["stage"] == "received"
        assert payload["elapsed_ms"] == 0.5

    def test_frame_is_valid_json(self):
        from app.services.stream_service import _sse_frame
        event = StreamEvent(
            stage=PipelineStage.ERROR,
            elapsed_ms=123.4,
            data={"code": "TEST", "message": "oops"},
        )
        frame = _sse_frame(event)
        parsed = json.loads(frame[6:].strip())
        assert parsed["data"]["code"] == "TEST"


# ---------------------------------------------------------------------------
# StreamQueryRequest schema validation tests
# ---------------------------------------------------------------------------

class TestStreamQueryRequestSchema:
    """Validate Pydantic-level constraints on StreamQueryRequest."""

    def test_empty_message_raises(self):
        from pydantic import ValidationError
        from app.schemas.stream import StreamQueryRequest
        with pytest.raises(ValidationError):
            StreamQueryRequest(
                connection_id=uuid.uuid4(),
                message="",  # min_length=1
            )

    def test_message_too_long_raises(self):
        from pydantic import ValidationError
        from app.schemas.stream import StreamQueryRequest
        with pytest.raises(ValidationError):
            StreamQueryRequest(
                connection_id=uuid.uuid4(),
                message="x" * 4001,  # max_length=4000
            )

    def test_invalid_connection_id_raises(self):
        from pydantic import ValidationError
        from app.schemas.stream import StreamQueryRequest
        with pytest.raises(ValidationError):
            StreamQueryRequest(
                connection_id="not-a-uuid",
                message="Give me all users",
            )

    def test_session_id_is_optional(self):
        from app.schemas.stream import StreamQueryRequest
        req = StreamQueryRequest(
            connection_id=uuid.uuid4(),
            message="Give me all users",
        )
        assert req.session_id is None

    def test_timeout_seconds_bounds(self):
        from pydantic import ValidationError
        from app.schemas.stream import StreamQueryRequest
        with pytest.raises(ValidationError):
            StreamQueryRequest(
                connection_id=uuid.uuid4(),
                message="query",
                timeout_seconds=0,  # ge=1
            )
        with pytest.raises(ValidationError):
            StreamQueryRequest(
                connection_id=uuid.uuid4(),
                message="query",
                timeout_seconds=121,  # le=120
            )

    def test_max_rows_bounds(self):
        from pydantic import ValidationError
        from app.schemas.stream import StreamQueryRequest
        with pytest.raises(ValidationError):
            StreamQueryRequest(
                connection_id=uuid.uuid4(),
                message="query",
                max_rows=0,  # ge=1
            )
        with pytest.raises(ValidationError):
            StreamQueryRequest(
                connection_id=uuid.uuid4(),
                message="query",
                max_rows=5001,  # le=5000
            )


# ---------------------------------------------------------------------------
# Session validation tests
# ---------------------------------------------------------------------------

class TestSessionValidation:
    """Tests for Stage 1b — session_id validation added to stream_service."""

    @pytest.mark.asyncio
    async def test_invalid_session_emits_error(self, mock_connection_entry):
        from app.schemas.stream import StreamQueryRequest
        from app.services.stream_service import run_query_pipeline

        request = StreamQueryRequest(
            connection_id=mock_connection_entry.connection_id,
            message="Show all users",
            session_id=uuid.uuid4(),  # Non-existent session
        )
        mock_db = AsyncMock()

        with (
            patch(
                "app.services.stream_service.connection_manager.get",
                new_callable=AsyncMock,
                return_value=mock_connection_entry,
            ),
            patch(
                "app.services.stream_service._session_repo.get_active_by_id",
                new_callable=AsyncMock,
                return_value=None,  # Session not found
            ),
        ):
            events = await collect_events(run_query_pipeline(request, mock_db))

        stages = [e["stage"] for e in events]
        assert PipelineStage.ERROR in stages
        error_event = next(e for e in events if e["stage"] == PipelineStage.ERROR)
        assert error_event["data"]["code"] == "SESSION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_valid_session_passes_validation(
        self, mock_connection_entry, mock_llm_response, mock_query_result
    ):
        from app.schemas.stream import StreamQueryRequest
        from app.services.stream_service import run_query_pipeline

        session_id = uuid.uuid4()
        mock_session = MagicMock()
        mock_session.session_id = session_id
        mock_session.is_active = True

        request = StreamQueryRequest(
            connection_id=mock_connection_entry.connection_id,
            message="How many orders?",
            session_id=session_id,
        )
        mock_db = AsyncMock()

        schema = {
            "dialect": "postgresql",
            "database_name": "testdb",
            "tables": [
                {"name": "orders", "columns": [{"name": "id", "type": "integer", "nullable": False}]}
            ],
        }

        with (
            patch(
                "app.services.stream_service.connection_manager.get",
                new_callable=AsyncMock,
                return_value=mock_connection_entry,
            ),
            patch(
                "app.services.stream_service._session_repo.get_active_by_id",
                new_callable=AsyncMock,
                return_value=mock_session,
            ),
            patch(
                "app.services.stream_service._message_repo.list_by_session",
                new_callable=AsyncMock,
                return_value=([], 0),
            ),
            patch(
                "app.services.stream_service._schema_extractor.get_schema",
                new_callable=AsyncMock,
                return_value=schema,
            ),
            patch(
                "app.services.stream_service.get_llm_client",
                return_value=MagicMock(
                    generate=AsyncMock(return_value=mock_llm_response)
                ),
            ),
            patch(
                "app.services.stream_service.sql_execution_service.execute",
                new_callable=AsyncMock,
                return_value=mock_query_result,
            ),
        ):
            events = await collect_events(run_query_pipeline(request, mock_db))

        stages = [e["stage"] for e in events]
        assert PipelineStage.ERROR not in stages
        assert PipelineStage.COMPLETE in stages

    @pytest.mark.asyncio
    async def test_no_session_id_skips_session_validation(
        self, mock_connection_entry, mock_llm_response, mock_query_result
    ):
        """When session_id is omitted, session validation is skipped entirely."""
        from app.schemas.stream import StreamQueryRequest
        from app.services.stream_service import run_query_pipeline

        request = StreamQueryRequest(
            connection_id=mock_connection_entry.connection_id,
            message="How many records?",
            # session_id intentionally omitted
        )
        mock_db = AsyncMock()

        schema = {
            "dialect": "postgresql",
            "database_name": "testdb",
            "tables": [
                {"name": "orders", "columns": [{"name": "id", "type": "integer", "nullable": False}]}
            ],
        }

        session_repo_mock = AsyncMock()

        with (
            patch(
                "app.services.stream_service.connection_manager.get",
                new_callable=AsyncMock,
                return_value=mock_connection_entry,
            ),
            patch(
                "app.services.stream_service._session_repo.get_active_by_id",
                session_repo_mock,
            ),
            patch(
                "app.services.stream_service._schema_extractor.get_schema",
                new_callable=AsyncMock,
                return_value=schema,
            ),
            patch(
                "app.services.stream_service.get_llm_client",
                return_value=MagicMock(
                    generate=AsyncMock(return_value=mock_llm_response)
                ),
            ),
            patch(
                "app.services.stream_service.sql_execution_service.execute",
                new_callable=AsyncMock,
                return_value=mock_query_result,
            ),
        ):
            events = await collect_events(run_query_pipeline(request, mock_db))

        # Session repo should NOT have been called since session_id is None
        session_repo_mock.assert_not_called()
        stages = [e["stage"] for e in events]
        assert PipelineStage.COMPLETE in stages


# ---------------------------------------------------------------------------
# SQL safety validator tests
# ---------------------------------------------------------------------------

class TestSQLValidatorSafety:
    """Tests for the 8-layer SQL safety validator."""

    def test_select_is_valid(self):
        from app.ai.sql_validator import SQLValidator
        v = SQLValidator()
        result = v.validate("SELECT * FROM users")
        assert result.is_valid is True
        assert result.clean_sql != ""

    def test_drop_table_blocked(self):
        from app.ai.sql_validator import SQLValidator
        v = SQLValidator()
        result = v.validate("DROP TABLE users")
        assert result.is_valid is False
        assert "DROP" in result.error

    def test_delete_blocked(self):
        from app.ai.sql_validator import SQLValidator
        v = SQLValidator()
        result = v.validate("DELETE FROM users WHERE id = 1")
        assert result.is_valid is False
        assert "DELETE" in result.error

    def test_insert_blocked(self):
        from app.ai.sql_validator import SQLValidator
        v = SQLValidator()
        result = v.validate("INSERT INTO users (name) VALUES ('hacker')")
        assert result.is_valid is False

    def test_update_blocked(self):
        from app.ai.sql_validator import SQLValidator
        v = SQLValidator()
        result = v.validate("UPDATE users SET name = 'x' WHERE id = 1")
        assert result.is_valid is False

    def test_multiple_statements_blocked(self):
        from app.ai.sql_validator import SQLValidator
        v = SQLValidator()
        result = v.validate("SELECT 1; SELECT 2")
        assert result.is_valid is False
        assert "multiple" in result.error.lower()

    def test_empty_sql_blocked(self):
        from app.ai.sql_validator import SQLValidator
        v = SQLValidator()
        result = v.validate("")
        assert result.is_valid is False

    def test_dangerous_function_blocked(self):
        from app.ai.sql_validator import SQLValidator
        v = SQLValidator()
        result = v.validate("SELECT pg_read_file('/etc/passwd')")
        assert result.is_valid is False

    @pytest.mark.asyncio
    async def test_sql_validation_failure_emits_error_in_pipeline(
        self, mock_connection_entry
    ):
        """When LLM generates unsafe SQL, the pipeline emits SQL_VALIDATION_FAILED."""
        from app.schemas.stream import StreamQueryRequest
        from app.services.stream_service import run_query_pipeline

        bad_llm_response = MagicMock()
        bad_llm_response.parsed_json = {
            "sql": "DROP TABLE users",   # Forbidden
            "reasoning": "User asked to drop table.",
            "confidence": 0.5,
            "assumptions": [],
        }
        bad_llm_response.input_tokens = 50
        bad_llm_response.output_tokens = 10
        bad_llm_response.total_tokens = 60
        bad_llm_response.model = "gemini-1.5-flash"

        request = StreamQueryRequest(
            connection_id=mock_connection_entry.connection_id,
            message="Drop the users table",
        )
        mock_db = AsyncMock()
        schema = {
            "dialect": "postgresql",
            "database_name": "testdb",
            "tables": [{"name": "users", "columns": [{"name": "id", "type": "integer", "nullable": False}]}],
        }

        with (
            patch(
                "app.services.stream_service.connection_manager.get",
                new_callable=AsyncMock,
                return_value=mock_connection_entry,
            ),
            patch(
                "app.services.stream_service._schema_extractor.get_schema",
                new_callable=AsyncMock,
                return_value=schema,
            ),
            patch(
                "app.services.stream_service.get_llm_client",
                return_value=MagicMock(
                    generate=AsyncMock(return_value=bad_llm_response)
                ),
            ),
        ):
            events = await collect_events(run_query_pipeline(request, mock_db))

        stages = [e["stage"] for e in events]
        assert PipelineStage.ERROR in stages
        error_event = next(e for e in events if e["stage"] == PipelineStage.ERROR)
        assert error_event["data"]["code"] in ("SQL_VALIDATION_FAILED", "SQL_GENERATION_FAILED")


# ---------------------------------------------------------------------------
# Chat history persistence tests
# ---------------------------------------------------------------------------

class TestChatHistoryPersistence:
    """Tests that messages are stored in chat history when session_id is provided."""

    @pytest.mark.asyncio
    async def test_messages_persisted_when_session_provided(
        self, mock_connection_entry, mock_llm_response, mock_query_result
    ):
        from app.schemas.stream import StreamQueryRequest
        from app.services.stream_service import run_query_pipeline

        session_id = uuid.uuid4()
        mock_session = MagicMock()
        mock_session.session_id = session_id
        mock_session.is_active = True

        # Mock created messages returned by repository
        mock_user_msg = MagicMock()
        mock_user_msg.message_id = uuid.uuid4()
        mock_asst_msg = MagicMock()
        mock_asst_msg.message_id = uuid.uuid4()

        request = StreamQueryRequest(
            connection_id=mock_connection_entry.connection_id,
            message="How many users?",
            session_id=session_id,
        )
        mock_db = AsyncMock()
        schema = {
            "dialect": "postgresql",
            "database_name": "testdb",
            "tables": [{"name": "users", "columns": [{"name": "id", "type": "integer", "nullable": False}]}],
        }

        message_create_mock = AsyncMock(side_effect=[mock_user_msg, mock_asst_msg])

        with (
            patch(
                "app.services.stream_service.connection_manager.get",
                new_callable=AsyncMock,
                return_value=mock_connection_entry,
            ),
            patch(
                "app.services.stream_service._session_repo.get_active_by_id",
                new_callable=AsyncMock,
                return_value=mock_session,
            ),
            patch(
                "app.services.stream_service._message_repo.list_by_session",
                new_callable=AsyncMock,
                return_value=([], 0),
            ),
            patch(
                "app.services.stream_service._message_repo.create",
                message_create_mock,
            ),
            patch(
                "app.services.stream_service._schema_extractor.get_schema",
                new_callable=AsyncMock,
                return_value=schema,
            ),
            patch(
                "app.services.stream_service.get_llm_client",
                return_value=MagicMock(
                    generate=AsyncMock(return_value=mock_llm_response)
                ),
            ),
            patch(
                "app.services.stream_service.sql_execution_service.execute",
                new_callable=AsyncMock,
                return_value=mock_query_result,
            ),
        ):
            events = await collect_events(run_query_pipeline(request, mock_db))

        # Verify both user and assistant messages were persisted
        assert message_create_mock.call_count == 2

        stages = [e["stage"] for e in events]
        assert PipelineStage.COMPLETE in stages

        complete_event = next(e for e in events if e["stage"] == PipelineStage.COMPLETE)
        data = complete_event["data"]
        # IDs should match the mocked repo return values
        assert data["user_message_id"] == str(mock_user_msg.message_id)
        assert data["assistant_message_id"] == str(mock_asst_msg.message_id)

    @pytest.mark.asyncio
    async def test_messages_not_persisted_without_session(
        self, mock_connection_entry, mock_llm_response, mock_query_result
    ):
        """When no session_id is given, no messages should be written to the DB."""
        from app.schemas.stream import StreamQueryRequest
        from app.services.stream_service import run_query_pipeline

        request = StreamQueryRequest(
            connection_id=mock_connection_entry.connection_id,
            message="Count all records",
            # No session_id
        )
        mock_db = AsyncMock()
        schema = {
            "dialect": "postgresql",
            "database_name": "testdb",
            "tables": [{"name": "records", "columns": [{"name": "id", "type": "integer", "nullable": False}]}],
        }

        message_create_mock = AsyncMock()

        with (
            patch(
                "app.services.stream_service.connection_manager.get",
                new_callable=AsyncMock,
                return_value=mock_connection_entry,
            ),
            patch(
                "app.services.stream_service._message_repo.create",
                message_create_mock,
            ),
            patch(
                "app.services.stream_service._schema_extractor.get_schema",
                new_callable=AsyncMock,
                return_value=schema,
            ),
            patch(
                "app.services.stream_service.get_llm_client",
                return_value=MagicMock(
                    generate=AsyncMock(return_value=mock_llm_response)
                ),
            ),
            patch(
                "app.services.stream_service.sql_execution_service.execute",
                new_callable=AsyncMock,
                return_value=mock_query_result,
            ),
        ):
            events = await collect_events(run_query_pipeline(request, mock_db))

        # No messages should have been written
        message_create_mock.assert_not_called()
        stages = [e["stage"] for e in events]
        assert PipelineStage.COMPLETE in stages
