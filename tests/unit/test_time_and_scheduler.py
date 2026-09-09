from datetime import datetime
from pathlib import Path

from aria.prompts import current_time_context
from aria.services.scheduler import CronSchedule


def test_time_context_contains_current_local_year() -> None:
    assert str(datetime.now().year) in current_time_context()
    assert "CURRENT DATE AND TIME" in current_time_context()


def test_cron_accepts_sunday_as_zero_or_seven() -> None:
    sunday = datetime(2026, 9, 13, 8, 0)
    assert CronSchedule.parse("0 8 * * 0").matches(sunday)
    assert CronSchedule.parse("0 8 * * 7").matches(sunday)


def test_cron_restricted_day_fields_use_standard_or_semantics() -> None:
    schedule = CronSchedule.parse("0 8 1 * 1")
    # September 1, 2026 is Tuesday: day-of-month matches.
    assert schedule.matches(datetime(2026, 9, 1, 8, 0))
    # September 7, 2026 is Monday: weekday matches.
    assert schedule.matches(datetime(2026, 9, 7, 8, 0))
    assert not schedule.matches(datetime(2026, 9, 2, 8, 0))
