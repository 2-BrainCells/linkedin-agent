from __future__ import annotations

import asyncio
import urllib.parse
from dataclasses import dataclass

from loguru import logger
from playwright.async_api import Page

from agent.config import Settings, load_settings
from agent.db.models import Prospect, ProspectStatus
from agent.db.session import session_scope
from agent.linkedin.browser import linkedin_context, open_page
from agent.linkedin.detection import inspect_page
from agent.safety import audit
from agent.safety.delay import human_sleep


@dataclass
class SearchQuery:
    keywords: str
    location: str = ""
    current_company: str = ""
    title: str = ""

    def to_regular_url(self) -> str:
        params = {"keywords": self.keywords}
        if self.location:
            params["origin"] = "FACETED_SEARCH"
        qs = urllib.parse.urlencode(params)
        return f"https://www.linkedin.com/search/results/people/?{qs}"

    def to_sales_nav_url(self) -> str:
        params = {"query": self.keywords}
        qs = urllib.parse.urlencode(params)
        return f"https://www.linkedin.com/sales/search/people?{qs}"


@dataclass
class SearchResult:
    profile_url: str
    full_name: str
    headline: str
    location: str = ""
    current_company: str = ""
    current_title: str = ""


async def _has_sales_navigator(page: Page) -> bool:
    try:
        await page.goto("https://www.linkedin.com/sales/", wait_until="domcontentloaded",
                        timeout=15000)
        url = page.url.lower()
        return "/sales/" in url and "checkpoint" not in url and "upsell" not in url
    except Exception:
        return False


def _clean_profile_url(href: str) -> str:
    if not href:
        return ""
    full = href if href.startswith("http") else f"https://www.linkedin.com{href}"
    parsed = urllib.parse.urlparse(full)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


async def _scrape_regular_results(page: Page, max_results: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    # LinkedIn renders result cards lazily; scroll and collect.
    while len(results) < max_results:
        cards = await page.query_selector_all(
            "ul.reusable-search__entity-result-list > li"
        )
        if not cards:
            cards = await page.query_selector_all("li.reusable-search__result-container")
        for card in cards:
            link = await card.query_selector("a.app-aware-link[href*='/in/']")
            if not link:
                continue
            href = await link.get_attribute("href") or ""
            url = _clean_profile_url(href)
            if any(r.profile_url == url for r in results):
                continue
            name_el = await card.query_selector(
                ".entity-result__title-text a span[aria-hidden='true']"
            )
            full_name = (await name_el.inner_text()).strip() if name_el else ""
            headline_el = await card.query_selector(".entity-result__primary-subtitle")
            headline = (await headline_el.inner_text()).strip() if headline_el else ""
            location_el = await card.query_selector(".entity-result__secondary-subtitle")
            location = (await location_el.inner_text()).strip() if location_el else ""
            if url and full_name:
                results.append(SearchResult(
                    profile_url=url,
                    full_name=full_name,
                    headline=headline,
                    location=location,
                ))
                if len(results) >= max_results:
                    break

        # Try to advance to the next page.
        next_btn = await page.query_selector("button[aria-label='Next']")
        if not next_btn or not await next_btn.is_enabled():
            break
        await next_btn.click()
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1.5)
    return results


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    return (parts[0] if parts else ""), (parts[-1] if len(parts) > 1 else "")


def _upsert(results: list[SearchResult], query_label: str) -> tuple[int, int]:
    inserted = updated = 0
    with session_scope() as s:
        for r in results:
            first, _last = _split_name(r.full_name)
            existing = s.query(Prospect).filter_by(profile_url=r.profile_url).one_or_none()
            if existing:
                existing.full_name = r.full_name or existing.full_name
                existing.first_name = first or existing.first_name
                existing.headline = r.headline or existing.headline
                existing.location = r.location or existing.location
                existing.current_company = r.current_company or existing.current_company
                existing.current_title = r.current_title or existing.current_title
                updated += 1
            else:
                s.add(Prospect(
                    profile_url=r.profile_url,
                    full_name=r.full_name,
                    first_name=first,
                    headline=r.headline,
                    location=r.location,
                    current_company=r.current_company,
                    current_title=r.current_title,
                    search_query=query_label,
                    status=ProspectStatus.DISCOVERED,
                ))
                inserted += 1
    return inserted, updated


async def run_search(query: SearchQuery, settings: Settings | None = None) -> dict:
    settings = settings or load_settings()
    max_results = settings.search.max_results_per_query
    audit.record("search.start", target=query.keywords,
                 payload={"max_results": max_results}, dry_run=False)

    async with linkedin_context(settings) as ctx:
        page = await open_page(ctx)
        use_sales_nav = settings.search.prefer_sales_navigator and await _has_sales_navigator(page)
        target_url = query.to_sales_nav_url() if use_sales_nav else query.to_regular_url()
        logger.info(f"search via {'Sales Navigator' if use_sales_nav else 'regular'}: {target_url}")
        await page.goto(target_url, wait_until="domcontentloaded")
        await human_sleep(settings.delays.between_profile_visits)
        (await inspect_page(page)).raise_if_blocked()
        # Sales Nav DOM differs; for v1 we use the regular scraper for both,
        # which works when Sales Nav falls back to the people-search layout.
        results = await _scrape_regular_results(page, max_results)

    inserted, updated = _upsert(results, query.keywords)
    audit.record("search.done", target=query.keywords,
                 payload={"found": len(results), "inserted": inserted, "updated": updated},
                 dry_run=False)
    return {"found": len(results), "inserted": inserted, "updated": updated,
            "via_sales_nav": use_sales_nav}


__all__ = ["SearchQuery", "SearchResult", "run_search"]
