"""Chinese-market calendar dates, independent of the host's timezone.

These are civil dates, not exchange trading-day or night-session labels.
Explicit source dates and exchange calendars remain authoritative.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def shanghai_today() -> date:
    return datetime.now(SHANGHAI).date()
