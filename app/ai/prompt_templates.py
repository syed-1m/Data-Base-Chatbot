"""
app/ai/prompt_templates.py
===========================
Prompt engineering templates for Natural Language to SQL conversion.
"""

from __future__ import annotations
from typing import Any

NL_TO_SQL_SYSTEM_PROMPT = """You are an expert SQL assistant integrated into a database chatbot.
Your ONLY job is to convert natural language questions into safe, read-only SQL queries.

## ABSOLUTE RULES:
1. Generate ONLY SELECT statements. NEVER write INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, GRANT, REVOKE.
2. NEVER use multiple statements separated by semicolons.
3. NEVER include comments containing SQL keywords (-- or /* */).
4. Always use fully qualified column names when tables are joined (table.column).
5. If the question cannot be answered, explain in "reasoning" and set "sql" to empty string.
6. NEVER hallucinate table or column names.

## OUTPUT FORMAT:
Respond ONLY with a valid JSON object:
{
    "reasoning": "<explanation>",
    "sql": "<SELECT query or empty string>",
    "confidence": <float 0.0-1.0>,
    "assumptions": ["<assumptions>"]
}
"""


def format_schema_for_prompt(schema_info: dict[str, Any], dialect: str = "postgresql") -> str:
    if not schema_info or not schema_info.get("tables"):
        return "No schema information available."

    lines: list[str] = [
        f"DATABASE: {schema_info.get('database_name', 'unknown')}",
        f"DIALECT: {dialect.upper()}",
        f"TOTAL TABLES: {len(schema_info['tables'])}",
        "",
    ]

    for table in schema_info["tables"]:
        table_name = table["name"]
        row_count = table.get("row_count")
        row_str = f" (~{row_count:,} rows)" if row_count is not None else ""
        lines.append(f"TABLE: {table_name}{row_str}")

        for col in table.get("columns", []):
            col_name = col["name"]
            col_type = col.get("type", "UNKNOWN").upper()
            constraints: list[str] = []

            if col.get("primary_key"):
                constraints.append("PK")
            if col.get("foreign_key"):
                constraints.append(f"FK -> {col['foreign_key']}")
            if not col.get("nullable", True):
                constraints.append("NOT NULL")
            if col.get("unique"):
                constraints.append("UNIQUE")

            constraint_str = "  " + "  ".join(constraints) if constraints else ""
            lines.append(f"  - {col_name:<30} {col_type:<20}{constraint_str}")

        indexes = table.get("indexes", [])
        if indexes:
            index_cols = ", ".join(indexes[:5])
            lines.append(f"  [INDEXES: {index_cols}]")

        lines.append("")

    return "\n".join(lines)


FEW_SHOT_EXAMPLES = """
## EXAMPLES:

Example 1:
User: "How many orders were placed last month?"
Response:
{
    "reasoning": "Count all orders placed in last month.",
    "sql": "SELECT COUNT(*) AS order_count FROM orders WHERE created_at >= DATE_TRUNC('month', NOW() - INTERVAL '1 month')",
    "confidence": 0.95,
    "assumptions": []
}
"""


def build_nl_to_sql_prompt(
    question: str,
    schema_info: dict[str, Any],
    dialect: str = "postgresql",
    conversation_history: list[dict[str, str]] | None = None,
) -> str:
    schema_str = format_schema_for_prompt(schema_info, dialect)
    return f"""## DATABASE SCHEMA:
{schema_str}

{FEW_SHOT_EXAMPLES}

## USER QUESTION:
{question}

Respond ONLY with the JSON object. No markdown.
"""


def build_error_refinement_prompt(
    original_question: str,
    failed_sql: str,
    error_message: str,
    schema_info: dict[str, Any],
    dialect: str = "postgresql",
) -> str:
    schema_str = format_schema_for_prompt(schema_info, dialect)
    return f"""## DATABASE SCHEMA:
{schema_str}

## ORIGINAL QUESTION:
{original_question}

## YOUR PREVIOUS SQL (which caused an error):
{failed_sql}

## DATABASE ERROR:
{error_message}

Fix the SQL error above and respond ONLY with the JSON object:
{{
    "reasoning": "<explain fix>",
    "sql": "<corrected SELECT query>",
    "confidence": 0.9,
    "assumptions": []
}}
"""
