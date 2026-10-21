"""Upcoming US macro calendar.

Fixed-date events (FOMC) are hard-coded; recurring releases use their standard
schedule rules and are flagged `estimated`. Replace `upcoming_events` with a
calendar API (e.g. Trading Economics, FMP) for exact dates/consensus figures.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

FOMC_DECISIONS = [  # second day of each 2026 meeting (statement 2:00pm ET)
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
]


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, m: int, weekday: int) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _first_business_day(y: int, m: int) -> date:
    d = date(y, m, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _monthly(y: int, m: int) -> list[dict]:
    ev = lambda d, name, t, imp: {"date": d, "event": name, "time": t, "impact": imp, "estimated": True}  # noqa: E731
    return [
        ev(_first_business_day(y, m), "ISM Manufacturing PMI", "10:00", "medium"),
        ev(_nth_weekday(y, m, 4, 1), "Nonfarm Payrolls / Unemployment", "08:30", "high"),
        ev(_nth_weekday(y, m, 2, 2), "CPI (MoM / YoY)", "08:30", "high"),
        ev(_nth_weekday(y, m, 3, 2), "PPI Final Demand", "08:30", "medium"),
        ev(_nth_weekday(y, m, 2, 3) - timedelta(days=1), "Retail Sales", "08:30", "medium"),
        ev(_last_weekday(y, m, 3), "GDP / Durable Goods", "08:30", "medium"),
        ev(_last_weekday(y, m, 4), "PCE Price Index", "08:30", "high"),
        ev(_last_weekday(y, m, 1), "Consumer Confidence", "10:00", "low"),
    ]


def upcoming_events(today: date | None = None, days: int = 21) -> list[dict]:
    today = today or date.today()
    end = today + timedelta(days=days)
    events: list[dict] = []
    y, m = today.year, today.month
    for _ in range(2):
        events += _monthly(y, m)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    events += [{"date": d, "event": "FOMC Rate Decision", "time": "14:00", "impact": "high",
                "estimated": False} for d in FOMC_DECISIONS]
    d = today + timedelta(days=(3 - today.weekday()) % 7)
    while d <= end:
        events.append({"date": d, "event": "Initial Jobless Claims", "time": "08:30",
                       "impact": "low", "estimated": True})
        d += timedelta(weeks=1)

    out = [e for e in events if today <= e["date"] <= end]
    out.sort(key=lambda e: (e["date"], e["time"]))
    for e in out:
        e["days_out"] = (e["date"] - today).days
        e["date"] = e["date"].isoformat()
    return out
