from datetime import datetime, timezone

from agent.mailer.reply_detector import _imap_date, _normalize_email


def test_imap_date_format():
    dt = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    assert _imap_date(dt) == "17-May-2026"


def test_normalize_plain():
    assert _normalize_email("Sam@Example.COM") == "sam@example.com"


def test_normalize_with_name():
    assert _normalize_email("Sam Smith <sam@example.com>") == "sam@example.com"


def test_normalize_blank():
    assert _normalize_email("") == ""
    assert _normalize_email(None) == ""
