"""Due slots that no delivery recorded done.

A workflow enrolled in github-cron-trigger that runs the slot action records
each slot in the ledger once a delivery has finished the slot's work
(slot_ledger.py, README.md). A slot still without a record hours after it was
due is one neither delivery completed: the clock's was not sent or failed, and
GitHub's backstop was dropped or failed too. A dropped delivery leaves no run
to go red, so nothing else reports it; this reads the ledger against each
line's due slots and names the gaps.

What counts, and why each bound:

- A workflow is checked when it runs the slot action itself
  (clock.uses_slot_action), since only those record: on each line it is
  enrolled for (clock.scheduled_workflow), and, when it chains through
  `workflow_run`, on each enrolled parent's line, which is the line its
  chained records are keyed by (the README's "Chained workflows"). A line
  nothing enrolled delivers is left out even though GitHub's `schedule`
  delivery of it records: GitHub alone delivers that line, as it does every
  workflow that is not enrolled, and this check answers for the deliveries
  github-cron-trigger arranges, so every line it checks is delivered by an
  enrolled workflow.
- A chained line follows the slot action (steps.resolve), which
  resolves a chained run by the parent that started it, so every parent the
  chain names is followed. A parent with no cron line never delivers a slot,
  and one that is not enrolled is GitHub's alone, so neither adds a line. An
  enrolled parent the slot action refuses - one carrying more than one line,
  or a line that is zoned or fires more than once a day - has every scheduled
  run chained off it refused, so its line never records and is reported
  rather than judged; so is a chain naming no parent, and a name no workflow
  file carries.
- A slot is due once GRACE has passed since it. GitHub's backstop has been
  measured starting more than six and a half hours late, and a delivery then
  takes its own run time, so a slot inside GRACE may still be in flight rather
  than missed.
- A line is checked from its oldest record on, never before it: slots from
  before a workflow enrolled were never recorded and are not misses, and a line
  with no record at all is not checked yet. A key that names no instant is not
  a record the ledger wrote (ledger_key writes none), so it opens no line.
- Any record of the slot counts, a part's included. A partly done slot was
  delivered, and its failed leg is a failed run, which reports itself.
- The window reaches back LOOKBACK at most, so a missed slot is named for that
  long and then left to whatever reported it.
"""

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import slot_ledger, workflow_files, yq
from .clock import EVENT_TYPE_PREFIX, scheduled_workflow, uses_slot_action
from .cron_slots import (
    UTC,
    CronSchedule,
    UnsupportedCron,
    format_slot,
    parse_ledger_key,
    slots_between,
)
from .github import GitHubError
from .slot_ledger import cron_segment, record_slot_key
from .workflow_triggers import (
    MisconfiguredWorkflow,
    dispatch_types,
    schedule_entries,
    triggers,
    utc_schedule,
)

GRACE = timedelta(hours=12)
LOOKBACK = timedelta(days=7)


@dataclass(frozen=True)
class LedgeredLine:
    """A cron line a workflow records slots on."""

    schedule: CronSchedule
    # The workflow a slot of this line is delivered to: the workflow itself for
    # a line it is enrolled for, the parent whose runs start it for a chained
    # line. Re-delivering a missed slot means dispatching it to this workflow -
    # for a chained line only while the slot is the parent line's newest, since
    # a chained run takes that slot whatever the parent was sent
    # (steps.resolve).
    delivered_by: str


@dataclass(frozen=True)
class MissedLine:
    """One cron line's due slots that have no record."""

    workflow_file: str
    schedule: CronSchedule
    delivered_by: str
    slots: tuple[datetime, ...]


@dataclass(frozen=True)
class Report:
    """What a check found: the missed lines, the workflows whose records were
    read, and what it could not judge, by workflow, each with its reason. A
    workflow judged on some lines but not all appears in `checked` and in
    `unreadable`."""

    missed: tuple[MissedLine, ...]
    checked: tuple[str, ...]
    unreadable: tuple[tuple[str, str], ...]
    # Every line the check judged, missed or not, as (workflow file, line): its
    # ledger read and holding at least one of its slots. A line absent here was
    # not judged this time - refused, gone, not yet recorded, or behind a
    # ledger that could not be read - which says nothing about what it missed.
    judged: tuple[tuple[str, CronSchedule], ...]


def display_name(workflow_file: str, document: Mapping[object, object]) -> str:
    """The name `workflow_run.workflows` matches a workflow by: its `name:`, or,
    when it sets none, its path from the repository root, as GitHub shows it."""
    name = document.get("name")
    return name if isinstance(name, str) else f".github/workflows/{workflow_file}"


