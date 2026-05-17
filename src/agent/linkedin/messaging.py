from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from loguru import logger
from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout
from sqlalchemy import select

from agent.config import Settings, load_settings
from agent.db.models import (
    OutreachChannel,
    OutreachEvent,
    OutreachStatus,
    Prospect,
    ProspectStatus,
)
from agent.db.session import session_scope
from agent.linkedin.browser import linkedin_context, open_page
from agent.linkedin.detection import LinkedInBlocked, inspect_page
from agent.llm.personalize import generate_opener
from agent.safety import audit
from agent.safety.caps import CapExceeded, assert_under_cap
from agent.safety.delay import assert_working_hours, human_sleep, type_like_human
from agent.templating import render_file


def compose_linkedin_drafts(settings: Settings | None = None) -> int:
    """Generate opener + render template for every ENRICHED prospect lacking a draft."""
    settings = settings or load_settings()
    tmpl_path = settings.resolve_path(settings.templates.linkedin_message)
    drafted = 0
    with session_scope() as s:
        prospects = list(s.scalars(
            select(Prospect).where(Prospect.status == ProspectStatus.ENRICHED)
        ))
        for p in prospects:
            existing = s.scalar(
                select(OutreachEvent).where(
                    OutreachEvent.prospect_id == p.id,
                    OutreachEvent.channel == OutreachChannel.LINKEDIN,
                )
            )
            if existing:
                continue
            try:
                opener = generate_opener(p, settings=settings)
            except Exception as e:
                logger.warning(f"opener failed for {p.profile_url}: {e}")
                opener = f"Came across your work at {p.current_company or 'your company'} and wanted to reach out."
            body = render_file(
                tmpl_path,
                first_name=p.first_name or p.full_name.split(" ")[0],
                opener=opener,
                from_name=settings.email.from_name or "",
            )
            s.add(OutreachEvent(
                prospect_id=p.id,
                channel=OutreachChannel.LINKEDIN,
                opener=opener,
                rendered_body=body,
                status=OutreachStatus.DRAFTED,
            ))
            p.status = ProspectStatus.COMPOSED
            drafted += 1
    audit.record("compose.linkedin", payload={"drafted": drafted}, dry_run=False)
    return drafted


async def _open_dm_composer(page: Page) -> bool:
    """Click the Message button on the profile; return True if composer opened."""
    candidates = [
        "button:has-text('Message')",
        "main button[aria-label^='Message']",
        "div.pvs-profile-actions button[aria-label^='Message']",
    ]
    for sel in candidates:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                break
        except Exception:
            continue
    else:
        return False
    try:
        await page.wait_for_selector("div.msg-form__contenteditable", timeout=8000)
        return True
    except PWTimeout:
        return False


async def _send_dm(page: Page, body: str) -> bool:
    editor_sel = "div.msg-form__contenteditable"
    try:
        await type_like_human(page, editor_sel, body)
        send_btn = await page.query_selector("button.msg-form__send-button")
        if not send_btn:
            return False
        is_disabled = await send_btn.get_attribute("disabled")
        if is_disabled is not None:
            return False
        await send_btn.click()
        await asyncio.sleep(1.5)
        return True
    except Exception as e:
        logger.warning(f"DM send failed: {e}")
        return False


async def _send_one(ctx: BrowserContext, event_id: int,
                    settings: Settings, dry_run: bool) -> tuple[bool, str]:
    with session_scope() as s:
        event = s.get(OutreachEvent, event_id)
        if not event or event.channel != OutreachChannel.LINKEDIN:
            return False, "event missing"
        prospect = s.get(Prospect, event.prospect_id)
        url = prospect.profile_url
        body = event.rendered_body

    if dry_run:
        with session_scope() as s:
            event = s.get(OutreachEvent, event_id)
            event.status = OutreachStatus.SKIPPED_DRY_RUN
        audit.record("linkedin.send", target=url, payload={"preview": body[:200]}, dry_run=True)
        return True, ""

    page = await open_page(ctx)
    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    await human_sleep(settings.delays.between_messages)
    det = await inspect_page(page)
    if not det.ok:
        raise LinkedInBlocked(det.reason)

    if not await _open_dm_composer(page):
        with session_scope() as s:
            event = s.get(OutreachEvent, event_id)
            event.status = OutreachStatus.FAILED
            event.error_text = "could not open DM composer"
        return False, "no composer"

    ok = await _send_dm(page, body)
    with session_scope() as s:
        event = s.get(OutreachEvent, event_id)
        if ok:
            event.status = OutreachStatus.SENT
            event.sent_at = datetime.now(timezone.utc)
            prospect = s.get(Prospect, event.prospect_id)
            prospect.status = ProspectStatus.LINKEDIN_SENT
        else:
            event.status = OutreachStatus.FAILED
            event.error_text = "send button click failed"
    audit.record("linkedin.send", target=url,
                 payload={"ok": ok, "preview": body[:200]}, dry_run=False)
    return ok, "" if ok else "send failed"


async def send_linkedin_drafts(
    *,
    dry_run: bool = True,
    limit: int | None = None,
    settings: Settings | None = None,
) -> dict:
    settings = settings or load_settings()
    if not dry_run:
        assert_working_hours(settings)

    with session_scope() as s:
        stmt = select(OutreachEvent.id).where(
            OutreachEvent.channel == OutreachChannel.LINKEDIN,
            OutreachEvent.status == OutreachStatus.DRAFTED,
        )
        if limit:
            stmt = stmt.limit(limit)
        ids = list(s.scalars(stmt))

    sent = failed = skipped = 0
    blocked = False
    blocked_reason = ""

    if dry_run:
        # Dry-run path doesn't need to launch a browser at all.
        for eid in ids:
            await _send_one_dryrun(eid)
            sent += 1
        return {"sent": sent, "failed": 0, "skipped": 0, "dry_run": True}

    async with linkedin_context(settings) as ctx:
        for eid in ids:
            try:
                assert_under_cap("linkedin_message", settings)
            except CapExceeded as e:
                logger.info(f"linkedin cap reached: {e}")
                with session_scope() as s:
                    ev = s.get(OutreachEvent, eid)
                    ev.status = OutreachStatus.SKIPPED_CAP
                skipped += 1
                continue
            try:
                ok, _err = await _send_one(ctx, eid, settings, dry_run=False)
            except LinkedInBlocked as e:
                blocked = True
                blocked_reason = str(e)
                audit.record("linkedin.blocked", payload={"reason": blocked_reason},
                             dry_run=False)
                break
            if ok:
                sent += 1
            else:
                failed += 1

    return {"sent": sent, "failed": failed, "skipped": skipped,
            "dry_run": False, "blocked": blocked, "blocked_reason": blocked_reason}


async def _send_one_dryrun(event_id: int) -> None:
    with session_scope() as s:
        event = s.get(OutreachEvent, event_id)
        if not event:
            return
        prospect = s.get(Prospect, event.prospect_id)
        audit.record("linkedin.send", target=prospect.profile_url if prospect else "",
                     payload={"preview": event.rendered_body[:200]}, dry_run=True)
        event.status = OutreachStatus.SKIPPED_DRY_RUN


__all__ = ["compose_linkedin_drafts", "send_linkedin_drafts"]
