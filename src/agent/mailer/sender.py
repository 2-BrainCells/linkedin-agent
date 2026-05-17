from __future__ import annotations

import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr

from loguru import logger
from sqlalchemy import select

from agent.config import Settings, load_settings
from agent.db.models import (
    ContactInfo,
    OutreachChannel,
    OutreachEvent,
    OutreachStatus,
    Prospect,
    ProspectStatus,
)
from agent.db.session import session_scope
from agent.llm.personalize import generate_opener
from agent.mailer.render import render_email
from agent.safety import audit
from agent.safety.caps import CapExceeded, assert_under_cap


class MissingAppPassword(RuntimeError):
    pass


def compose_email_drafts(settings: Settings | None = None) -> int:
    """For each ENRICHED prospect with at least one email, create draft events
    (one per recipient email)."""
    settings = settings or load_settings()
    drafted = 0
    with session_scope() as s:
        rows = list(s.scalars(
            select(Prospect).join(ContactInfo).where(
                Prospect.status.in_(
                    [ProspectStatus.ENRICHED, ProspectStatus.COMPOSED,
                     ProspectStatus.LINKEDIN_SENT]
                )
            )
        ))
        for p in rows:
            if not p.contact or not p.contact.emails:
                continue
            for addr in p.contact.emails:
                already = s.scalar(
                    select(OutreachEvent).where(
                        OutreachEvent.prospect_id == p.id,
                        OutreachEvent.channel == OutreachChannel.EMAIL,
                        OutreachEvent.recipient_email == addr,
                    )
                )
                if already:
                    continue
                try:
                    opener = generate_opener(p, settings=settings)
                except Exception as e:
                    logger.warning(f"opener failed for {p.profile_url}: {e}")
                    opener = (f"Came across your work at "
                              f"{p.current_company or 'your company'} and wanted to reach out.")
                rendered = render_email(p, opener, settings)
                s.add(OutreachEvent(
                    prospect_id=p.id,
                    channel=OutreachChannel.EMAIL,
                    recipient_email=addr,
                    opener=opener,
                    rendered_subject=rendered.subject,
                    rendered_body=rendered.body,
                    status=OutreachStatus.DRAFTED,
                ))
                drafted += 1
    audit.record("compose.email", payload={"drafted": drafted}, dry_run=False)
    return drafted


def _build_message(*, settings: Settings, to_addr: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    from_address = settings.secrets.gmail_from_address or settings.email.from_address
    msg["From"] = formataddr((settings.email.from_name or "", from_address))
    msg["To"] = to_addr
    msg["Subject"] = subject
    if settings.email.reply_to:
        msg["Reply-To"] = settings.email.reply_to
    msg.set_content(body)
    return msg


def _smtp_send(settings: Settings, msg: EmailMessage) -> None:
    pw = settings.secrets.gmail_app_password
    if not pw:
        raise MissingAppPassword(
            "GMAIL_APP_PASSWORD not set in .env. Generate one at "
            "https://myaccount.google.com/apppasswords"
        )
    user = settings.secrets.gmail_from_address or settings.email.from_address
    ctx = ssl.create_default_context()
    with smtplib.SMTP(settings.email.smtp_host, settings.email.smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ctx)
        smtp.ehlo()
        smtp.login(user, pw)
        smtp.send_message(msg)


def send_email_drafts(
    *,
    dry_run: bool = True,
    limit: int | None = None,
    settings: Settings | None = None,
) -> dict:
    settings = settings or load_settings()

    with session_scope() as s:
        stmt = select(OutreachEvent.id).where(
            OutreachEvent.channel == OutreachChannel.EMAIL,
            OutreachEvent.status == OutreachStatus.DRAFTED,
        )
        if limit:
            stmt = stmt.limit(limit)
        ids = list(s.scalars(stmt))

    sent = failed = skipped = 0
    for eid in ids:
        with session_scope() as s:
            ev = s.get(OutreachEvent, eid)
            if not ev:
                continue
            prospect = s.get(Prospect, ev.prospect_id)
            to_addr = ev.recipient_email
            subject = ev.rendered_subject
            body = ev.rendered_body
            profile_url = prospect.profile_url if prospect else ""

        if dry_run:
            audit.record("email.send", target=to_addr,
                         payload={"subject": subject, "preview": body[:200],
                                  "profile": profile_url}, dry_run=True)
            with session_scope() as s:
                ev = s.get(OutreachEvent, eid)
                ev.status = OutreachStatus.SKIPPED_DRY_RUN
            sent += 1
            continue

        try:
            assert_under_cap("email", settings)
        except CapExceeded as e:
            logger.info(f"email cap reached: {e}")
            with session_scope() as s:
                ev = s.get(OutreachEvent, eid)
                ev.status = OutreachStatus.SKIPPED_CAP
            skipped += 1
            continue

        try:
            msg = _build_message(settings=settings, to_addr=to_addr,
                                 subject=subject, body=body)
            _smtp_send(settings, msg)
        except MissingAppPassword:
            raise
        except Exception as e:
            logger.warning(f"SMTP send to {to_addr} failed: {e}")
            with session_scope() as s:
                ev = s.get(OutreachEvent, eid)
                ev.status = OutreachStatus.FAILED
                ev.error_text = str(e)[:1000]
            audit.record("email.send", target=to_addr,
                         payload={"ok": False, "error": str(e)[:300]}, dry_run=False)
            failed += 1
            continue

        with session_scope() as s:
            ev = s.get(OutreachEvent, eid)
            ev.status = OutreachStatus.SENT
            ev.sent_at = datetime.now(timezone.utc)
            prospect = s.get(Prospect, ev.prospect_id)
            if prospect and prospect.status != ProspectStatus.DONE:
                prospect.status = ProspectStatus.EMAIL_SENT
        audit.record("email.send", target=to_addr,
                     payload={"ok": True, "subject": subject}, dry_run=False)
        sent += 1

    return {"sent": sent, "failed": failed, "skipped": skipped, "dry_run": dry_run}


__all__ = ["MissingAppPassword", "compose_email_drafts", "send_email_drafts"]