def parent_line(
    workflow_file: str, parent_file: str, parent: object
) -> CronSchedule | None:
    """The line a run chained off `parent_file` resolves its slot by, or None
    when there is none to check: the parent carries no cron line, so it never
    delivers a slot, or it is not enrolled, so GitHub alone delivers it.

    Raises MisconfiguredWorkflow, or UnsupportedCron for a zoned or unreadable
    line, where the slot action refuses every scheduled run chained off the
    parent (steps.resolve), so the line never records.
    """
    on = triggers(parent)
    entries = schedule_entries(parent_file, on.get("schedule"))
    if not entries:
        return None
    enrollment = EVENT_TYPE_PREFIX + parent_file
    if enrollment not in dispatch_types(parent_file, on.get("repository_dispatch")):
        return None
    if len(entries) != 1:
        raise MisconfiguredWorkflow(
            f"{workflow_file}: parent {parent_file} carries more than one cron line,"
            " so a chained run cannot tell which one fired it"
        )
    schedule = utc_schedule(entries[0])
    if not schedule.fires_at_most_daily:
        raise MisconfiguredWorkflow(
            f"{workflow_file}: parent {parent_file}'s line {schedule.expression!r}"
            " fires more than once a day, so a chained run has no attributable slot"
        )
    return schedule


def chained_lines(
    workflow_file: str, document: object, documents: Mapping[str, object]
) -> tuple[list[LedgeredLine], list[str]]:
    """The parents' lines a workflow chained through `workflow_run` keys its
    records by, and why any parent's line could not be established. Neither
    holds anything when the workflow does not chain.
    """
    on = triggers(document)
    if "workflow_run" not in on:
        return [], []
    run = on.get("workflow_run")
    names = run.get("workflows") if isinstance(run, Mapping) else None
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) for name in names)
    ):
        unnamed = (
            f"{workflow_file}: chains through workflow_run without naming its"
            " parent workflows"
        )
        return [], [unnamed]
    lines: list[LedgeredLine] = []
    problems: list[str] = []
    for name in names:
        parents = sorted(
            parent_file
            for parent_file, parent in documents.items()
            if isinstance(parent, Mapping) and display_name(parent_file, parent) == name
        )
        if not parents:
            problems.append(
                f"{workflow_file}: parent workflow {name!r} is no workflow file here"
            )
        for parent_file in parents:
            try:
                schedule = parent_line(
                    workflow_file, parent_file, documents[parent_file]
                )
            except (MisconfiguredWorkflow, UnsupportedCron) as exc:
                problems.append(str(exc))
                continue
            if schedule is not None:
                lines.append(LedgeredLine(schedule, parent_file))
    return lines, problems


def ledgered_lines(
    workflow_file: str, document: object, documents: Mapping[str, object]
) -> tuple[list[LedgeredLine], list[str]]:
    """The lines a workflow records slots on, and why any line it might record
    on could not be established.

    Empty for a workflow that does not run the slot action. Its own lines count
    only when it is enrolled for them; a chained line comes from a parent.
    """
    if not uses_slot_action(document):
        return [], []
    candidates: list[LedgeredLine] = []
    problems: list[str] = []
    try:
        own = scheduled_workflow(workflow_file, document)
    except (MisconfiguredWorkflow, UnsupportedCron) as exc:
        problems.append(str(exc))
    else:
        if own is not None and own.enrolled:
            candidates.extend(
                LedgeredLine(schedule, workflow_file) for schedule in own.schedules
            )
    chained, chain_problems = chained_lines(workflow_file, document, documents)
    candidates.extend(chained)
    problems.extend(chain_problems)
    # Records are keyed by the line alone, so a line reached more than once -
    # its own and a parent's, or several parents' - holds one set of records
    # and is checked once, delivered by the first way it was reached.
    lines: list[LedgeredLine] = []
    for line in candidates:
        if all(line.schedule.canonical != kept.schedule.canonical for kept in lines):
            lines.append(line)
    return lines, problems


def recorded_slot(record_name: str) -> datetime | None:
    """The slot a record names, or None for a name the ledger did not write:
    one outside its form (slot_ledger.record_slot_key), or a key in that form
    naming no instant, such as a 31 February."""
    key = record_slot_key(record_name)
    if key is None:
        return None
    try:
        return parse_ledger_key(key)
    except ValueError:
        return None


