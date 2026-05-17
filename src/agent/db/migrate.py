"""Tiny ad-hoc migration helper.

We don't use Alembic for v1. This module adds new columns to existing tables
via ALTER TABLE when running against an older SQLite database. SQLAlchemy's
`Base.metadata.create_all` already handles missing tables, but it never adds
new columns to an existing table — so any post-v1 column additions must be
registered here.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import inspect, text

from agent.db.session import get_engine

# (table, column, ddl_fragment)
_COLUMN_ADDITIONS: list[tuple[str, str, str]] = [
    ("outreach_events", "sequence_number", "INTEGER NOT NULL DEFAULT 1"),
    ("outreach_events", "due_at", "DATETIME"),
    ("outreach_events", "parent_event_id", "INTEGER"),
    ("outreach_events", "message_id", "VARCHAR(255) NOT NULL DEFAULT ''"),
]


def migrate() -> dict[str, list[str]]:
    """Add any missing columns to existing tables. Idempotent."""
    engine = get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: dict[str, list[str]] = {}

    with engine.begin() as conn:
        for table, column, ddl in _COLUMN_ADDITIONS:
            if table not in existing_tables:
                continue  # create_all built it with the column already
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column in cols:
                continue
            sql = f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'
            logger.info(f"migrate: {sql}")
            conn.execute(text(sql))
            added.setdefault(table, []).append(column)
    return added


__all__ = ["migrate"]
