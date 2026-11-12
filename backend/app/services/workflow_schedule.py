"""Calendar maths for ``trigger.schedule`` — pure, deterministic, no clock reads, no database.

A schedule config (the trigger node's ``data``)::

    {"cadence": "monthly" | "weekly" | "once",
     "day": 1..31 (monthly) | 0..6 Mon..Sun (weekly),
     "last_day": true            # monthly: always the last day of the month
     "date": "2026-12-03",       # once
     "time": "09:00",            # local wall-clock, 24h
     "timezone": "Asia/Kolkata", # IANA name; default Asia/Kolkata
     "weekend_rule": "as_is" | "previous_business_day" | "next_business_day",
     "employer_id": "<uuid>", "fan_out": "org" | "employers"}

Rules
-----
* Monthly ``day`` N in a month shorter than N means the LAST day of that month (Feb 30 -> Feb 28/29).
* The weekend rule moves an occurrence that lands on Saturday/Sunday to the previous Friday or the next Monday.
  Public holidays are NOT modelled.
* Legacy configs without ``time`` / ``timezone`` keep their old meaning: the whole UTC day (00:00 UTC, 24h window).
* An occurrence is identified by its UTC instant; :func:`occurrence_key` is the dedupe suffix used by the engine.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Kolkata"
CADENCES = ("monthly", "weekly", "once")
WEEKEND_RULES = ("as_is", "previous_business_day", "next_business_day")
CATCH_UP_WINDOW = timedelta(hours=24)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class ScheduleConfigError(ValueError):
    """A schedule config problem with a user-facing message and the offending field."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(message)


def get_tz(name: str | None) -> tzinfo:
    """IANA zone by name. Asia/Kolkata has a fixed +05:30 fallback when tz data is missing (it has no DST)."""
    zone = (name or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if zone == "Asia/Kolkata":
            from datetime import timezone

            return timezone(timedelta(hours=5, minutes=30), "Asia/Kolkata")
        if zone in ("UTC", "Etc/UTC"):
            return UTC
        raise ScheduleConfigError("timezone", f"Unknown timezone '{zone}'. Use an IANA name such as Asia/Kolkata "
                                              "or Europe/Berlin.") from None


def _is_legacy(data: dict) -> bool:
    return not data.get("time") and not data.get("timezone")


def parse_config(data: dict) -> dict:
    """Validate + normalise a schedule config. Raises :class:`ScheduleConfigError` with a friendly message."""
    cadence = data.get("cadence", data.get("recurrence"))
    if cadence not in CADENCES:
        raise ScheduleConfigError("cadence", "Choose how often it runs: monthly, weekly or on one specific date.")
    legacy = _is_legacy(data)
    tz_name = "UTC" if legacy else (data.get("timezone") or DEFAULT_TIMEZONE)
    tz = get_tz(tz_name)
    hh, mm = 0, 0
    if not legacy:
        time_text = data.get("time") or "09:00"
        match = _TIME_RE.match(str(time_text))
        if not match:
            raise ScheduleConfigError("time", "Time of day must look like 09:00 (24-hour).")
        hh, mm = int(match.group(1)), int(match.group(2))
    rule = data.get("weekend_rule") or "as_is"
    if rule not in WEEKEND_RULES:
        raise ScheduleConfigError("weekend_rule", "Weekend rule must be: run as is, previous business day or "
                                                  "next business day.")
    cfg: dict = {"cadence": cadence, "tz": tz, "tz_name": tz_name if not legacy else "UTC", "hour": hh, "minute": mm,
                 "weekend_rule": rule, "legacy": legacy, "day": None, "last_day": False, "date": None}
    if cadence == "monthly":
        if data.get("last_day") is True:
            cfg["last_day"] = True
        else:
            day = data.get("day")
            if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 31:
                raise ScheduleConfigError("day", "Monthly schedules need a day of the month between 1 and 31 "
                                                 "(or choose 'last day').")
            cfg["day"] = day
    elif cadence == "weekly":
        day = data.get("day")
        if not isinstance(day, int) or isinstance(day, bool) or not 0 <= day <= 6:
            raise ScheduleConfigError("day", "Weekly schedules need a weekday: 0 (Monday) to 6 (Sunday).")
        cfg["day"] = day
    else:
        raw = data.get("date")
        try:
            cfg["date"] = date.fromisoformat(str(raw))
        except (TypeError, ValueError):
            raise ScheduleConfigError("date", "Pick the date it should run on (YYYY-MM-DD).") from None
    return cfg


def _adjust(d: date, rule: str) -> date:
    if rule == "as_is" or d.weekday() < 5:
        return d
    if rule == "previous_business_day":
        return d - timedelta(days=d.weekday() - 4)  # Sat -> Fri, Sun -> Fri
    return d + timedelta(days=7 - d.weekday())  # Sat/Sun -> Mon


def _month_day(year: int, month: int, cfg: dict) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last if cfg["last_day"] else min(cfg["day"], last))


