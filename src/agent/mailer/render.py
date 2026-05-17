from __future__ import annotations

from dataclasses import dataclass

from agent.config import Settings, load_settings
from agent.db.models import Prospect
from agent.templating import render_file


@dataclass
class RenderedEmail:
    subject: str
    body: str


def render_email(prospect: Prospect, opener: str,
                 settings: Settings | None = None) -> RenderedEmail:
    settings = settings or load_settings()
    sig_text = ""
    sig_path = settings.resolve_path(settings.email.signature_file)
    if sig_path.exists():
        sig_text = sig_path.read_text(encoding="utf-8").strip()

    first_name = prospect.first_name or prospect.full_name.split(" ")[0]
    subj = render_file(
        settings.resolve_path(settings.templates.email_subject),
        first_name=first_name,
    ).strip()
    body = render_file(
        settings.resolve_path(settings.templates.email_body),
        first_name=first_name,
        opener=opener,
        signature=sig_text,
    )
    return RenderedEmail(subject=subj, body=body)


__all__ = ["RenderedEmail", "render_email"]
