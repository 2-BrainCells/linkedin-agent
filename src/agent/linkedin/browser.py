from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from loguru import logger
from playwright.async_api import BrowserContext, Page, async_playwright

from agent.config import Settings, load_settings

# Default UA mirrors the latest stable Chrome on Windows; keep updated occasionally.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)


@asynccontextmanager
async def linkedin_context(settings: Settings | None = None) -> AsyncIterator[BrowserContext]:
    """Yield a persistent Chromium context, headed, using the configured profile dir."""
    settings = settings or load_settings()
    profile_dir = settings.resolve_path(settings.linkedin.browser_profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=not settings.linkedin.headed,
            slow_mo=settings.linkedin.slow_mo_ms,
            viewport={"width": 1366, "height": 850},
            user_agent=_DEFAULT_UA,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
            ],
        )
        try:
            yield ctx
        finally:
            await ctx.close()


async def open_page(ctx: BrowserContext) -> Page:
    if ctx.pages:
        return ctx.pages[0]
    return await ctx.new_page()


async def is_logged_in(page: Page) -> bool:
    """Cheap check — visits feed, returns True if not redirected to login."""
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    url = page.url.lower()
    if "/login" in url or "/checkpoint" in url or "/authwall" in url:
        return False
    # Heuristic: feed has the global nav with a profile menu.
    try:
        await page.wait_for_selector("nav[aria-label='Primary Navigation']", timeout=5000)
        return True
    except Exception:
        return False


async def interactive_login(settings: Settings | None = None) -> None:
    """Open a headed window, wait for the user to log in manually."""
    settings = settings or load_settings()
    async with linkedin_context(settings) as ctx:
        page = await open_page(ctx)
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        logger.info(
            "A Chrome window is open. Log in to LinkedIn (handle 2FA if prompted), "
            "then return here. The session will be saved to the profile dir."
        )
        # Poll until they reach the feed or close the window.
        try:
            while True:
                if await is_logged_in(page):
                    logger.success("Logged in. Session persisted.")
                    return
                await page.wait_for_timeout(2000)
        except Exception as e:
            logger.warning(f"Login flow ended: {e}")


__all__ = [
    "linkedin_context",
    "open_page",
    "is_logged_in",
    "interactive_login",
]
