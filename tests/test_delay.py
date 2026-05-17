from datetime import datetime, time

from zoneinfo import ZoneInfo

from agent.config import DelayWindow, WorkingHours
from agent.safety.delay import is_within_working_hours, sample_delay


def test_sample_delay_respects_min():
    w = DelayWindow(mean_seconds=2, stdev_seconds=10, min_seconds=5)
    for _ in range(50):
        assert sample_delay(w) >= 5


def test_working_hours_enforce_false():
    wh = WorkingHours(start=time(9), end=time(17), tz="UTC", enforce=False)
    # Any time should pass when not enforcing.
    assert is_within_working_hours(wh, now=datetime(2026, 1, 1, 3, 0, tzinfo=ZoneInfo("UTC")))


def test_working_hours_inside():
    wh = WorkingHours(start=time(9), end=time(17), tz="UTC", enforce=True)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=ZoneInfo("UTC"))
    assert is_within_working_hours(wh, now=now)


def test_working_hours_outside():
    wh = WorkingHours(start=time(9), end=time(17), tz="UTC", enforce=True)
    now = datetime(2026, 1, 1, 22, 0, tzinfo=ZoneInfo("UTC"))
    assert not is_within_working_hours(wh, now=now)
