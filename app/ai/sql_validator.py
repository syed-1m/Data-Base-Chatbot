"""
app/ai/sql_validator.py
========================
SQL safety validation layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
import sqlparse

FORBIDDEN_KEYWORDS: frozenset[str] = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
    "TRUNCATE", "REPLACE", "MERGE", "UPSERT", "GRANT", "REVOKE",
    "EXECUTE", "EXEC", "CALL", "DO", "SET", "LOCK", "UNLOCK",
    "RENAME", "SAVEPOINT", "ROLLBACK", "COMMIT",
})

DANGEROUS_FUNCTIONS: frozenset[str] = frozenset({
    "PG_READ_FILE", "PG_WRITE_FILE", "COPY", "LO_IMPORT", "LO_EXPORT",
    "PG_SLEEP", "PG_CANCEL_BACKEND", "PG_TERMINATE_BACKEND",
    "LOAD_FILE", "INTO OUTFILE", "INTO DUMPFILE", "BENCHMARK",
})

MAX_SQL_LENGTH: int = 8000


@dataclass
class ValidationResult:
    is_valid: bool = True
    error: str = ""
    checks: dict[str, bool] = field(default_factory=dict)
    clean_sql: str = ""


class SQLValidationError(Exception):
    def __init__(self, message: str, checks: dict[str, bool] | None = None) -> None:
        super().__init__(message)
        self.checks = checks or {}


def _check_not_empty(sql: str) -> str | None:
    if not sql or not sql.strip():
        return "SQL query is empty."
    return None


def _check_length(sql: str) -> str | None:
    if len(sql) > MAX_SQL_LENGTH:
        return "Generated SQL exceeds maximum allowed length."
    return None


def _check_forbidden_keywords(sql: str) -> str | None:
    sql_upper = sql.upper()
    for keyword in FORBIDDEN_KEYWORDS:
        pattern = rf"\b{re.escape(keyword)}\b"
        if re.search(pattern, sql_upper):
            return f"Security violation: SQL contains forbidden keyword '{keyword}'."
    return None


def _check_multiple_statements(sql: str) -> str | None:
    statements = [s for s in sqlparse.parse(sql) if s.get_type() is not None or str(s).strip()]
    non_empty = [s for s in statements if str(s).strip().rstrip(";").strip()]
    if len(non_empty) > 1:
        return "Security violation: multiple SQL statements detected."
    return None


def _check_comment_injection(sql: str) -> str | None:
    single_line_comments = re.findall(r"--[^\n]*", sql)
    block_comments = re.findall(r"/\*.*?\*/", sql, re.DOTALL)
    all_comment_text = " ".join(single_line_comments + block_comments).upper()
    for keyword in FORBIDDEN_KEYWORDS:
        pattern = rf"\b{re.escape(keyword)}\b"
        if re.search(pattern, all_comment_text):
            return f"Security violation: forbidden keyword '{keyword}' found inside a SQL comment."
    return None


def _check_dangerous_functions(sql: str) -> str | None:
    sql_upper = sql.upper()
    for func in DANGEROUS_FUNCTIONS:
        if func in sql_upper:
            return f"Security violation: dangerous function '{func}' is not permitted."
    return None


def _check_union_injection(sql: str) -> str | None:
    sql_upper = sql.upper()
    if re.search(r"\bUNION\b", sql_upper):
        post_union = sql_upper.split("UNION")[-1]
        for keyword in {"INSERT", "UPDATE", "DELETE", "DROP", "EXEC", "EXECUTE"}:
            if re.search(rf"\b{keyword}\b", post_union):
                return f"Security violation: UNION followed by '{keyword}' is not permitted."
    return None


def _check_select_only(sql: str) -> str | None:
    parsed = sqlparse.parse(sql.strip())
    if not parsed:
        return "Could not parse SQL query."
    stmt_type = parsed[0].get_type()
    if stmt_type != "SELECT":
        return f"Only SELECT statements are permitted. Got: {stmt_type or 'unknown'}."
    return None


class SQLValidator:
    def validate(self, sql: str) -> ValidationResult:
        result = ValidationResult()
        checks: dict[str, bool] = {}

        check_fns = [
            ("not_empty", _check_not_empty),
            ("length", _check_length),
            ("forbidden_keywords", _check_forbidden_keywords),
            ("multiple_statements", _check_multiple_statements),
            ("comment_injection", _check_comment_injection),
            ("dangerous_functions", _check_dangerous_functions),
            ("union_injection", _check_union_injection),
            ("select_only", _check_select_only),
        ]

        for check_name, check_fn in check_fns:
            error = check_fn(sql)
            checks[check_name] = error is None
            if error is not None:
                result.is_valid = False
                result.error = error
                result.checks = checks
                return result

        try:
            clean = sqlparse.format(sql, reindent=True, keyword_case="upper")
        except Exception:
            clean = sql

        result.is_valid = True
        result.clean_sql = clean.strip()
        result.checks = checks
        return result
