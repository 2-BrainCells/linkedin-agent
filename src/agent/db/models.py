from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ProspectStatus(str, enum.Enum):
    DISCOVERED = "discovered"
    FILTERED_IN = "filtered_in"
    FILTERED_OUT = "filtered_out"
    ENRICHED = "enriched"
    COMPOSED = "composed"
    LINKEDIN_SENT = "linkedin_sent"
    EMAIL_SENT = "email_sent"
    DONE = "done"
    FAILED = "failed"


class OutreachChannel(str, enum.Enum):
    LINKEDIN = "linkedin"
    EMAIL = "email"


class OutreachStatus(str, enum.Enum):
    DRAFTED = "drafted"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED_CAP = "skipped_cap"
    SKIPPED_DRY_RUN = "skipped_dry_run"
    SKIPPED_DUPLICATE = "skipped_duplicate"


class Prospect(Base):
    __tablename__ = "prospects"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_url: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(128), default="")
    headline: Mapped[str] = mapped_column(Text, default="")
    current_title: Mapped[str] = mapped_column(String(255), default="")
    current_company: Mapped[str] = mapped_column(String(255), default="")
    location: Mapped[str] = mapped_column(String(255), default="")
    about: Mapped[str] = mapped_column(Text, default="")
    search_query: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[ProspectStatus] = mapped_column(
        Enum(ProspectStatus), default=ProspectStatus.DISCOVERED, index=True
    )
    filter_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    filter_reason: Mapped[str] = mapped_column(Text, default="")
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    contact: Mapped["ContactInfo | None"] = relationship(
        "ContactInfo", back_populates="prospect", uselist=False, cascade="all, delete-orphan"
    )
    events: Mapped[list["OutreachEvent"]] = relationship(
        "OutreachEvent", back_populates="prospect", cascade="all, delete-orphan"
    )


class ContactInfo(Base):
    __tablename__ = "contact_info"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(
        ForeignKey("prospects.id", ondelete="CASCADE"), unique=True
    )
    emails: Mapped[list[str]] = mapped_column(JSON, default=list)
    phone: Mapped[str] = mapped_column(String(64), default="")
    twitter: Mapped[str] = mapped_column(String(255), default="")
    website: Mapped[str] = mapped_column(String(512), default="")
    raw_modal_text: Mapped[str] = mapped_column(Text, default="")
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    prospect: Mapped[Prospect] = relationship("Prospect", back_populates="contact")


class OutreachEvent(Base):
    __tablename__ = "outreach_events"
    __table_args__ = (
        UniqueConstraint(
            "prospect_id", "channel", "recipient_email", name="uq_outreach_dedup"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int] = mapped_column(
        ForeignKey("prospects.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[OutreachChannel] = mapped_column(Enum(OutreachChannel), index=True)
    template_id: Mapped[str] = mapped_column(String(128), default="default")
    recipient_email: Mapped[str] = mapped_column(String(255), default="")
    rendered_subject: Mapped[str] = mapped_column(Text, default="")
    rendered_body: Mapped[str] = mapped_column(Text, default="")
    opener: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[OutreachStatus] = mapped_column(
        Enum(OutreachStatus), default=OutreachStatus.DRAFTED, index=True
    )
    error_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    prospect: Mapped[Prospect] = relationship("Prospect", back_populates="events")


class ProfileVisit(Base):
    """One row per profile page actually opened — used for cap accounting."""

    __tablename__ = "profile_visits"

    id: Mapped[int] = mapped_column(primary_key=True)
    prospect_id: Mapped[int | None] = mapped_column(
        ForeignKey("prospects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    profile_url: Mapped[str] = mapped_column(String(512))
    visited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    detected_warning: Mapped[str] = mapped_column(Text, default="")


class AuditEvent(Base):
    """Append-only log of every meaningful action."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), default="agent")
    action: Mapped[str] = mapped_column(String(128), index=True)
    target: Mapped[str] = mapped_column(String(512), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dry_run: Mapped[bool] = mapped_column(default=True)


__all__ = [
    "Base",
    "Prospect",
    "ContactInfo",
    "OutreachEvent",
    "ProfileVisit",
    "AuditEvent",
    "ProspectStatus",
    "OutreachChannel",
    "OutreachStatus",
]
