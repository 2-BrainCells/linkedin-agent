from __future__ import annotations

import re
from dataclasses import dataclass, field

from loguru import logger

from agent.config import Settings, load_settings
from agent.llm.client import LLMError, chat, parse_json
from agent.llm.prompts import PARSE_CONTACT_SYSTEM, PARSE_CONTACT_USER_TEMPLATE

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")
_URL_RE = re.compile(r"https?://[^\s)>\"']+")
_TWITTER_RE = re.compile(r"(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})")


@dataclass
class ParsedContact:
    emails: list[str] = field(default_factory=list)
    phone: str = ""
    twitter: str = ""
    website: str = ""

    def merge(self, other: "ParsedContact") -> "ParsedContact":
        emails = list(dict.fromkeys([*self.emails, *other.emails]))
        return ParsedContact(
            emails=emails,
            phone=self.phone or other.phone,
            twitter=self.twitter or other.twitter,
            website=self.website or other.website,
        )


def _regex_pass(raw: str) -> ParsedContact:
    if not raw:
        return ParsedContact()
    emails = list(dict.fromkeys(m.group(0) for m in _EMAIL_RE.finditer(raw)))
    phone_match = _PHONE_RE.search(raw)
    twitter_match = _TWITTER_RE.search(raw)
    url_match = _URL_RE.search(raw)
    return ParsedContact(
        emails=emails,
        phone=phone_match.group(0).strip() if phone_match else "",
        twitter=twitter_match.group(1) if twitter_match else "",
        website=url_match.group(0) if url_match else "",
    )


def _llm_pass(raw: str, settings: Settings) -> ParsedContact:
    if not raw.strip():
        return ParsedContact()
    user = PARSE_CONTACT_USER_TEMPLATE.format(raw=raw[:2000])
    try:
        text = chat(
            model=settings.ollama.parse_model,
            messages=[
                {"role": "system", "content": PARSE_CONTACT_SYSTEM},
                {"role": "user", "content": user},
            ],
            json_mode=True,
            temperature=0.0,
        )
        data = parse_json(text)
    except LLMError as e:
        logger.debug(f"parse_contact LLM skipped: {e}")
        return ParsedContact()
    return ParsedContact(
        emails=[e for e in (data.get("emails") or []) if isinstance(e, str)],
        phone=str(data.get("phone") or ""),
        twitter=str(data.get("twitter") or "").lstrip("@"),
        website=str(data.get("website") or ""),
    )


def extract_contact(raw_text: str, settings: Settings | None = None) -> ParsedContact:
    """Regex pass + LLM augmentation; returns deduplicated ParsedContact."""
    settings = settings or load_settings()
    return _regex_pass(raw_text).merge(_llm_pass(raw_text, settings))


__all__ = ["ParsedContact", "extract_contact"]
