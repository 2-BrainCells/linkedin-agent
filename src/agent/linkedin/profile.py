from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger
from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout
from sqlalchemy import select

from agent.config import Settings, load_settings
from agent.db.models import ContactInfo, Prospect, ProfileVisit, ProspectStatus
from agent.db.session import session_scope
from agent.linkedin.browser import linkedin_context, open_page
from agent.linkedin.detection import LinkedInBlocked, inspect_page
from agent.llm.parse_contact import extract_contact
from agent.safety import audit
from agent.safety.caps import CapExceeded, assert_under_cap
from agent.safety.delay import assert_working_hours, human_sleep


@dataclass
class EnrichReport:
    visited: int
    enriched: int
    skipped: int
    blocked: bool = False
    reason: str = ""


async def _scrape_about(page: Page) -> str:
    selectors = [
        "section[data-section='summary'] div.display-flex span[aria-hidden='true']",
        "section.summary div.display-flex span[aria-hidden='true']",
        "div#about ~ div span[aria-hidden='true']",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


async def _open_contact_modal(page: Page) -> str:
    """Click the 'Contact info' link and return the raw modal text."""
    # The contact-info anchor is usually `a#top-card-text-details-contact-info`
    # but LinkedIn has shipped variants. Try a few selectors.
    candidates = [
        "a#top-card-text-details-contact-info",
        "a[data-control-name='contact_see_more']",
        "a[href*='overlay/contact-info']",
        "a:has-text('Contact info')",
    ]
    for sel in candidates:
        try:
            link = await page.query_selector(sel)
            if link:
                await link.click()
                break
        except Exception:
            continue
    else:
        return ""

    try:
        await page.wait_for_selector("section.pv-contact-info, div[role='dialog']", timeout=8000)
    except PWTimeout:
        return ""
    try:
        modal = await page.query_selector("div[role='dialog']")
        if not modal:
            return ""
        text = await modal.inner_text()
        # Close the modal so subsequent profile loads start clean.
        close = await page.query_selector("div[role='dialog'] button[aria-label='Dismiss']")
        if close:
            await close.click()
        return text or ""
    except Exception:
        return ""


async def _enrich_one(ctx: BrowserContext, prospect_id: int,
                      settings: Settings) -> tuple[bool, str]:
    """Visit one profile and write ContactInfo. Returns (success, error)."""
    with session_scope() as s:
        prospect = s.get(Prospect, prospect_id)
        if not prospect:
            return False, "prospect missing"
        url = prospect.profile_url
        name = prospect.full_name

    page = await open_page(ctx)
    visited_at_warning = ""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await human_sleep(settings.delays.between_profile_visits)
        det = await inspect_page(page)
        if not det.ok:
            visited_at_warning = det.reason
            with session_scope() as s:
                s.add(ProfileVisit(prospect_id=prospect_id, profile_url=url,
                                   detected_warning=visited_at_warning))
            raise LinkedInBlocked(det.reason)

        about_text = await _scrape_about(page)
        raw_contact = await _open_contact_modal(page)
        parsed = extract_contact(raw_contact, settings)

        with session_scope() as s:
            prospect = s.get(Prospect, prospect_id)
            prospect.about = about_text or prospect.about
            existing = prospect.contact
            if existing is None:
                existing = ContactInfo(prospect_id=prospect_id)
                s.add(existing)
            existing.emails = parsed.emails
            existing.phone = parsed.phone
            existing.twitter = parsed.twitter
            existing.website = parsed.website
            existing.raw_modal_text = raw_contact[:5000]
            prospect.status = ProspectStatus.ENRICHED
            s.add(ProfileVisit(prospect_id=prospect_id, profile_url=url))

        audit.record("enrich.ok", target=url, payload={
            "name": name, "emails": parsed.emails, "phone": bool(parsed.phone),
        }, dry_run=False)
        return True, ""
    except LinkedInBlocked:
        raise
    except Exception as e:
        logger.warning(f"enrich failed for {url}: {e}")
        return False, str(e)


async def enrich_pending(
    *,
    limit: int | None = None,
    settings: Settings | None = None,
) -> EnrichReport:
    settings = settings or load_settings()
    assert_working_hours(settings)
    remaining = settings.caps.profile_visits_per_day

    with session_scope() as s:
        stmt = select(Prospect.id).where(Prospect.status == ProspectStatus.FILTERED_IN)
        if limit:
            stmt = stmt.limit(limit)
        ids = list(s.scalars(stmt))
    if not ids:
        return EnrichReport(0, 0, 0)

    visited = enriched = skipped = 0
    blocked_reason = ""
    async with linkedin_context(settings) as ctx:
        for pid in ids:
            try:
                usage = assert_under_cap("profile_visit", settings)
            except CapExceeded as e:
                logger.info(f"cap reached: {e}")
                skipped += len(ids) - visited
                break
            try:
                ok, _err = await _enrich_one(ctx, pid, settings)
            except LinkedInBlocked as e:
                blocked_reason = str(e)
                audit.record("enrich.blocked", target=str(pid),
                             payload={"reason": blocked_reason}, dry_run=False)
                return EnrichReport(visited, enriched, skipped + (len(ids) - visited - 1),
                                    blocked=True, reason=blocked_reason)
            visited += 1
            if ok:
                enriched += 1
            else:
                skipped += 1
            await asyncio.sleep(0)
            _ = usage  # silence unused

    return EnrichReport(visited, enriched, skipped, reason=blocked_reason)


__all__ = ["EnrichReport", "enrich_pending"]
