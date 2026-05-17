from __future__ import annotations

import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

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
from agent.mailer.reply_detector import ReplyCheckError, has_reply_from
from agent.safety import audit
from agent.safety.caps import CapExceeded, assert_under_cap
from agent.templating import render_file


class MissingAppPassword(RuntimeError):
    pass


# ────────────────────────────── compose ──────────────────────────────


def compose_email_drafts(settings: Settings | None = None) -> int:
    """For each prospect with at least one email, create an INITIAL draft
    event (sequence_number=1, due_at=None) per recipient address."""
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
                        OutreachEvent.sequence_number == 1,
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
                    sequence_number=1,
                    due_at=None,
                ))
                drafted += 1
    audit.record("compose.email", payload={"drafted": drafted}, dry_run=False)
    return drafted


# ────────────────────────────── followups: scheduling ──────────────────────────────


def _normalize_subject_for_reply(subject: str) -> str:
    """Strip existing 'Re:' prefixes and prepend exactly one."""
    s = (subject or "").strip()
    while s.lower().startswith("re:"):
        s = s[3:].lstrip()
    return f"Re: {s}" if s else "Re:"


def _render_followup_body(prospect: Prospect, opener: str,
                          template_path, settings: Settings) -> str:
    sig_path = settings.resolve_path(settings.email.signature_file)
    sig_text = sig_path.read_text(encoding="utf-8").strip() if sig_path.exists() else ""
    first_name = prospect.first_name or prospect.full_name.split(" ")[0]
    return render_file(
        settings.resolve_path(template_path),
        first_name=first_name,
        opener=opener,
        signature=sig_text,
        from_name=settings.email.from_name or "",
    )


def _schedule_followups(initial_event_id: int, settings: Settings) -> int:
    """Create SCHEDULED followup events for every step in config.followups.email_sequence.
    Each step's `delay_hours` is added to the PREVIOUS step's due_at (or initial sent_at)."""
    if not settings.followups.enabled:
        return 0
    steps = settings.followups.email_sequence
    if not steps:
        return 0

    with session_scope() as s:
        initial = s.get(OutreachEvent, initial_event_id)
        if not initial or initial.sequence_number != 1 or not initial.sent_at:
            return 0
        prospect = s.get(Prospect, initial.prospect_id)
        base_subject = initial.rendered_subject
        reply_subject = _normalize_subject_for_reply(base_subject)

        cumulative = initial.sent_at
        scheduled = 0
        for idx, step in enumerate(steps, start=2):  # sequence_number starts at 2
            cumulative = cumulative + timedelta(hours=step.delay_hours)
            already = s.scalar(
                select(OutreachEvent).where(
                    OutreachEvent.prospect_id == initial.prospect_id,
                    OutreachEvent.channel == OutreachChannel.EMAIL,
                    OutreachEvent.recipient_email == initial.recipient_email,
                    OutreachEvent.sequence_number == idx,
                )
            )
            if already:
                continue
            body = _render_followup_body(prospect, initial.opener,
                                         step.template, settings)
            s.add(OutreachEvent(
                prospect_id=initial.prospect_id,
                channel=OutreachChannel.EMAIL,
                recipient_email=initial.recipient_email,
                opener=initial.opener,
                rendered_subject=reply_subject,
                rendered_body=body,
                status=OutreachStatus.SCHEDULED,
                sequence_number=idx,
                due_at=cumulative,
                parent_event_id=initial.id,
            ))
            scheduled += 1

    audit.record("followups.scheduled", target=str(initial_event_id),
                 payload={"count": scheduled}, dry_run=False)
    return scheduled


def _cascade_skip_replied(parent_event_id: int) -> int:
    """Mark all still-SCHEDULED followups in the same chain as SKIPPED_REPLIED."""
    with session_scope() as s:
        siblings = list(s.scalars(
            select(OutreachEvent).where(
                OutreachEvent.parent_event_id == parent_event_id,
                OutreachEvent.status == OutreachStatus.SCHEDULED,
            )
        ))
        for sib in siblings:
            sib.status = OutreachStatus.SKIPPED_REPLIED
            sib.error_text = "recipient replied — followup skipped"
        return len(siblings)


# ────────────────────────────── SMTP plumbing ──────────────────────────────


def _build_message(
    *,
    settings: Settings,
    to_addr: str,
    subject: str,
    body: str,
    message_id: str,
    in_reply_to: str = "",
) -> EmailMessage:
    msg = EmailMessage()
    from_address = settings.secrets.gmail_from_address or settings.email.from_address
    msg["From"] = formataddr((settings.email.from_name or "", from_address))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
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


# ────────────────────────────── send loop ──────────────────────────────


def _due_event_ids(settings: Settings, *, only_followups: bool = False,
                   limit: int | None = None) -> list[int]:
    """Return IDs of email events ready to send now:
    - DRAFTED (initial), or
    - SCHEDULED with due_at <= now
    Ordered by sequence_number (initials first, then due followups by oldest due)."""
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        stmt = select(OutreachEvent.id).where(
            OutreachEvent.channel == OutreachChannel.EMAIL,
        )
        if only_followups:
            stmt = stmt.where(
                OutreachEvent.status == OutreachStatus.SCHEDULED,
                OutreachEvent.due_at.isnot(None),
                OutreachEvent.due_at <= now,
            )
        else:
            stmt = stmt.where(
                (OutreachEvent.status == OutreachStatus.DRAFTED) |
                (
                    (OutreachEvent.status == OutreachStatus.SCHEDULED) &
                    (OutreachEvent.due_at.isnot(None)) &
                    (OutreachEvent.due_at <= now)
                )
            )
        stmt = stmt.order_by(OutreachEvent.sequence_number.asc(),
                             OutreachEvent.due_at.asc().nullsfirst(),
                             OutreachEvent.id.asc())
        if limit:
            stmt = stmt.limit(limit)
        return list(s.scalars(stmt))


