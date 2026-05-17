from __future__ import annotations

import email as _stdlib_email
import imaplib
from datetime import datetime
from email.utils import parseaddr

from loguru import logger

from agent.config import Settings, load_settings


class ReplyCheckError(RuntimeError):
    """Raised when we can't determine reply status (network/auth error)."""


def _imap_date(dt: datetime) -> str:
    # IMAP requires "DD-Mon-YYYY" with English month abbreviation.
    return dt.strftime("%d-%b-%Y")


def _normalize_email(addr: str) -> str:
    name, email_addr = parseaddr(addr or "")
    return (email_addr or addr or "").strip().lower()


def has_reply_from(
    address: str,
    since: datetime,
    *,
    settings: Settings | None = None,
) -> bool:
    """Return True if INBOX contains any message FROM `address` SINCE `since`.

    Raises ReplyCheckError on connection/auth failure — caller decides whether
    to skip the send (safer) or send anyway, per config.followups.reply_detection.on_error.
    """
    settings = settings or load_settings()
    rd = settings.followups.reply_detection
    if not address:
        return False

    pw = settings.secrets.gmail_app_password
    if not pw:
        raise ReplyCheckError("GMAIL_APP_PASSWORD not set; cannot run IMAP reply check.")

    user = settings.secrets.gmail_from_address or settings.email.from_address
    target = _normalize_email(address)

    try:
        with imaplib.IMAP4_SSL(rd.imap_host, rd.imap_port, timeout=30) as imap:
            imap.login(user, pw)
            status, _ = imap.select(rd.mailbox, readonly=True)
            if status != "OK":
                raise ReplyCheckError(f"IMAP select '{rd.mailbox}' failed: {status}")
            since_str = _imap_date(since)
            # IMAP SEARCH FROM/SINCE
            status, data = imap.search(None, "FROM", f'"{target}"',
                                       "SINCE", since_str)
            if status != "OK":
                raise ReplyCheckError(f"IMAP search failed: {status}")
            ids = (data[0] or b"").split()
            if not ids:
                return False
            # Confirm by fetching the From header — IMAP SEARCH FROM matches substrings.
            for msg_id in ids:
                status, msg_data = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
                if status != "OK" or not msg_data:
                    continue
                header_blob = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
                parsed = _stdlib_email.message_from_bytes(header_blob)
                from_addr = _normalize_email(parsed.get("From", ""))
                if from_addr == target:
                    logger.info(f"reply detected from {target}")
                    return True
            return False
    except imaplib.IMAP4.error as e:
        raise ReplyCheckError(f"IMAP error: {e}") from e
    except OSError as e:
        raise ReplyCheckError(f"IMAP network error: {e}") from e


__all__ = ["ReplyCheckError", "has_reply_from"]
