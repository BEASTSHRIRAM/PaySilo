"""Schedule calendar maths: month-end clamping, leap year, weekend rules, DST-free tz, catch-up window."""

from datetime import UTC, datetime

import pytest

from app.services import workflow_schedule as sch


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def cfg(**kw):
    base = {"cadence": "monthly", "day": 3, "time": "09:00", "timezone": "Asia/Kolkata"}
    base.update(kw)
    return base


def local_rows(data, after, n=3):
    return [(r["local"], r["weekday"]) for r in sch.preview(data, after, n)]


def test_monthly_day_three_next_three_runs_in_kolkata():
    rows = local_rows(cfg(), utc(2026, 11, 12))
    assert rows == [("2026-12-03 09:00", "Thursday"), ("2027-01-03 09:00", "Sunday"), ("2027-02-03 09:00", "Wednesday")]
    first = sch.next_runs(cfg(), utc(2026, 11, 12), 1)[0]
    assert first == utc(2026, 12, 3, 3, 30)  # 09:00 IST = 03:30 UTC, no DST


def test_day_31_uses_last_day_of_short_months_and_leap_year():
    rows = [r[0] for r in local_rows(cfg(day=31), utc(2027, 12, 31, 12), 3)]
    assert rows == ["2028-01-31 09:00", "2028-02-29 09:00", "2028-03-31 09:00"]  # 2028 is a leap year
    rows = [r[0] for r in local_rows(cfg(day=30), utc(2027, 1, 31), 2)]
    assert rows == ["2027-02-28 09:00", "2027-03-30 09:00"]
    assert local_rows(cfg(day=29), utc(2027, 1, 31), 1)[0][0] == "2027-02-28 09:00"


def test_explicit_last_day_option():
    rows = [r[0] for r in local_rows(cfg(last_day=True, day=None), utc(2026, 1, 1), 3)]
    assert rows == ["2026-01-31 09:00", "2026-02-28 09:00", "2026-03-31 09:00"]


def test_weekend_rules():
    # 2027-01-03 is a Sunday
    assert local_rows(cfg(weekend_rule="previous_business_day"), utc(2026, 12, 20), 1)[0] == ("2027-01-01 09:00", "Friday")
    assert local_rows(cfg(weekend_rule="next_business_day"), utc(2026, 12, 20), 1)[0] == ("2027-01-04 09:00", "Monday")
    assert local_rows(cfg(weekend_rule="as_is"), utc(2026, 12, 20), 1)[0] == ("2027-01-03 09:00", "Sunday")
    # a weekday is never moved
    assert local_rows(cfg(weekend_rule="next_business_day"), utc(2026, 11, 12), 1)[0][1] == "Thursday"


def test_weekend_rule_can_cross_month_boundary():
    # 2026-10-31 is a Saturday: next business day is Mon 2 Nov
    rows = local_rows(cfg(last_day=True, day=None, weekend_rule="next_business_day"), utc(2026, 10, 1), 1)
    assert rows == [("2026-11-02 09:00", "Monday")]


def test_weekly_and_once():
    assert local_rows({"cadence": "weekly", "day": 4, "time": "08:30", "timezone": "Asia/Kolkata"}, utc(2026, 11, 12), 2) == [
        ("2026-11-13 08:30", "Friday"), ("2026-11-20 08:30", "Friday")]
    once = {"cadence": "once", "date": "2026-12-25", "time": "10:00", "timezone": "Asia/Kolkata"}
    assert [r[0] for r in local_rows(once, utc(2026, 11, 12), 3)] == ["2026-12-25 10:00"]
    assert sch.next_runs(once, utc(2026, 12, 26), 3) == []


def test_next_run_is_strictly_after():
    t = utc(2026, 12, 3, 3, 30)
    assert sch.next_runs(cfg(), t, 1)[0] == utc(2027, 1, 3, 3, 30)


def test_dst_zone_keeps_local_wall_clock():
    data = cfg(timezone="Europe/Berlin", day=1)
    summer, winter = sch.next_runs(data, utc(2026, 6, 20), 1)[0], sch.next_runs(data, utc(2026, 11, 20), 1)[0]
    assert summer == utc(2026, 7, 1, 7, 0) and winter == utc(2026, 12, 1, 8, 0)


@pytest.mark.parametrize("bad,field", [
    ({"cadence": "daily"}, "cadence"), (cfg(day=0), "day"), (cfg(day=32), "day"), (cfg(day="3"), "day"),
    (cfg(time="25:00"), "time"), (cfg(timezone="Mars/Base"), "timezone"), (cfg(weekend_rule="sometimes"), "weekend_rule"),
    ({"cadence": "weekly", "day": 7}, "day"), ({"cadence": "once", "date": "2026-02-30"}, "date")])
def test_invalid_configs_name_the_field(bad, field):
    with pytest.raises(sch.ScheduleConfigError) as info:
        sch.parse_config(bad)
    assert info.value.field == field


def test_due_occurrence_catch_up_window():
    data = cfg()  # 2026-12-03 09:00 IST = 03:30 UTC
    occ = utc(2026, 12, 3, 3, 30)
    assert sch.due_occurrence(data, utc(2026, 12, 3, 3, 29)) is None  # not yet
    assert sch.due_occurrence(data, occ) == occ
    assert sch.due_occurrence(data, utc(2026, 12, 4, 3, 29)) == occ  # 23h59m late: one catch-up
    assert sch.due_occurrence(data, utc(2026, 12, 4, 3, 31)) is None  # older than 24h: dropped


def test_legacy_config_means_the_whole_utc_day():
    legacy = {"cadence": "monthly", "day": 3, "fan_out": "employers"}
    assert sch.due_occurrence(legacy, utc(2026, 12, 3, 22, 0)) == utc(2026, 12, 3)
    assert sch.due_occurrence(legacy, utc(2026, 12, 4, 0, 1)) is None
