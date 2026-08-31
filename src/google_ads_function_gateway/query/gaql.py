"""GAQL construction helpers for deterministic catalogue-owned queries."""

from __future__ import annotations

from collections.abc import Iterable


def quote_gaql_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def in_int_list(field: str, values: Iterable[int]) -> str | None:
    items = tuple(values)
    if not items:
        return None
    return f"{field} IN ({', '.join(str(item) for item in items)})"


def and_where(filters: Iterable[str | None]) -> str:
    clauses = [clause for clause in filters if clause]
    if not clauses:
        return ""
    return "WHERE " + " AND ".join(clauses)


def status_filter(field: str, status: str | None) -> str | None:
    return f"{field} = {status}" if status else None
