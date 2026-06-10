#!/usr/bin/env python3
"""
market_schedule.py
------------------
Check whether today (or a given date) is a NYSE trading day.

Uses pandas_market_calendars when available (full holiday coverage).
Falls back to weekday-only check if not installed.

Usage:
    python scripts/market_schedule.py                  # print status for today
    python scripts/market_schedule.py --check-today    # exit 0 = trading day, 1 = not
    python scripts/market_schedule.py --next           # print next trading day
    python scripts/market_schedule.py --date 2026-07-04  # check a specific date
"""

import sys
from datetime import date, timedelta

try:
    import pandas_market_calendars as mcal
    _NYSE = mcal.get_calendar("NYSE")
    _CALENDAR = "pandas_market_calendars"

    def is_trading_day(d: date = None) -> bool:
        d = d or date.today()
        schedule = _NYSE.schedule(start_date=str(d), end_date=str(d))
        return not schedule.empty

except ImportError:
    _CALENDAR = "weekday-only (install pandas_market_calendars for holiday support)"

    # Hardcode the most common US market holidays as a fallback
    # Update the HOLIDAY_MONTHS set yearly or just install the package
    _KNOWN_HOLIDAYS = {
        # 2026
        date(2026, 1, 1),   # New Year's Day
        date(2026, 1, 19),  # MLK Day
        date(2026, 2, 16),  # Presidents' Day
        date(2026, 4, 3),   # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 7, 3),   # Independence Day (observed)
        date(2026, 9, 7),   # Labor Day
        date(2026, 11, 26), # Thanksgiving
        date(2026, 11, 27), # Black Friday (early close — not full closure, but included)
        date(2026, 12, 24), # Christmas Eve (early close)
        date(2026, 12, 25), # Christmas
    }

    def is_trading_day(d: date = None) -> bool:
        d = d or date.today()
        if d.weekday() >= 5:
            return False
        if d in _KNOWN_HOLIDAYS:
            return False
        return True


def next_trading_day(after: date = None) -> date:
    d = (after or date.today()) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


if __name__ == "__main__":
    args = sys.argv[1:]

    target = date.today()
    if "--date" in args:
        idx = args.index("--date")
        target = date.fromisoformat(args[idx + 1])

    if "--check-today" in args or "--check" in args:
        if is_trading_day(target):
            print(f"[market_schedule] {target} is a trading day.")
            sys.exit(0)
        else:
            print(f"[market_schedule] {target} is NOT a trading day. Skipping.")
            sys.exit(1)

    elif "--next" in args:
        print(next_trading_day())

    else:
        status = "trading day" if is_trading_day(target) else "NOT a trading day"
        print(f"Today  ({target}): {status}")
        print(f"Next trading day: {next_trading_day()}")
        print(f"Calendar source: {_CALENDAR}")
