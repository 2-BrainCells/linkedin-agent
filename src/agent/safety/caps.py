from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from agent.config import Settings, load_settings
from agent.db.models import OutreachChannel, OutreachEvent, OutreachStatus, ProfileVisit
from agent.db.session import session_scope


class CapExceeded(Exception):
    """Raised when a daily cap would be exceeded by the next action."""


@dataclass
class CapUsage:
    profile_visits: int
    linkedin_sent: int
    emails_sent: int

    def remaining(self, settings: Settings) -> dict[str, int]:
        c = settings.caps
        return {
            "profile_visits": max(0, c.profile_visits_per_day - self.profile_visits),
            "linkedin_messages": max(0, c.linkedin_messages_per_day - self.linkedin_sent),
            "emails": max(0, c.emails_per_day - self.emails_sent),
        }


def _day_window(settings: Settings) -> tuple[datetime, datetime]:
    tz = settings.delays.working_hours.zoneinfo
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(), end_local.astimezone()


def today_usage(settings: Settings | None = None) -> CapUsage:
    settings = settings or load_settings()
    start, end = _day_window(settings)
    with session_scope() as s:
        visits = s.scalar(
            select(func.count(ProfileVisit.id)).where(
                ProfileVisit.visited_at >= start,
                ProfileVisit.visited_at < end,
            )
        ) or 0
        linkedin_sent = s.scalar(
            select(func.count(OutreachEvent.id)).where(
                OutreachEvent.channel == OutreachChannel.LINKEDIN,
                OutreachEvent.status == OutreachStatus.SENT,
                OutreachEvent.sent_at.isnot(None),
                OutreachEvent.sent_at >= start,
                OutreachEvent.sent_at < end,
            )
        ) or 0
        emails_sent = s.scalar(
            select(func.count(OutreachEvent.id)).where(
                OutreachEvent.channel == OutreachChannel.EMAIL,
                OutreachEvent.status == OutreachStatus.SENT,
                OutreachEvent.sent_at.isnot(None),
                OutreachEvent.sent_at >= start,
                OutreachEvent.sent_at < end,
            )
        ) or 0
    return CapUsage(visits, linkedin_sent, emails_sent)


def assert_under_cap(action: str, settings: Settings | None = None) -> CapUsage:
    """Raises CapExceeded if the next `action` would exceed the daily cap."""
    settings = settings or load_settings()
    usage = today_usage(settings)
    c = settings.caps
    if action == "profile_visit" and usage.profile_visits >= c.profile_visits_per_day:
        raise CapExceeded(
            f"profile visits: {usage.profile_visits}/{c.profile_visits_per_day} for today"
        )
    if action == "linkedin_message" and usage.linkedin_sent >= c.linkedin_messages_per_day:
        raise CapExceeded(
            f"linkedin messages: {usage.linkedin_sent}/{c.linkedin_messages_per_day} for today"
        )
    if action == "email" and usage.emails_sent >= c.emails_per_day:
        raise CapExceeded(
            f"emails: {usage.emails_sent}/{c.emails_per_day} for today"
        )
    return usage


__all__ = ["CapExceeded", "CapUsage", "today_usage", "assert_under_cap"]
