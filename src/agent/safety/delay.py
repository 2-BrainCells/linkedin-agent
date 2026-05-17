from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta

from loguru import logger

from agent.config import DelayWindow, Settings, WorkingHours, load_settings


class OutsideWorkingHours(Exception):
    pass


def sample_delay(window: DelayWindow) -> float:
    """Gaussian-sampled delay, floored at window.min_seconds."""
    val = random.gauss(window.mean_seconds, window.stdev_seconds)
    return max(window.min_seconds, val)


async def human_sleep(window: DelayWindow) -> float:
    secs = sample_delay(window)
    logger.debug(f"sleeping {secs:.1f}s (mean={window.mean_seconds})")
    await asyncio.sleep(secs)
    return secs


def is_within_working_hours(wh: WorkingHours, now: datetime | None = None) -> bool:
    if not wh.enforce:
        return True
    now = now or datetime.now(wh.zoneinfo)
    local = now.astimezone(wh.zoneinfo).time()
    return wh.start <= local <= wh.end


def assert_working_hours(settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    wh = settings.delays.working_hours
    if not is_within_working_hours(wh):
        raise OutsideWorkingHours(
            f"Outside working hours ({wh.start.isoformat()}–{wh.end.isoformat()} {wh.tz})."
            " Re-run inside the window or set delays.working_hours.enforce: false in config."
        )


async def wait_for_working_hours(settings: Settings | None = None) -> None:
    """Sleep until working hours open. No-op if currently inside."""
    settings = settings or load_settings()
    wh = settings.delays.working_hours
    if not wh.enforce or is_within_working_hours(wh):
        return
    now = datetime.now(wh.zoneinfo)
    today_start = now.replace(
        hour=wh.start.hour, minute=wh.start.minute, second=0, microsecond=0
    )
    target = today_start if now < today_start else today_start + timedelta(days=1)
    seconds = (target - now).total_seconds()
    logger.info(f"waiting {seconds/60:.1f} min until working hours open at {target}")
    await asyncio.sleep(seconds)


async def type_like_human(page, selector: str, text: str) -> None:  # noqa: ANN001
    """Type into a Playwright element with per-character random delays."""
    element = page.locator(selector)
    await element.click()
    for ch in text:
        await element.type(ch, delay=random.uniform(40, 140))
    await asyncio.sleep(random.uniform(0.4, 1.1))


__all__ = [
    "OutsideWorkingHours",
    "sample_delay",
    "human_sleep",
    "is_within_working_hours",
    "assert_working_hours",
    "wait_for_working_hours",
    "type_like_human",
]
