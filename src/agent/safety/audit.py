from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from agent.config import Settings, load_settings
from agent.db.models import AuditEvent
from agent.db.session import session_scope

_LOGGING_CONFIGURED = False


def configure_logging(settings: Settings | None = None) -> None:
    """Wire loguru sinks: console + rolling app.log + audit.log JSON sink."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    settings = settings or load_settings()
    app_log = settings.resolve_path(settings.paths.app_log)
    audit_log = settings.resolve_path(settings.paths.audit_log)
    app_log.parent.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<level>{level: <8}</level> | {message}")
    logger.add(str(app_log), level="DEBUG", rotation="10 MB", retention=5,
               enqueue=True, backtrace=False, diagnose=False)
    logger.add(str(audit_log), level="INFO",
               filter=lambda r: r["extra"].get("audit") is True,
               serialize=True, rotation="10 MB", retention=20, enqueue=True)
    _LOGGING_CONFIGURED = True


def record(
    action: str,
    *,
    target: str = "",
    payload: dict[str, Any] | None = None,
    dry_run: bool = True,
    actor: str = "agent",
) -> None:
    """Append an audit row to the DB and emit a structured loguru entry."""
    payload = payload or {}
    logger.bind(audit=True).info(
        f"audit[{action}] target={target!r} dry_run={dry_run} payload={json.dumps(payload, default=str)}"
    )
    try:
        with session_scope() as s:
            s.add(AuditEvent(
                actor=actor,
                action=action,
                target=target,
                payload=payload,
                dry_run=dry_run,
            ))
    except Exception as e:  # never let audit failure mask the underlying action
        logger.warning(f"audit DB write failed: {e}")


__all__ = ["configure_logging", "record"]
