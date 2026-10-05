"""Scheduled slots for GitHub Actions cron expressions.

A slot is one firing of a workflow's `cron:` line: the UTC instant the schedule
names. Every scheduled run belongs to exactly one slot, whichever trigger
delivered it - GitHub's own `schedule` event, which GitHub starts hours after
the instant whenever its queue is backed up, or an external clock's
`repository_dispatch`, sent on time. The slot, not the instant a run happened to
start, is what a run's work and its ledger record are keyed on, so every
delivery of one slot agrees on what it is for.

THE GRAMMAR is the one GitHub documents for `on.schedule`: minute 0-59, hour
0-23, day of month 1-31, month 1-12 or JAN-DEC, and day of week 0-6 or SUN-SAT,
each a `*`, a value, a range `a-b`, or a comma list of those, any of which may
take a step `/n`. A step on a single value runs from it to the end of
the field, as GitHub's own example `20/15` (minutes 20, 35 and 50) does. Values
and names stay as GitHub documents them, so a day of week 7, which some crons
read as Sunday, is refused, and so is a name in lower or mixed case: a line
GitHub may reject is one the clock would dispatch with no run to receive it.

A line is resolved exactly or refused with UnsupportedCron, never approximated,
because a slot resolved wrong makes a run skip work it owed or redo work already
done. Refused:

- A line restricting day of month and day of week together, meaning neither is
  a bare `*`. Cron implementations disagree on how such a line fires - on a date
  either field matches, only on a date they each match, or depending on whether
  a field was written with a leading `*` - and GitHub does not say which it
  follows. With either field a bare `*`, every implementation agrees that the
  other field alone decides.
- A line that can never fire, such as `0 0 30 2 *`: it has no slot to resolve.
- Anything outside the grammar: the `@daily`-style macros GitHub itself does
  not support, and other schedulers' extensions (`?`, `L`, `W`, `#`).

Slots are UTC instants. A `schedule:` entry that sets a `timezone:` is refused
where the entries are read (workflow_triggers.py), since the cron text alone
cannot say it is zoned.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# datetime.UTC arrived in Python 3.11; this is the same object, and 3.10 is
# still the system python3 of supported runner images.
UTC = timezone.utc

# How far back latest_slot looks. The longest gap between slots of a line that
# can fire at all is a February 29th that crosses a century year that is not a
# leap year (2096 to 2104); the bound covers it, and parse_cron refuses every
# line that can never fire, so the search always ends at a slot.
_LOOKBACK_DAYS = 8 * 366 + 1

# Ledger and payload renderings. The ledger form has no ':' because git refuses
# that character in a ref name; the payload form is ISO 8601 so a person reading
# a dispatch or a run log sees an ordinary timestamp.
_LEDGER_FORMAT = "%Y%m%dT%H%MZ"
_PAYLOAD_FORMAT = "%Y-%m-%dT%H:%MZ"

# Digits only: str.isdigit() would also admit superscripts and other scripts'
# digits, which no cron means.
_NUMBER = re.compile(r"[0-9]+")

_MONTH_NAMES = {
    name: number
    for number, name in enumerate(
        (
            "JAN",
            "FEB",
            "MAR",
            "APR",
            "MAY",
            "JUN",
            "JUL",
            "AUG",
            "SEP",
            "OCT",
            "NOV",
            "DEC",
        ),
        start=1,
    )
}
_WEEKDAY_NAMES = {
    name: number
    for number, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))
}

# The most days each month ever has. February's 29 decides whether a line
# naming the 29th can fire at all; its leap-year rarity is latest_slot's bound.
_MONTH_MOST_DAYS = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}


class UnsupportedCron(ValueError):
    """A cron expression outside the accepted grammar, named in the message."""


@dataclass(frozen=True)
class _Field:
    """One field of a cron line: what it is called in an error, the values it
    ranges over, and the names it accepts."""

    what: str
    low: int
    high: int
    names: Mapping[str, int]


_MINUTE = _Field("minute", 0, 59, {})
_HOUR = _Field("hour", 0, 23, {})
_DAY = _Field("day of month", 1, 31, {})
_MONTH = _Field("month", 1, 12, _MONTH_NAMES)
_WEEKDAY = _Field("day of week", 0, 6, _WEEKDAY_NAMES)


def _value(token: str, field: _Field, expression: str) -> int:
    if _NUMBER.fullmatch(token):
        value = int(token)
    elif token in field.names:
        value = field.names[token]
    else:
        kind = "a number or an upper-case name" if field.names else "a number"
        raise UnsupportedCron(
            f"cron {expression!r}: {field.what} {token!r} is not {kind}"
        )
    if not field.low <= value <= field.high:
        raise UnsupportedCron(
            f"cron {expression!r}: {field.what} {value} is outside"
            f" {field.low}-{field.high}"
        )
    return value


def _expand(text: str, field: _Field, expression: str) -> tuple[int, ...]:
    """The sorted values one field names; raises UnsupportedCron for any form
    outside the grammar."""
    values: set[int] = set()
    for item in text.split(","):
        base, slash, step_text = item.partition("/")
        step = 1
        if slash:
            if not _NUMBER.fullmatch(step_text) or int(step_text) == 0:
                raise UnsupportedCron(
                    f"cron {expression!r}: {field.what} step {step_text!r} is not a"
                    " positive number"
                )
            step = int(step_text)
        if base == "*":
            start, end = field.low, field.high
        elif "-" in base:
            first, _, last = base.partition("-")
            start = _value(first, field, expression)
            end = _value(last, field, expression)
            if start > end:
                raise UnsupportedCron(
                    f"cron {expression!r}: {field.what} range {base!r} runs backwards"
                )
        else:
            start = _value(base, field, expression)
            end = field.high if slash else start
        values.update(range(start, end + 1, step))
    return tuple(sorted(values))


def _render(values: tuple[int, ...], field: _Field) -> str:
    if values == tuple(range(field.low, field.high + 1)):
        return "*"
    return ",".join(str(value) for value in values)


@dataclass(frozen=True)
class CronSchedule:
    """One parsed `cron:` line: the values each field allows.

    `weekdays` counts 0 for Sunday through 6 for Saturday, as cron does. At most
    one of `days` and `weekdays` restricts anything - parse_cron refuses a line
    restricting them together - so a date fires when its day, month and weekday
    are each in their field, which is what every cron implementation means by
    such a line.
    """

    expression: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: tuple[int, ...]
    months: tuple[int, ...]
    weekdays: tuple[int, ...]

    @property
    def canonical(self) -> str:
        """The expression rebuilt from its parsed values, so any written form of one
        schedule - list order, spacing, ranges, steps, names, or a list covering a
        whole field - renders the same. A field covering its whole range renders
        as `*`, every other as its values in order."""
        return " ".join(
            _render(values, field)
            for values, field in (
                (self.minutes, _MINUTE),
                (self.hours, _HOUR),
                (self.days, _DAY),
                (self.months, _MONTH),
                (self.weekdays, _WEEKDAY),
            )
        )

    @property
    def fires_at_most_daily(self) -> bool:
        """At most one firing a day: a single minute and a single hour."""
        return len(self.minutes) == 1 and len(self.hours) == 1

    def fires_on(self, day: date) -> bool:
        # isoweekday is 1 (Monday) to 7 (Sunday); cron counts Sunday as 0.
        return (
            day.day in self.days
            and day.month in self.months
            and day.isoweekday() % 7 in self.weekdays
        )


def parse_cron(expression: str) -> CronSchedule:
    """Parse a cron expression in the accepted grammar, or raise UnsupportedCron."""
    fields = expression.split()
    if len(fields) != 5:
        raise UnsupportedCron(
            f"cron {expression!r}: expected five fields, got {len(fields)}"
        )
    minute, hour, day, month, weekday = fields
    if day != "*" and weekday != "*":
        raise UnsupportedCron(
            f"cron {expression!r}: day of month and day of week are restricted"
            " together, and cron implementations disagree on how such a line fires;"
            " make one of them `*`"
        )
    schedule = CronSchedule(
        expression=expression,
        minutes=_expand(minute, _MINUTE, expression),
        hours=_expand(hour, _HOUR, expression),
        days=_expand(day, _DAY, expression),
        months=_expand(month, _MONTH, expression),
        weekdays=_expand(weekday, _WEEKDAY, expression),
    )
    # It fires if its earliest day exists in some month it names.
    earliest = schedule.days[0]
    if not any(earliest <= _MONTH_MOST_DAYS[month] for month in schedule.months):
        raise UnsupportedCron(
            f"cron {expression!r}: no month it names has any of the days it names,"
            " so it never fires"
        )
    return schedule


def _require_aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware; slots are UTC instants")
    return now.astimezone(UTC)


def latest_slot(schedule: CronSchedule, now: datetime) -> datetime:
    """The latest instant the schedule names at or before `now`, in UTC.

    `now` must be timezone-aware. Slots are whole minutes, so a `now` part-way
    through a slot's minute belongs to that slot.
    """
    current = _require_aware(now)
    day = current.date()
    for _ in range(_LOOKBACK_DAYS):
        if schedule.fires_on(day):
            for hour in reversed(schedule.hours):
                for minute in reversed(schedule.minutes):
                    candidate = datetime(
                        day.year, day.month, day.day, hour, minute, tzinfo=UTC
                    )
                    if candidate <= current:
                        return candidate
        day -= timedelta(days=1)
    raise AssertionError(
        f"no slot of {schedule.expression!r} in the {_LOOKBACK_DAYS} days before {now}"
    )


def slots_between(
    schedule: CronSchedule, after: datetime, until: datetime
) -> list[datetime]:
    """Every instant the schedule names in (after, until], oldest first, in UTC.

    Open at `after` and closed at `until`, so consecutive windows that share an
    end name each slot once. `after` and `until` must be timezone-aware.
    """
    start = _require_aware(after)
    end = _require_aware(until)
    slots = []
    day = start.date()
    while day <= end.date():
        if schedule.fires_on(day):
            for hour in schedule.hours:
                for minute in schedule.minutes:
                    candidate = datetime(
                        day.year, day.month, day.day, hour, minute, tzinfo=UTC
                    )
                    if start < candidate <= end:
                        slots.append(candidate)
        day += timedelta(days=1)
    return slots


def is_occurrence(schedule: CronSchedule, instant: datetime) -> bool:
    """Whether `instant` is exactly one of the schedule's slots."""
    current = _require_aware(instant)
    if current.second or current.microsecond:
        return False
    return latest_slot(schedule, current) == current


