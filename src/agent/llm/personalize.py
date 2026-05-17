from __future__ import annotations

from agent.config import Settings, load_settings
from agent.db.models import Prospect
from agent.llm.client import chat
from agent.llm.prompts import PERSONALIZE_SYSTEM, PERSONALIZE_USER_TEMPLATE

_DEFAULT_GOAL = (
    "Open a friendly, low-pressure conversation to compare notes on building "
    "AI products. Not a pitch."
)


def generate_opener(
    prospect: Prospect,
    *,
    goal: str | None = None,
    settings: Settings | None = None,
) -> str:
    settings = settings or load_settings()
    about_excerpt = (prospect.about or "")[:600]
    user = PERSONALIZE_USER_TEMPLATE.format(
        goal=(goal or _DEFAULT_GOAL).strip(),
        first_name=prospect.first_name or prospect.full_name.split(" ")[0],
        headline=prospect.headline or "(no headline)",
        about=about_excerpt or "(no about text)",
    )
    text = chat(
        model=settings.ollama.personalize_model,
        messages=[
            {"role": "system", "content": PERSONALIZE_SYSTEM},
            {"role": "user", "content": user},
        ],
        temperature=0.7,
    )
    return text.strip().strip('"').strip("'")


__all__ = ["generate_opener"]
