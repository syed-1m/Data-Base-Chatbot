"""
app/services/execution_service.py
===================================
Pure SQL Execution Service — fully decoupled from the AI pipeline.

Responsibilities
----------------
* Open a **read-only** transaction against the target DB engine.
* Apply an **asyncio timeout** guard so a rogue query cannot block the server.
* Paginate results via ``fetchmany`` to protect memory on huge result sets.
* Serialise every cell to a JSON-safe Python value (dates, decimals, UUIDs …).
* Measure wall-clock execution time to the millisecond.
* Propagate database errors as structured ``ExecutionError`` exceptions so the
  streaming layer can forward them as ``error`` SSE frames.

Design decisions
----------------
* **No ORM** – this service executes raw SQL through the SQLAlchemy
  ``engine.connect()`` context manager, not the async session used by the
  application's own tables.  This keeps execution fully separated from app
  transactions.
* **Read-only enforcement** – PostgreSQL supports ``SET TRANSACTION READ ONLY``
  which is a server-side guard *on top of* our SQL validator.  MySQL uses
  ``SET SESSION TRANSACTION READ ONLY``.  MongoDB uses ``find`` / aggregation,
  which are already read-only by nature.
* **asyncio.wait_for** – wraps the inner coroutine so any query exceeding
  ``timeout_seconds`` raises ``asyncio.TimeoutError``, translated to a
  user-friendly ``ExecutionError``.
* **JSON serialisation** – Python ``datetime``, ``Decimal``, ``UUID``, ``bytes``
  objects are stringified; unknown types fall back to ``repr()``.
"""

from __future__ import annotations

import asyncio
import decimal
import uuid
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.logger import get_logger
from app.schemas.query import QueryResultSet

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class ExecutionError(Exception):
    """Raised when SQL execution fails for any reason."""

    def __init__(self, message: str, code: str = "EXECUTION_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------

def _to_json_safe(value: Any) -> Any:
    """
    Convert a database cell value to a JSON-serialisable Python primitive.

    Rules (checked in priority order):
      None        → None
      bool        → bool   (must come before int check – bool is a subclass of int)
      int / float → number
      Decimal     → float  (precision preserved up to Python float limits)
      str         → str
      datetime    → ISO-8601 string with timezone
      date        → ISO-8601 date string
      time        → HH:MM:SS string
      UUID        → hyphenated hex string
      bytes       → hex string prefixed with "0x"
      list / tuple→ recursively serialised list
      dict        → recursively serialised dict
      *           → repr() fallback
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value) if isinstance(value, memoryview) else value
        return "0x" + raw.hex()
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    # Final fallback – keeps result set JSON-serialisable at all times
    return repr(value)


def _serialise_row(row: Any) -> list[Any]:
    """Serialise one result row (tuple-like) into a list of JSON-safe values."""
    return [_to_json_safe(cell) for cell in row]


# ---------------------------------------------------------------------------
# Read-only transaction helpers
# ---------------------------------------------------------------------------

async def _set_read_only_postgresql(conn: Any) -> None:
    try:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
    except Exception as exc:  # pragma: no cover – best-effort guard
        logger.warning("Could not set READ ONLY on PostgreSQL transaction.", exc_info=exc)


async def _set_read_only_mysql(conn: Any) -> None:
    try:
        await conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
    except Exception as exc:
        logger.warning("Could not set READ ONLY on MySQL session.", exc_info=exc)


# ---------------------------------------------------------------------------
# Core execution coroutine
# ---------------------------------------------------------------------------

async def _execute_query_inner(
    engine: AsyncEngine,
    db_type: str,
    sql: str,
    max_rows: int,
) -> QueryResultSet:
    """
    Run ``sql`` against ``engine`` inside a read-only transaction.

    Returns a ``QueryResultSet``; raises ``ExecutionError`` on DB errors.
    """
    import time as _time

    wall_start = _time.perf_counter()

    try:
        async with engine.begin() as conn:  # auto-rollback on __aexit__
            # Apply dialect-specific read-only guard
            if db_type == "postgresql":
                await _set_read_only_postgresql(conn)
            elif db_type == "mysql":
                await _set_read_only_mysql(conn)

            result = await conn.execute(text(sql))
            columns: list[str] = list(result.keys())

            # fetchmany caps memory usage – never load the entire result set
            raw_rows = result.fetchmany(max_rows + 1)  # +1 to detect truncation

        truncated = len(raw_rows) > max_rows
        rows_to_return = raw_rows[:max_rows]

        serialised = [_serialise_row(row) for row in rows_to_return]
        execution_ms = round((_time.perf_counter() - wall_start) * 1000, 2)

        logger.info(
            "SQL executed successfully.",
            extra={
                "db_type": db_type,
                "row_count": len(serialised),
                "truncated": truncated,
                "execution_ms": execution_ms,
            },
        )

        return QueryResultSet(
            columns=columns,
            rows=serialised,
            row_count=len(serialised),
            truncated=truncated,
            execution_ms=execution_ms,
        )

    except ExecutionError:
        raise
    except Exception as exc:
        logger.error("SQL execution failed.", exc_info=exc)
        raise ExecutionError(
            message=str(exc),
            code="QUERY_FAILED",
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SQLExecutionService:
    """
    Stateless service that executes validated SQL on a target engine.

    Usage::

        svc = SQLExecutionService()
        result = await svc.execute(
            engine=entry.engine,
            db_type="postgresql",
            sql="SELECT * FROM orders LIMIT 10",
            max_rows=500,
            timeout_seconds=30,
        )
    """

    async def execute(
        self,
        engine: AsyncEngine,
        db_type: str,
        sql: str,
        max_rows: int = 500,
        timeout_seconds: int = 30,
    ) -> QueryResultSet:
        """
        Execute ``sql`` with a hard wall-clock timeout.

        Parameters
        ----------
        engine : AsyncEngine
            The SQLAlchemy async engine for the target database.
        db_type : str
            Dialect tag: ``"postgresql"``, ``"mysql"``.
        sql : str
            Pre-validated SQL string (only SELECT statements reach here).
        max_rows : int
            Maximum rows to fetch. Rows beyond this limit are discarded and
            ``QueryResultSet.truncated`` is set to ``True``.
        timeout_seconds : int
            Wall-clock execution budget.  Exceeded → ``ExecutionError`` with
            code ``EXECUTION_TIMEOUT``.

        Returns
        -------
        QueryResultSet
            Columns, serialised rows, metadata.

        Raises
        ------
        ExecutionError
            On timeout, DB error, or serialisation failure.
        """
        if engine is None:
            raise ExecutionError(
                message="No SQLAlchemy engine available for this connection.",
                code="NO_ENGINE",
            )

        try:
            result = await asyncio.wait_for(
                _execute_query_inner(engine, db_type, sql, max_rows),
                timeout=float(timeout_seconds),
            )
        except asyncio.TimeoutError:
            logger.error(
                "SQL execution timed out.",
                extra={"timeout_seconds": timeout_seconds, "db_type": db_type},
            )
            raise ExecutionError(
                message=f"Query exceeded the {timeout_seconds}s execution timeout.",
                code="EXECUTION_TIMEOUT",
            )
        return result


# Singleton – import and use directly
sql_execution_service = SQLExecutionService()