def recorded_slots(
    schedule: CronSchedule, record_names: Sequence[str]
) -> set[datetime]:
    """The slots of one line that the records hold, as recorded_slot reads
    each; `record_names` are as slot_ledger.records returns them."""
    prefix = cron_segment(schedule) + "/"
    slots: set[datetime] = set()
    for name in record_names:
        if name.startswith(prefix):
            slot = recorded_slot(name)
            if slot is not None:
                slots.add(slot)
    return slots


def missed_lines(
    workflow_file: str,
    lines: Sequence[LedgeredLine],
    record_names: Sequence[str],
    now: datetime,
    grace: timedelta = GRACE,
    lookback: timedelta = LOOKBACK,
) -> list[MissedLine]:
    """Each of the workflow's lines with due slots the records do not hold.

    `record_names` are the workflow's records as slot_ledger.records returns
    them, `<cron line>/<leaf>`.
    """
    missed = []
    for line in lines:
        schedule = line.schedule
        recorded = recorded_slots(schedule, record_names)
        if not recorded:
            continue
        # The oldest record is itself recorded, so the window opens just after it.
        after = max(min(recorded), now - lookback)
        slots = tuple(
            slot
            for slot in slots_between(schedule, after, now - grace)
            if slot not in recorded
        )
        if slots:
            missed.append(MissedLine(workflow_file, schedule, line.delivered_by, slots))
    return missed


def check(
    repo: str,
    documents: Mapping[str, object],
    now: datetime,
    records: Callable[[str, str], list[str]] = slot_ledger.records,
) -> Report:
    """Missed slots across every workflow in `documents` that records slots.

    A workflow's records, or a line it might record on, that cannot be read is
    reported as unreadable and not judged: saying nothing about it is the
    honest answer, where an empty record list would name every slot as missed.
    """
    missed: list[MissedLine] = []
    checked: list[str] = []
    unreadable: list[tuple[str, str]] = []
    judged: list[tuple[str, CronSchedule]] = []
    for workflow_file, document in sorted(documents.items()):
        lines, problems = ledgered_lines(workflow_file, document, documents)
        unreadable.extend((workflow_file, problem) for problem in problems)
        if not lines:
            continue
        try:
            record_names = records(repo, workflow_file)
        except (GitHubError, ValueError) as exc:
            unreadable.append((workflow_file, str(exc)))
            continue
        checked.append(workflow_file)
        judged.extend(
            (workflow_file, line.schedule)
            for line in lines
            if recorded_slots(line.schedule, record_names)
        )
        missed.extend(missed_lines(workflow_file, lines, record_names, now))
    return Report(tuple(missed), tuple(checked), tuple(unreadable), tuple(judged))


# The command's exit status is a bit set, so neither finding hides the other.
EXIT_MISSED = 1
# Set when anything could not be read or judged - a workflow file, a line, a
# ledger - since a check that did not reach everything cannot say nothing was
# missed.
EXIT_INCOMPLETE = 2


def main(
    argv: Sequence[str],
    now: datetime | None = None,
    records: Callable[[str, str], list[str]] = slot_ledger.records,
    load: workflow_files.Loader = workflow_files.load_default_branch,
) -> int:
    parser = argparse.ArgumentParser(
        prog="github-cron-trigger missed",
        description="Name the due slots no delivery recorded done.",
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    args = parser.parse_args(argv)
    try:
        documents, unparsed = load(args.repo)
    except (GitHubError, ValueError, yq.ParserUnavailable) as exc:
        print(f"::error::missed: cannot read any workflow of {args.repo}: {exc}")
        return EXIT_INCOMPLETE
    report = check(args.repo, documents, now or datetime.now(UTC), records)
    for workflow_file, reason in sorted(unparsed.items()):
        print(f"::error::{workflow_file}: not read, not judged: {reason}")
    for workflow_file, reason in report.unreadable:
        print(f"::error::{workflow_file}: not judged: {reason}")
    for line in report.missed:
        slots = ", ".join(format_slot(slot) for slot in line.slots)
        via = (
            ""
            if line.delivered_by == line.workflow_file
            else f" (delivered through {line.delivered_by})"
        )
        print(
            f"{line.workflow_file} cron {line.schedule.canonical!r}{via}: no record"
            f" for {slots}"
        )
    unjudged = set(unparsed) | {workflow_file for workflow_file, _ in report.unreadable}
    print(
        f"missed: {len(report.checked)} ledgered workflow(s) checked,"
        f" {len(report.missed)} line(s) with missed slots,"
        f" {len(unjudged)} workflow(s) not judged in full"
    )
    status = EXIT_MISSED if report.missed else 0
    if unjudged:
        status |= EXIT_INCOMPLETE
    return status
