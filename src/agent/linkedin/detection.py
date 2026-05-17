from __future__ import annotations

from dataclasses import dataclass

from loguru import logger


class LinkedInBlocked(Exception):
    """Raised when LinkedIn shows a restriction, CAPTCHA, or login wall."""


# Substrings that indicate the agent should halt immediately.
_WARNING_PATTERNS = [
    "unusual activity",
    "we've restricted",
    "your account has been restricted",
    "please complete this security check",
    "verify it's you",
    "checkpoint/challenge",
    "you've reached the weekly invitation limit",
    "you've reached the commercial use limit",
    "search results limit",
]

_LOGIN_WALL_URLS = ("/login", "/checkpoint", "/uas/login", "/authwall")


@dataclass
class DetectionResult:
    ok: bool
    reason: str = ""

    def raise_if_blocked(self) -> None:
        if not self.ok:
            raise LinkedInBlocked(self.reason)


async def inspect_page(page) -> DetectionResult:  # noqa: ANN001
    """Inspect a Playwright page for any halt-now signals."""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if any(seg in url.lower() for seg in _LOGIN_WALL_URLS):
        return DetectionResult(False, f"redirected to login/checkpoint: {url}")

    try:
        body_text = (await page.inner_text("body", timeout=2000)).lower()
    except Exception:
        body_text = ""

    for pat in _WARNING_PATTERNS:
        if pat in body_text:
            logger.warning(f"detection: matched pattern '{pat}' on {url}")
            return DetectionResult(False, f"LinkedIn warning detected: {pat!r}")

    # Captcha iframe heuristic
    try:
        captcha = await page.query_selector("iframe[src*='captcha'], div[class*='captcha']")
        if captcha:
            return DetectionResult(False, "captcha element detected")
    except Exception:
        pass

    return DetectionResult(True)


__all__ = ["LinkedInBlocked", "DetectionResult", "inspect_page"]
