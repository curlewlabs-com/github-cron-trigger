"""Unit tests for slot resolution.

A wrong slot is the failure these guard: a run keyed to the wrong slot either
skips work it owed (it finds a neighbouring slot's record) or does work twice
(it misses its own). So the cases sit where a slot boundary can be misread - the
exact instant, the minute before it, a run part-way through the slot's minute,
UTC midnight, the day a weekly schedule fires, a month without the named day, a
leap day across a century, and a `now` whose local calendar date differs from
its UTC date - and where a line could be read more than one way: every form the grammar
accepts is pinned to one set of values, and every form it cannot resolve
exactly is pinned to a refusal. Every instant is hardcoded; nothing reads the
clock.
"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from github_cron_trigger.cron_slots import (
    UTC,
    UnsupportedCron,
    format_slot,
    is_occurrence,
    latest_slot,
    ledger_key,
    parse_cron,
    parse_ledger_key,
    parse_slot,
    slots_between,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


def utc(
    year: int, month: int, day: int, hour: int, minute: int, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


class ParseCronTest(unittest.TestCase):
    def test_accepts_githubs_documented_examples(self) -> None:
        # The operator examples GitHub's `on.schedule` reference gives, each
        # read the way that reference describes it.
        self.assertEqual(parse_cron("15 * * * *").minutes, (15,))
        documented = parse_cron("2,10 4,5 * * *")
        self.assertEqual((documented.minutes, documented.hours), ((2, 10), (4, 5)))
        self.assertEqual(parse_cron("30 4-6 * * *").hours, (4, 5, 6))
        self.assertEqual(parse_cron("20/15 * * * *").minutes, (20, 35, 50))
        self.assertEqual(parse_cron("30 5 * * 1-5").weekdays, (1, 2, 3, 4, 5))

    def test_accepts_the_shapes_in_use(self) -> None:
        daily = parse_cron("30 19 * * *")
        self.assertEqual((daily.minutes, daily.hours), ((30,), (19,)))
        self.assertEqual(daily.weekdays, tuple(range(7)))
        every_twenty = parse_cron("47,7,27 * * * *")
        self.assertEqual(every_twenty.minutes, (7, 27, 47))
        self.assertEqual(every_twenty.hours, tuple(range(24)))
        self.assertEqual(parse_cron("20 2 * * 0").weekdays, (0,))

    def test_steps_ranges_and_lists(self) -> None:
        self.assertEqual(parse_cron("*/20 * * * *").minutes, (0, 20, 40))
        self.assertEqual(parse_cron("0-30/10 * * * *").minutes, (0, 10, 20, 30))
        self.assertEqual(parse_cron("0 1-5/2 * * *").hours, (1, 3, 5))
        self.assertEqual(parse_cron("*/90 * * * *").minutes, (0,))
        self.assertEqual(parse_cron("0 0 */10 * *").days, (1, 11, 21, 31))
        # Overlapping items name each value once.
        self.assertEqual(parse_cron("1-5,3,5 * * * *").minutes, (1, 2, 3, 4, 5))

    def test_upper_case_names(self) -> None:
        self.assertEqual(parse_cron("0 9 * * MON-FRI").weekdays, (1, 2, 3, 4, 5))
        self.assertEqual(parse_cron("0 9 * JAN,DEC *").months, (1, 12))
        self.assertEqual(parse_cron("0 9 * * SAT,SUN").weekdays, (0, 6))

    def test_refuses_day_of_month_and_day_of_week_together(self) -> None:
        # Cron implementations disagree on whether such a line fires when either
        # field matches or only when they each do, and a `*/2` or a range covering
        # every day decides it differently again - so any line where neither is
        # a bare `*` is refused rather than resolved to a guessed slot.
        for expression in (
            "0 1 1 * 1",
            "0 1 */2 * 1",
            "0 1 1-31 * 0-6",
            "0 1 15 * MON-FRI",
        ):
            with (
                self.subTest(expression=expression),
                self.assertRaises(UnsupportedCron),
            ):
                parse_cron(expression)

    def test_refuses_a_line_that_never_fires(self) -> None:
        # No slot exists to resolve, and a search for one would never end.
        for expression in ("0 0 30 2 *", "0 0 31 4,6,9,11 *", "0 0 30,31 FEB *"):
            with (
                self.subTest(expression=expression),
                self.assertRaises(UnsupportedCron),
            ):
                parse_cron(expression)
        # The 29th of February does fire, every leap year.
        self.assertEqual(parse_cron("0 0 29 2 *").days, (29,))

    def test_refuses_a_day_of_week_seven(self) -> None:
        # Some crons read 7 as Sunday, but GitHub documents the day of week as
        # 0-6: a line GitHub may reject would have the clock dispatch slots no
        # run can receive, so 7 is refused in every form it can take.
        for expression in ("0 0 * * 7", "0 0 * * 5-7", "0 0 * * 7/2", "0 0 * * 0,7"):
            with (
                self.subTest(expression=expression),
                self.assertRaises(UnsupportedCron),
            ):
                parse_cron(expression)

    def test_refuses_a_name_in_lower_or_mixed_case(self) -> None:
        # GitHub documents names as JAN-DEC and SUN-SAT, and not whether it reads
        # another case: a line GitHub may reject would have the clock dispatch
        # slots no run can receive, so a name is accepted only as documented.
        for expression in (
            "0 9 * * mon",
            "0 9 * * Mon",
            "0 9 * * mon-FRI",
            "0 9 * JAN,dec *",
            "0 0 * jan *",
        ):
            with (
                self.subTest(expression=expression),
                self.assertRaises(UnsupportedCron),
            ):
                parse_cron(expression)

    def test_refuses_every_form_outside_the_grammar(self) -> None:
        for expression in (
            "60 1 * * *",
            "0 24 * * *",
            "0 1 0 * *",
            "0 1 32 * *",
            "0 1 * 13 *",
            "0 1 * 0 *",
            "0 1 * * 8",
            "0 1 * * MONDAY",
            "0 1 * * THU-MON",
            "5-3 * * * *",
            "*/0 * * * *",
            "*/ * * * *",
            "/5 * * * *",
            "1-2-3 * * * *",
            "1,,2 * * * *",
            "0 1 ? * *",
            "0 1 L * *",
            "0 1 15W * *",
            "0 1 * * 1#2",
            "\u0663 1 * * *",
            "0 1 * *",
            "0 1 * * * 2026",
            "@daily",
        ):
            with (
                self.subTest(expression=expression),
                self.assertRaises(UnsupportedCron),
            ):
                parse_cron(expression)


class LatestSlotTest(unittest.TestCase):
    def test_daily_at_the_exact_instant_is_that_slot(self) -> None:
        self.assertEqual(
            latest_slot(parse_cron("0 1 * * *"), utc(2026, 9, 28, 1, 0)),
            utc(2026, 9, 28, 1, 0),
        )

    def test_daily_one_minute_early_is_the_previous_day(self) -> None:
        self.assertEqual(
            latest_slot(parse_cron("0 1 * * *"), utc(2026, 9, 28, 0, 59)),
            utc(2026, 9, 27, 1, 0),
        )

    def test_part_way_through_the_slot_minute_is_that_slot(self) -> None:
        self.assertEqual(
            latest_slot(parse_cron("0 1 * * *"), utc(2026, 9, 28, 1, 0, 30)),
            utc(2026, 9, 28, 1, 0),
        )

    def test_hours_late_delivery_still_resolves_to_its_own_slot(self) -> None:
        # GitHub's scheduler starts runs hours after the instant; the run must
        # still belong to that morning's slot, not drift to another.
        self.assertEqual(
            latest_slot(parse_cron("0 1 * * *"), utc(2026, 9, 28, 6, 36)),
            utc(2026, 9, 28, 1, 0),
        )

    def test_utc_date_ahead_of_local_date(self) -> None:
        # 18:30 Pacific on the 27th is 01:30 UTC on the 28th: the slot is the
        # 28th's, because schedules are UTC whatever the caller's zone.
        now = datetime(2026, 9, 27, 18, 30, tzinfo=PACIFIC)
        self.assertEqual(
            latest_slot(parse_cron("0 1 * * *"), now), utc(2026, 9, 28, 1, 0)
        )

    def test_utc_date_behind_local_date(self) -> None:
        # 17:30 Pacific on the 27th is 00:30 UTC on the 28th, still before that
        # day's 01:00 slot, so the slot is the 27th's.
        now = datetime(2026, 9, 27, 17, 30, tzinfo=PACIFIC)
        self.assertEqual(
            latest_slot(parse_cron("0 1 * * *"), now), utc(2026, 9, 27, 1, 0)
        )

    def test_weekly_on_its_day_and_between(self) -> None:
        monday_three = parse_cron("0 3 * * 1")
        self.assertEqual(
            latest_slot(monday_three, utc(2026, 9, 28, 3, 0)), utc(2026, 9, 28, 3, 0)
        )
        self.assertEqual(
            latest_slot(monday_three, utc(2026, 9, 28, 2, 59)), utc(2026, 9, 21, 3, 0)
        )
        self.assertEqual(
            latest_slot(monday_three, utc(2026, 9, 27, 12, 0)), utc(2026, 9, 21, 3, 0)
        )

    def test_sunday_is_day_zero(self) -> None:
        # Saturday night UTC: the latest Sunday 02:20 is six days back.
        self.assertEqual(
            latest_slot(parse_cron("20 2 * * 0"), utc(2026, 10, 3, 23, 0)),
            utc(2026, 9, 27, 2, 20),
        )

    def test_minute_list_within_the_hour_and_across_midnight(self) -> None:
        every_twenty = parse_cron("7,27,47 * * * *")
        self.assertEqual(
            latest_slot(every_twenty, utc(2026, 9, 28, 10, 6)), utc(2026, 9, 28, 9, 47)
        )
        self.assertEqual(
            latest_slot(every_twenty, utc(2026, 9, 28, 10, 27)),
            utc(2026, 9, 28, 10, 27),
        )
        self.assertEqual(
            latest_slot(every_twenty, utc(2026, 9, 28, 0, 5)), utc(2026, 9, 27, 23, 47)
        )

    def test_step_from_a_value_within_the_hour(self) -> None:
        documented = parse_cron("20/15 * * * *")
        self.assertEqual(
            latest_slot(documented, utc(2026, 9, 28, 10, 51)), utc(2026, 9, 28, 10, 50)
        )
        self.assertEqual(
            latest_slot(documented, utc(2026, 9, 28, 11, 19)), utc(2026, 9, 28, 10, 50)
        )
        self.assertEqual(
            latest_slot(documented, utc(2026, 9, 28, 11, 20)), utc(2026, 9, 28, 11, 20)
        )

    def test_weekday_range_over_a_weekend(self) -> None:
        # Sunday 2026-09-27: the latest weekday 05:30 is Friday's.
        self.assertEqual(
            latest_slot(parse_cron("30 5 * * 1-5"), utc(2026, 9, 27, 12, 0)),
            utc(2026, 9, 25, 5, 30),
        )

    def test_day_of_month_and_month(self) -> None:
        monthly = parse_cron("0 0 1 * *")
        self.assertEqual(
            latest_slot(monthly, utc(2026, 9, 28, 12, 0)), utc(2026, 9, 1, 0, 0)
        )
        self.assertEqual(
            latest_slot(monthly, utc(2026, 10, 1, 0, 0)), utc(2026, 10, 1, 0, 0)
        )
        # September has no 31st, so the latest is August's.
        self.assertEqual(
            latest_slot(parse_cron("0 0 31 * *"), utc(2026, 9, 30, 23, 0)),
            utc(2026, 8, 31, 0, 0),
        )
        self.assertEqual(
            latest_slot(parse_cron("0 0 1 JAN *"), utc(2026, 9, 28, 12, 0)),
            utc(2026, 1, 1, 0, 0),
        )

    def test_leap_day_across_a_century_that_is_not_a_leap_year(self) -> None:
        # 2100 is not a leap year, so from early 2104 the latest February 29th
        # is 2096's: the longest gap any line that fires can have, and the
        # search must reach it.
        leap_day = parse_cron("0 0 29 2 *")
        self.assertEqual(
            latest_slot(leap_day, utc(2026, 9, 28, 12, 0)), utc(2024, 2, 29, 0, 0)
        )
        self.assertEqual(
            latest_slot(leap_day, utc(2104, 2, 28, 23, 59)), utc(2096, 2, 29, 0, 0)
        )
        self.assertEqual(
            latest_slot(leap_day, utc(2104, 2, 29, 0, 0)), utc(2104, 2, 29, 0, 0)
        )

    def test_naive_now_is_refused(self) -> None:
        # A naive datetime has no zone to convert from, so any slot computed
        # from it would silently assume one.
        with self.assertRaises(ValueError):
            # The naive value is the input under test.
            naive = datetime(2026, 9, 28, 1, 0)  # noqa: DTZ001
            latest_slot(parse_cron("0 1 * * *"), naive)


class ScheduleShapeTest(unittest.TestCase):
    def test_canonical_form_ignores_how_a_schedule_is_written(self) -> None:
        # The ledger keys on this form, so every written form of one schedule must
        # meet at one record.
        self.assertEqual(parse_cron("30,0  1 * * *").canonical, "0,30 1 * * *")
        self.assertEqual(parse_cron("0 1 * * *").canonical, "0 1 * * *")
        self.assertEqual(parse_cron("20 2 * * 0").canonical, "20 2 * * 0")
        every_hour = ",".join(str(h) for h in range(24))
        self.assertEqual(parse_cron(f"5 {every_hour} * * *").canonical, "5 * * * *")

    def test_canonical_form_meets_across_ranges_steps_and_names(self) -> None:
        self.assertEqual(
            parse_cron("0 9 * * MON-FRI").canonical,
            parse_cron("0 9 * * 1,2,3,4,5").canonical,
        )
        self.assertEqual(parse_cron("0 9 * * 1-5").canonical, "0 9 * * 1,2,3,4,5")
        self.assertEqual(parse_cron("*/20 * * * *").canonical, "0,20,40 * * * *")
        self.assertEqual(parse_cron("*/1 0-23 * * *").canonical, "* * * * *")
        self.assertEqual(parse_cron("0 0 * JAN SUN").canonical, "0 0 * 1 0")
        self.assertEqual(parse_cron("0 0 1-31 1-12 *").canonical, "0 0 * * *")
        self.assertEqual(parse_cron("0 0 * * 0-6").canonical, "0 0 * * *")

    def test_canonical_form_of_the_lines_in_use_is_unchanged(self) -> None:
        # The ledger's record names and the clock's state keys are built from
        # this form, so the lines scheduled before the grammar widened must
        # still render exactly as they did.
        for expression in (
            "0 3 * * 1",
            "15 11 * * 1",
            "30 12 * * *",
            "15 13 * * *",
            "45 3 * * 1",
            "0 1 * * *",
            "45 11 * * 1",
            "0 17 * * *",
            "30 19 * * *",
            "25 10 * * *",
            "0 8 * * *",
            "0 4 * * 1",
            "37 8 * * *",
            "20 2 * * 0",
            "17 9 * * *",
            "0 11 * * *",
            "7,27,47 * * * *",
            "20 6 * * *",
            "0 13 * * *",
        ):
            with self.subTest(expression=expression):
                self.assertEqual(parse_cron(expression).canonical, expression)

    def test_at_most_daily_means_a_single_minute_and_hour(self) -> None:
        self.assertTrue(parse_cron("30 19 * * *").fires_at_most_daily)
        self.assertTrue(parse_cron("0 3 * * 1").fires_at_most_daily)
        self.assertTrue(parse_cron("30 5 * * 1-5").fires_at_most_daily)
        self.assertTrue(parse_cron("0 0 1 JAN *").fires_at_most_daily)
        self.assertFalse(parse_cron("*/20 * * * *").fires_at_most_daily)
        self.assertFalse(parse_cron("7,27,47 * * * *").fires_at_most_daily)
        self.assertFalse(parse_cron("0 1,13 * * *").fires_at_most_daily)
        self.assertFalse(parse_cron("0,30 1 * * *").fires_at_most_daily)

    def test_occurrence_is_exact(self) -> None:
        daily = parse_cron("0 1 * * *")
        self.assertTrue(is_occurrence(daily, utc(2026, 9, 28, 1, 0)))
        self.assertFalse(is_occurrence(daily, utc(2026, 9, 28, 1, 1)))
        self.assertFalse(is_occurrence(daily, utc(2026, 9, 28, 1, 0, 30)))
        monday = parse_cron("0 3 * * 1")
        self.assertTrue(is_occurrence(monday, utc(2026, 9, 28, 3, 0)))
        self.assertFalse(is_occurrence(monday, utc(2026, 9, 27, 3, 0)))
        first_of_month = parse_cron("0 0 1 * *")
        self.assertTrue(is_occurrence(first_of_month, utc(2026, 10, 1, 0, 0)))
        self.assertFalse(is_occurrence(first_of_month, utc(2026, 10, 2, 0, 0)))


class SlotsBetweenTest(unittest.TestCase):
    """The freshness check walks every due slot of a line in its window, so a
    slot left out here is one no missed-slot report can ever name."""

    def test_daily_line_over_a_week(self) -> None:
        slots = slots_between(
            parse_cron("30 19 * * *"),
            utc(2026, 9, 21, 19, 30),
            utc(2026, 9, 28, 19, 30),
        )
        # Open at the start and closed at the end: the 21st's slot is the
        # window's lower bound, so it belongs to the window before this one.
        self.assertEqual(slots, [utc(2026, 9, day, 19, 30) for day in range(22, 29)])

    def test_weekly_line_names_its_day_only(self) -> None:
        # 2026-09-28 is a Monday.
        self.assertEqual(
            slots_between(
                parse_cron("0 3 * * 1"), utc(2026, 9, 14, 0, 0), utc(2026, 9, 28, 23, 0)
            ),
            [utc(2026, 9, 14, 3, 0), utc(2026, 9, 21, 3, 0), utc(2026, 9, 28, 3, 0)],
        )

    def test_several_firings_a_day_and_the_month_end(self) -> None:
        self.assertEqual(
            slots_between(
                parse_cron("7,27,47 23 * * *"),
                utc(2026, 9, 30, 23, 20),
                utc(2026, 10, 1, 0, 0),
            ),
            [utc(2026, 9, 30, 23, 27), utc(2026, 9, 30, 23, 47)],
        )

    def test_an_empty_or_backwards_window_names_nothing(self) -> None:
        schedule = parse_cron("0 1 * * *")
        self.assertEqual(
            slots_between(schedule, utc(2026, 9, 28, 1, 0), utc(2026, 9, 28, 1, 0)), []
        )
        self.assertEqual(
            slots_between(schedule, utc(2026, 9, 29, 0, 0), utc(2026, 9, 28, 0, 0)), []
        )

    def test_the_window_bounds_must_be_aware(self) -> None:
        with self.assertRaises(ValueError):
            # The naive value is the input under test.
            naive = datetime(2026, 9, 28, 1, 0)  # noqa: DTZ001
            slots_between(parse_cron("0 1 * * *"), naive, utc(2026, 9, 29, 0, 0))


class SlotRenderingTest(unittest.TestCase):
    def test_ledger_and_payload_forms(self) -> None:
        slot = utc(2026, 9, 28, 1, 0)
        self.assertEqual(ledger_key(slot), "20260928T0100Z")
        self.assertEqual(format_slot(slot), "2026-09-28T01:00Z")
        self.assertEqual(parse_slot("2026-09-28T01:00Z"), slot)

    def test_ledger_key_round_trips_and_parses_strictly(self) -> None:
        slot = utc(2026, 9, 28, 1, 0)
        self.assertEqual(parse_ledger_key(ledger_key(slot)), slot)
        for key in ("20260928T0100", "2026-09-28T01:00Z", "20261328T0100Z", ""):
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_ledger_key(key)

    def test_payload_parse_is_strict(self) -> None:
        for text in (
            "2026-09-28T01:00:00Z",
            "2026-09-28 01:00Z",
            "2026-09-28T01:00",
            "20260928T0100Z",
            "2026-09-28T01:00+01:00",
            "",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_slot(text)


if __name__ == "__main__":
    unittest.main()