def _nominal_dates(cfg: dict, start: date):
    """Nominal (pre weekend-rule) local dates, ascending, starting at/after ``start``."""
    if cfg["cadence"] == "once":
        if cfg["date"] >= start:
            yield cfg["date"]
        return
    if cfg["cadence"] == "weekly":
        d = start + timedelta(days=(cfg["day"] - start.weekday()) % 7)
        while True:
            yield d
            d += timedelta(days=7)
    year, month = start.year, start.month
    while True:
        d = _month_day(year, month, cfg)
        if d >= start:
            yield d
        month += 1
        if month == 13:
            year, month = year + 1, 1


def _at(cfg: dict, local_date: date) -> datetime:
    return datetime.combine(local_date, time(cfg["hour"], cfg["minute"]), tzinfo=cfg["tz"]).astimezone(UTC)


def occurrences(data: dict, start: datetime, end: datetime) -> list[datetime]:
    """Occurrences (UTC-aware) with ``start <= t <= end``, ascending."""
    cfg = parse_config(data)
    local_start = (start.astimezone(cfg["tz"]).date()) - timedelta(days=4)  # a weekend rule can pull a date forward
    local_end = end.astimezone(cfg["tz"]).date() + timedelta(days=4)
    out: list[datetime] = []
    for nominal in _nominal_dates(cfg, local_start):
        if nominal > local_end:
            break
        moment = _at(cfg, _adjust(nominal, cfg["weekend_rule"]))
        if start <= moment <= end:
            out.append(moment)
    return sorted(set(out))


def next_runs(data: dict, after: datetime, count: int = 3) -> list[datetime]:
    """The next ``count`` occurrences strictly after ``after`` (UTC-aware)."""
    cfg = parse_config(data)
    found: list[datetime] = []
    local_start = after.astimezone(cfg["tz"]).date() - timedelta(days=4)
    for i, nominal in enumerate(_nominal_dates(cfg, local_start)):
        moment = _at(cfg, _adjust(nominal, cfg["weekend_rule"]))
        if moment > after:
            found.append(moment)
        if len(found) >= count or i > 400:
            break
    return sorted(found)[:count]


def due_occurrence(data: dict, now: datetime) -> datetime | None:
    """The one occurrence a tick at ``now`` may fire: the latest one at/before ``now`` and no older than 24h.

    Catch-up is therefore at most ONE missed occurrence within 24h after downtime; older ones are dropped.
    """
    window = occurrences(data, now - CATCH_UP_WINDOW, now)
    return window[-1] if window else None


def occurrence_key(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def describe(data: dict) -> str:
    """Plain-language one-liner for cards and previews."""
    cfg = parse_config(data)
    when = "" if cfg["legacy"] else f" at {cfg['hour']:02d}:{cfg['minute']:02d} ({cfg['tz_name']})"
    if cfg["cadence"] == "once":
        base = f"Once on {cfg['date'].isoformat()}"
    elif cfg["cadence"] == "weekly":
        base = f"Every {_WEEKDAYS[cfg['day']]}"
    elif cfg["last_day"]:
        base = "Monthly on the last day"
    else:
        base = f"Monthly on day {cfg['day']}"
    suffix = {"as_is": "", "previous_business_day": ", moved to the previous business day if on a weekend",
              "next_business_day": ", moved to the next business day if on a weekend"}[cfg["weekend_rule"]]
    return f"{base}{when}{suffix}"


def preview(data: dict, after: datetime, count: int = 3) -> list[dict]:
    """Server-computed 'Next runs' rows for the UI."""
    cfg = parse_config(data)
    rows = []
    for moment in next_runs(data, after, count):
        local = moment.astimezone(cfg["tz"])
        rows.append({"utc": moment.isoformat(), "local": local.strftime("%Y-%m-%d %H:%M"),
                     "weekday": local.strftime("%A"), "timezone": cfg["tz_name"],
                     "key": occurrence_key(moment)})
    return rows