def _reply_check(
    *, recipient: str, parent_event_id: int | None, settings: Settings,
) -> tuple[bool, str]:
    """Returns (should_skip, reason). Only meaningful for sequence > 1.

    - reply detected → (True, "replied")
    - IMAP error + on_error=skip → (True, "imap_error: ...")
    - IMAP error + on_error=send → (False, "imap_error_send_anyway")
    - no reply → (False, "")
    """
    rd = settings.followups.reply_detection
    if not rd.enabled:
        return False, ""
    with session_scope() as s:
        parent = s.get(OutreachEvent, parent_event_id) if parent_event_id else None
        since = parent.sent_at if (parent and parent.sent_at) else datetime.now(timezone.utc)
    try:
        if has_reply_from(recipient, since, settings=settings):
            return True, "replied"
        return False, ""
    except ReplyCheckError as e:
        logger.warning(f"reply check failed for {recipient}: {e}")
        if rd.on_error == "skip":
            return True, f"imap_error: {e}"
        return False, "imap_error_send_anyway"


def send_email_drafts(
    *,
    dry_run: bool = True,
    limit: int | None = None,
    only_followups: bool = False,
    settings: Settings | None = None,
) -> dict:
    settings = settings or load_settings()

    ids = _due_event_ids(settings, only_followups=only_followups, limit=limit)

    sent = failed = skipped = replied = 0
    scheduled_total = 0

    for eid in ids:
        with session_scope() as s:
            ev = s.get(OutreachEvent, eid)
            if not ev:
                continue
            prospect = s.get(Prospect, ev.prospect_id)
            to_addr = ev.recipient_email
            subject = ev.rendered_subject
            body = ev.rendered_body
            seq = ev.sequence_number
            parent_id = ev.parent_event_id
            profile_url = prospect.profile_url if prospect else ""
            # snapshot parent message-id (for threading) while session is open
            parent_message_id = ""
            if parent_id:
                parent_ev = s.get(OutreachEvent, parent_id)
                if parent_ev:
                    parent_message_id = parent_ev.message_id

        # ── dry-run path
        if dry_run:
            audit.record("email.send", target=to_addr,
                         payload={"subject": subject, "preview": body[:200],
                                  "sequence": seq, "profile": profile_url,
                                  "in_reply_to": parent_message_id or None},
                         dry_run=True)
            with session_scope() as s:
                ev = s.get(OutreachEvent, eid)
                ev.status = OutreachStatus.SKIPPED_DRY_RUN
            sent += 1
            continue

        # ── reply check (followups only)
        if seq > 1:
            should_skip, reason = _reply_check(
                recipient=to_addr, parent_event_id=parent_id, settings=settings,
            )
            if should_skip and reason == "replied":
                # cascade: mark this AND all later siblings
                count = _cascade_skip_replied(parent_id) if parent_id else 0
                audit.record("email.skipped_replied", target=to_addr,
                             payload={"sequence": seq, "cascaded": count},
                             dry_run=False)
                replied += 1
                continue
            if should_skip:  # imap_error with on_error=skip
                logger.info(f"skipping followup to {to_addr} due to {reason}; "
                            "will retry next run")
                # Leave the row as SCHEDULED so the next run retries.
                skipped += 1
                continue

        # ── cap check
        try:
            assert_under_cap("email", settings)
        except CapExceeded as e:
            logger.info(f"email cap reached: {e}")
            with session_scope() as s:
                ev = s.get(OutreachEvent, eid)
                # don't bump status — leave SCHEDULED/DRAFTED for next run
                if ev.status == OutreachStatus.DRAFTED:
                    ev.status = OutreachStatus.SKIPPED_CAP
                ev.error_text = str(e)
            skipped += 1
            continue

        # ── send
        message_id = make_msgid(domain=(to_addr.split("@", 1)[-1] or "localhost"))
        try:
            msg = _build_message(
                settings=settings, to_addr=to_addr, subject=subject,
                body=body, message_id=message_id, in_reply_to=parent_message_id,
            )
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
                         payload={"ok": False, "error": str(e)[:300],
                                  "sequence": seq}, dry_run=False)
            failed += 1
            continue

        with session_scope() as s:
            ev = s.get(OutreachEvent, eid)
            ev.status = OutreachStatus.SENT
            ev.sent_at = datetime.now(timezone.utc)
            ev.message_id = message_id
            prospect = s.get(Prospect, ev.prospect_id)
            if prospect:
                if seq == 1 and prospect.status != ProspectStatus.DONE:
                    prospect.status = ProspectStatus.EMAIL_SENT

        audit.record("email.send", target=to_addr,
                     payload={"ok": True, "subject": subject,
                              "sequence": seq, "message_id": message_id},
                     dry_run=False)
        sent += 1

        # ── schedule followups after a successful initial send
        if seq == 1 and settings.followups.enabled:
            scheduled_total += _schedule_followups(eid, settings)

    return {
        "sent": sent, "failed": failed, "skipped": skipped,
        "replied": replied, "scheduled_followups": scheduled_total,
        "dry_run": dry_run,
    }


__all__ = [
    "MissingAppPassword",
    "compose_email_drafts",
    "send_email_drafts",
]
