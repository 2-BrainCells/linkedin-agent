from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select

from agent.config import Settings, load_settings
from agent.db.models import Prospect, ProspectStatus
from agent.db.session import session_scope
from agent.llm.client import LLMError, chat, parse_json
from agent.llm.prompts import FILTER_SYSTEM, FILTER_USER_TEMPLATE
from agent.safety import audit


@dataclass
class FilterReport:
    evaluated: int
    kept: int
    dropped: int
    errors: int


def _score_one(model: str, prospect: Prospect, criteria: str) -> tuple[float, bool, str]:
    user = FILTER_USER_TEMPLATE.format(
        criteria=criteria.strip(),
        name=prospect.full_name,
        headline=prospect.headline or "(empty)",
        current_title=prospect.current_title or "(unknown)",
        current_company=prospect.current_company or "(unknown)",
    )
    raw = chat(
        model=model,
        messages=[
            {"role": "system", "content": FILTER_SYSTEM},
            {"role": "user", "content": user},
        ],
        json_mode=True,
        temperature=0.1,
    )
    data = parse_json(raw)
    score = float(data.get("score", 0))
    keep = bool(data.get("keep", False))
    reason = str(data.get("reason", ""))[:500]
    return score, keep, reason


def run_filter(
    criteria: str | None = None,
    *,
    limit: int | None = None,
    settings: Settings | None = None,
) -> FilterReport:
    settings = settings or load_settings()
    criteria = (criteria or settings.filter.criteria).strip()
    if not criteria:
        raise ValueError("No filter criteria provided (config or --criteria-file).")

    min_score = settings.filter.min_score
    model = settings.ollama.filter_model

    evaluated = kept = dropped = errors = 0
    with session_scope() as s:
        stmt = select(Prospect).where(Prospect.status == ProspectStatus.DISCOVERED)
        if limit:
            stmt = stmt.limit(limit)
        prospects = list(s.scalars(stmt))

        for p in prospects:
            evaluated += 1
            try:
                score, keep, reason = _score_one(model, p, criteria)
            except LLMError as e:
                errors += 1
                logger.warning(f"filter LLM error on {p.profile_url}: {e}")
                continue
            p.filter_score = score
            p.filter_reason = reason
            if keep and score >= min_score:
                p.status = ProspectStatus.FILTERED_IN
                kept += 1
            else:
                p.status = ProspectStatus.FILTERED_OUT
                dropped += 1

    audit.record("filter.done", payload={
        "evaluated": evaluated, "kept": kept, "dropped": dropped,
        "errors": errors, "model": model, "min_score": min_score,
    }, dry_run=False)
    return FilterReport(evaluated, kept, dropped, errors)


__all__ = ["FilterReport", "run_filter"]
