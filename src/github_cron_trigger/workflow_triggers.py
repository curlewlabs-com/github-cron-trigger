"""The `on:` block of a parsed workflow document, read for its schedule and its
enrollment.

The clock reads every workflow's schedule entries and dispatch types through
these, and the slot command reads the run's own workflow, or a chained run's
parent, for the cron line that identifies its slot. Each reader raises
MisconfiguredWorkflow rather than guessing at a shape it cannot read.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .cron_slots import CronSchedule, UnsupportedCron, parse_cron


class MisconfiguredWorkflow(ValueError):
    """A workflow whose triggers cannot be read as a schedule or an enrollment,
    named in the message."""


def triggers(document: object) -> Mapping[object, object]:
    """The `on:` block as a mapping, whichever of its forms the file uses."""
    if not isinstance(document, Mapping):
        return {}
    on = document.get("on")
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {entry: None for entry in on}
    if isinstance(on, Mapping):
        return on
    return {}


def dispatch_types(workflow_file: str, value: object) -> list[str]:
    if not isinstance(value, Mapping) or "types" not in value:
        return []
    types = value["types"]
    if isinstance(types, str):
        return [types]
    if isinstance(types, list) and all(isinstance(t, str) for t in types):
        return list(types)
    raise MisconfiguredWorkflow(
        f"{workflow_file}: repository_dispatch types is not a string list"
    )


@dataclass(frozen=True)
class ScheduleEntry:
    """One `schedule:` entry: its cron line, and the IANA timezone it sets, if
    any. GitHub reads an entry without one in UTC."""

    cron: str
    timezone: str | None


def schedule_entries(workflow_file: str, entries: object) -> list[ScheduleEntry]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise MisconfiguredWorkflow(f"{workflow_file}: schedule is not a list")
    result: list[ScheduleEntry] = []
    for entry in entries:
        cron = entry.get("cron") if isinstance(entry, Mapping) else None
        if not isinstance(cron, str):
            raise MisconfiguredWorkflow(
                f"{workflow_file}: a schedule entry has no cron string"
            )
        timezone = entry.get("timezone")
        if timezone is not None and not isinstance(timezone, str):
            raise MisconfiguredWorkflow(
                f"{workflow_file}: schedule entry {cron!r} has a timezone that is not"
                " a string"
            )
        result.append(ScheduleEntry(cron, timezone))
    return result


def utc_schedule(entry: ScheduleEntry) -> CronSchedule:
    """The entry's cron line, parsed as the UTC schedule slots are computed in.

    Raises UnsupportedCron for a line outside the accepted grammar, and for an
    entry that sets a timezone: its slots are local times, and GitHub documents
    only half of how they fire across daylight saving changes - a time the
    spring-forward skips runs at the next valid time, but nothing says whether a
    time the fall-back repeats runs once or twice - so no UTC instant can be
    named for it without guessing.
    """
    if entry.timezone is not None:
        raise UnsupportedCron(
            f"cron {entry.cron!r} sets timezone {entry.timezone!r}; only UTC"
            " schedules can be resolved to a slot"
        )
    return parse_cron(entry.cron)