def ledger_key(slot: datetime) -> str:
    """The slot as a ledger ref leaf, e.g. 20260928T0100Z."""
    return _require_aware(slot).strftime(_LEDGER_FORMAT)


def parse_ledger_key(key: str) -> datetime:
    """Parse ledger_key's rendering, strictly, into an aware UTC datetime."""
    try:
        parsed = datetime.strptime(key, _LEDGER_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(
            f"ledger key {key!r} is not in the form YYYYMMDDTHHMMZ"
        ) from exc
    if ledger_key(parsed) != key:
        raise ValueError(f"ledger key {key!r} is not in the form YYYYMMDDTHHMMZ")
    return parsed


def format_slot(slot: datetime) -> str:
    """The slot as it travels in a dispatch payload and a step output."""
    return _require_aware(slot).strftime(_PAYLOAD_FORMAT)


def parse_slot(text: str) -> datetime:
    """Parse format_slot's rendering, strictly, into an aware UTC datetime.

    `%z` alone would also take an offset such as +01:00; requiring the parse to
    render back to the same text is what holds the input to the one form.
    """
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M%z")
    except ValueError as exc:
        raise ValueError(f"slot {text!r} is not in the form YYYY-MM-DDTHH:MMZ") from exc
    if format_slot(parsed) != text:
        raise ValueError(f"slot {text!r} is not in the form YYYY-MM-DDTHH:MMZ")
    return parsed.astimezone(UTC)
