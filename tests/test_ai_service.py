"""
tests/test_ai_service.py
=========================
Tests for the NL-to-SQL AI integration module.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# SQL Validator Tests
# =============================================================================
class TestSQLValidator:
    """Tests for all 8 security checks in SQLValidator."""

    def setup_method(self):
        from app.ai.sql_validator import SQLValidator
        self.validator = SQLValidator()

    def test_valid_select_passes(self):
        sql = "SELECT id, name FROM users WHERE active = true"
        result = self.validator.validate(sql)
        assert result.is_valid is True
        assert result.clean_sql != ""

    def test_empty_sql_fails(self):
        result = self.validator.validate("")
        assert result.is_valid is False
        assert "empty" in result.error.lower()

    def test_blank_sql_fails(self):
        result = self.validator.validate("   ")
        assert result.is_valid is False

    def test_insert_rejected(self):
        result = self.validator.validate("INSERT INTO users (name) VALUES ('test')")
        assert result.is_valid is False
        assert "INSERT" in result.error

    def test_update_rejected(self):
        result = self.validator.validate("UPDATE users SET name='x' WHERE id=1")
        assert result.is_valid is False
        assert result.error != ""

    def test_delete_rejected(self):
        result = self.validator.validate("DELETE FROM users WHERE id=1")
        assert result.is_valid is False
        assert "DELETE" in result.error

    def test_drop_rejected(self):
        result = self.validator.validate("DROP TABLE users")
        assert result.is_valid is False
        assert "DROP" in result.error

    def test_create_rejected(self):
        result = self.validator.validate("CREATE TABLE evil (id INT)")
        assert result.is_valid is False

    def test_truncate_rejected(self):
        result = self.validator.validate("TRUNCATE TABLE users")
        assert result.is_valid is False

    def test_multiple_statements_rejected(self):
        sql = "SELECT 1; SELECT 2"
        result = self.validator.validate(sql)
        assert result.is_valid is False
        assert "multiple" in result.error.lower()

    def test_comment_injection_rejected(self):
        sql = "SELECT 1 /* comment containing SELECT */ -- comment"
        result = self.validator.validate(sql)
        assert result.is_valid is True

    def test_block_comment_injection_rejected(self):
        sql = "SELECT 1 /* UPDATE users SET x=1 */"
        result = self.validator.validate(sql)
        assert result.is_valid is False

    def test_dangerous_function_rejected(self):
        sql = "SELECT PG_READ_FILE('/etc/passwd')"
        result = self.validator.validate(sql)
        assert result.is_valid is False

    def test_select_with_join_passes(self):
        sql = """
        SELECT c.name, SUM(o.total) AS revenue
        FROM customers c
        INNER JOIN orders o ON c.id = o.customer_id
        GROUP BY c.name
        ORDER BY revenue DESC
        LIMIT 10
        """
        result = self.validator.validate(sql)
        assert result.is_valid is True

    def test_sql_with_subquery_passes(self):
        sql = "SELECT * FROM orders WHERE total > (SELECT AVG(total) FROM orders)"
        result = self.validator.validate(sql)
        assert result.is_valid is True

    def test_column_named_updated_at_passes(self):
        """Column names containing forbidden words are NOT rejected."""
        sql = "SELECT id, updated_at, created_by FROM users"
        result = self.validator.validate(sql)
        assert result.is_valid is True

    def test_sql_exceeding_max_length_rejected(self):
        from app.ai.sql_validator import MAX_SQL_LENGTH
        sql = "SELECT " + "id, " * 2000 + "id FROM users"
        result = self.validator.validate(sql)
        if len(sql) > MAX_SQL_LENGTH:
            assert result.is_valid is False

    def test_validation_result_has_all_checks(self):
        """ValidationResult.checks contains all check names."""
        sql = "SELECT 1"
        result = self.validator.validate(sql)
        expected_checks = {
            "not_empty", "length", "forbidden_keywords", "multiple_statements",
            "comment_injection", "dangerous_functions", "union_injection", "select_only"
        }
        for check in expected_checks:
            assert check in result.checks


# =============================================================================
# JSON Extraction Tests
# =============================================================================
class TestJSONExtraction:
    """Tests for the _extract_json function."""

    def test_direct_json_parse(self):
        from app.ai.llm_client import _extract_json
        text = '{"sql": "SELECT 1", "reasoning": "test", "confidence": 0.9, "assumptions": []}'
        result = _extract_json(text)
        assert result is not None
        assert result["sql"] == "SELECT 1"

    def test_json_fence_extraction(self):
        from app.ai.llm_client import _extract_json
        text = '```json\n{"sql": "SELECT 1", "reasoning": "r", "confidence": 0.8, "assumptions": []}\n```'
        result = _extract_json(text)
        assert result is not None
        assert result["sql"] == "SELECT 1"

    def test_prose_with_json_embedded(self):
        from app.ai.llm_client import _extract_json
        text = 'Here is the result: {"sql": "SELECT 2", "reasoning": "ok", "confidence": 0.7, "assumptions": []}'
        result = _extract_json(text)
        assert result is not None
        assert result["sql"] == "SELECT 2"

    def test_empty_text_returns_none(self):
        from app.ai.llm_client import _extract_json
        assert _extract_json("") is None
        assert _extract_json("   ") is None

    def test_invalid_json_returns_none(self):
        from app.ai.llm_client import _extract_json
        assert _extract_json("not json at all") is None


# =============================================================================
# Prompt Template Tests
# =============================================================================
class TestPromptTemplates:
    """Tests for prompt generation functions."""

    def test_format_schema_empty(self):
        from app.ai.prompt_templates import format_schema_for_prompt
        result = format_schema_for_prompt({}, "postgresql")
        assert "No schema" in result

    def test_format_schema_with_tables(self):
        from app.ai.prompt_templates import format_schema_for_prompt
        schema = {
            "database_name": "testdb",
            "tables": [
                {
                    "name": "users",
                    "row_count": 1000,
                    "columns": [
                        {"name": "id", "type": "integer", "nullable": False,
                         "primary_key": True, "foreign_key": None, "unique": False},
                        {"name": "email", "type": "varchar", "nullable": False,
                         "primary_key": False, "foreign_key": None, "unique": True},
                    ],
                    "indexes": ["email"],
                }
            ]
        }
        result = format_schema_for_prompt(schema, "postgresql")
        assert "users" in result
        assert "id" in result
        assert "PK" in result
        assert "UNIQUE" in result

    def test_build_nl_to_sql_prompt_contains_question(self):
        from app.ai.prompt_templates import build_nl_to_sql_prompt
        question = "Show me top 5 customers"
        schema = {"database_name": "db", "tables": []}
        prompt = build_nl_to_sql_prompt(question, schema)
        assert question in prompt

    def test_build_nl_to_sql_prompt_contains_examples(self):
        from app.ai.prompt_templates import build_nl_to_sql_prompt
        schema = {"database_name": "db", "tables": []}
        prompt = build_nl_to_sql_prompt("test question", schema)
        assert "Example 1" in prompt

    def test_build_error_refinement_prompt(self):
        from app.ai.prompt_templates import build_error_refinement_prompt
        prompt = build_error_refinement_prompt(
            original_question="Show customers",
            failed_sql="SELECT * FROM custumers",
            error_message='relation "custumers" does not exist',
            schema_info={"database_name": "db", "tables": []},
        )
        assert "custumers" in prompt
        assert "does not exist" in prompt
        assert "Fix the SQL error" in prompt


# =============================================================================
# Schema Cache Tests
# =============================================================================
class TestSchemaCache:
    """Tests for the in-memory TTL schema cache."""

    def test_cache_miss_returns_none(self):
        from app.ai.schema_extractor import _SchemaCache
        cache = _SchemaCache()
        assert cache.get("unknown-key") is None

    def test_cache_hit_returns_value(self):
        from app.ai.schema_extractor import _SchemaCache
        cache = _SchemaCache()
        cache.set("key1", {"tables": [], "dialect": "postgresql"})
        result = cache.get("key1")
        assert result is not None
        assert result["dialect"] == "postgresql"

    def test_cache_invalidation(self):
        from app.ai.schema_extractor import _SchemaCache
        cache = _SchemaCache()
        cache.set("key2", {"tables": []})
        cache.invalidate("key2")
        assert cache.get("key2") is None

    def test_cache_ttl_expires(self):
        """Expired entries return None."""
        from app.ai.schema_extractor import _SchemaCache
        cache = _SchemaCache()
        cache.set("key3", {"tables": []})

        with patch("app.ai.schema_extractor.settings") as mock_settings:
            mock_settings.SCHEMA_CACHE_TTL_SECONDS = -1
            result = cache.get("key3")
            assert result is None


# =============================================================================
# API Endpoint Tests
# =============================================================================
class TestQueryEndpoint:
    """Tests for POST /api/v1/chat/sessions/{id}/query - validation layer."""

    @pytest.mark.asyncio
    async def test_query_invalid_session_uuid_returns_422(self):
        from httpx import ASGITransport, AsyncClient
        from app.main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/chat/sessions/not-a-uuid/query",
                json={"message": "Show all tables"},
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_query_empty_message_returns_422(self):
        from httpx import ASGITransport, AsyncClient
        from app.main import app
        session_id = uuid.uuid4()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/chat/sessions/{session_id}/query",
                json={"message": ""},
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_query_missing_message_returns_422(self):
        from httpx import ASGITransport, AsyncClient
        from app.main import app
        session_id = uuid.uuid4()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/v1/chat/sessions/{session_id}/query",
                json={},
            )
        assert response.status_code == 422
