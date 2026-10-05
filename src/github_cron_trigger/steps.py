"""Workflow-step commands: resolve which slot a run belongs to, record it done.

`slot` and `done` are what the slot and done actions run. The README describes
how a workflow enrolls and what each output means.
"""

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import slot_ledger, workflow_files, yq
from .cron_slots import (
    UTC,
    CronSchedule,
    UnsupportedCron,
    format_slot,
    is_occurrence,
    latest_slot,
    parse_cron,
    parse_slot,
)
from .github import GitHubError
from .workflow_triggers import (
    ScheduleEntry,
    schedule_entries,
    triggers,
    utc_schedule,
)

# How far in the future a dispatched slot may be before it is refused. A clock
# sends a slot once the slot's instant has passed on its own clock, and the
# runner's clock can trail that host's by a little; a slot further ahead than
# this came from something that is not following the delivery contract.
FUTURE_TOLERANCE = timedelta(minutes=10)

# How long records are kept, and so how old a dispatched slot may be: a slot
# older than this could have had its record pruned, and the ledger could not
# tell a finished slot from an owed one. Far beyond any delivery delay a record
# exists to absorb.
RECORD_RETENTION = timedelta(days=60)


# The parents a chained run can be a scheduled delivery of: a run GitHub's
# schedule started, or one the clock dispatched. Any other parent (a manual
# dispatch, a push) makes the chained run unscheduled too.
SCHEDULED_PARENT_EVENTS = frozenset({"schedule", "repository_dispatch"})

# The deliveries that name one of the run's own cron lines, which must be one
# of its workflow's schedule entries, in UTC.
OWN_LINE_EVENTS = frozenset({"schedule", "repository_dispatch"})

# Reads one workflow file's text: (repository, path, commit) -> text.
ReadText = Callable[[str, str, str], str]


class SlotError(Exception):
    """A scheduled delivery whose slot cannot be established."""


@dataclass(frozen=True)
class Parent:
    """The run that triggered a `workflow_run` run: its event, its workflow's
    schedule entries, and when GitHub created it."""

    event: str
    entries: tuple[ScheduleEntry, ...]
    created_at: datetime


@dataclass(frozen=True)
class Resolution:
    """What a run is: a scheduled delivery of one cron line's slot, or not
    scheduled at all (every field None)."""

    schedule: CronSchedule | None
    slot: datetime | None


def _own_line(
    entries: Sequence[ScheduleEntry],
    cron: str,
    names_entry: Callable[[ScheduleEntry], bool],
) -> CronSchedule:
    """The run's own schedule entry that a delivery names, parsed as UTC.

    The cron text a delivery carries cannot say whether its entry sets a
    timezone, so the workflow's own entries decide: a delivery naming no entry
    is refused, and so is one naming an entry that sets a timezone
    (utc_schedule), rather than being resolved to a UTC slot it is not.
    """
    named = [entry for entry in entries if names_entry(entry)]
    if not named:
        raise SlotError(f"cron {cron!r} is not one of this workflow's schedule entries")
    # Every named entry is checked, not only the first: a line written twice,
    # once with a timezone, cannot say which of the entries fired.
    schedules = [utc_schedule(entry) for entry in named]
    return schedules[0]


def _canonical_or_none(cron: str) -> str | None:
    try:
        return parse_cron(cron).canonical
    except UnsupportedCron:
        return None


def resolve(
    event_name: str,
    payload_slot: str,
    payload_cron: str,
    schedule_cron: str,
    own_entries: Sequence[ScheduleEntry],
    parent: Parent | None,
    now: datetime,
) -> Resolution:
    """Establish the cron line and slot a run belongs to from its event.

    `own_entries` are the run's own workflow's schedule entries, which a
    `repository_dispatch` or `schedule` delivery's line must be one of, in UTC.

    - repository_dispatch: the line and slot the sender named in
      `client_payload.cron` and `client_payload.slot`. The slot must be an
      instant of that line.
    - schedule: the line that fired (`github.event.schedule`) and its latest
      instant at or before `now`. GitHub's payload names no scheduled instant,
      so the slot is found by time alone, which is exact only while GitHub's
      delay is shorter than the line's period. GitHub's delay runs to hours, so
      a line that fires more than once a day is refused rather than attributed
      to whichever of its slots happens to be latest.
    - workflow_run, from a scheduled parent: the parent's slot - the latest
      instant of the parent workflow's cron line at or before the parent run's
      creation. Every delivery of the parent's slot starts a chained run, and
      each resolves to that slot, so the chained run's own records dedupe them.
      The parent must carry exactly one cron line, since its run does not say
      which line fired it, and that line must fire at most once a day for the
      same reason as above; a slot older than the ledger keeps records is
      refused, as a dispatched one is. From any other parent: not a scheduled
      run.
    - anything else: not a scheduled run.
    """
    if event_name == "repository_dispatch":
        if not payload_slot or not payload_cron:
            raise SlotError(
                "repository_dispatch must carry client_payload.slot and client_payload.cron"
            )
        # Matched by canonical form: the sender names the line as it renders it.
        payload_line = parse_cron(payload_cron).canonical
        schedule = _own_line(
            own_entries,
            payload_cron,
            lambda entry: _canonical_or_none(entry.cron) == payload_line,
        )
        slot = parse_slot(payload_slot)
        if not is_occurrence(schedule, slot):
            raise SlotError(
                f"dispatched slot {payload_slot} is not an instant of cron {payload_cron!r}"
            )
        if slot < now - RECORD_RETENTION:
            # The ledger no longer holds records this old, so it cannot say
            # whether this slot was done; running it could repeat finished work.
            raise SlotError(
                f"dispatched slot {payload_slot} is older than the ledger keeps records"
                f" ({RECORD_RETENTION.days} days)"
            )
        if slot > now + FUTURE_TOLERANCE:
            raise SlotError(
                f"dispatched slot {payload_slot} is in the future (now {format_slot(now)})"
            )
        return Resolution(schedule=schedule, slot=slot)
    if event_name == "schedule":
        if not schedule_cron:
            raise SlotError("schedule event carried no github.event.schedule")
        # Matched as written: GitHub hands back the entry's own cron text.
        schedule = _own_line(
            own_entries, schedule_cron, lambda entry: entry.cron == schedule_cron
        )
        if not schedule.fires_at_most_daily:
            raise SlotError(
                f"cron {schedule_cron!r} fires more than once a day, so a late"
                " schedule delivery cannot be attributed to the slot GitHub fired"
            )
        return Resolution(schedule=schedule, slot=latest_slot(schedule, now))
    if event_name == "workflow_run":
        if parent is None:
            raise SlotError("workflow_run event carried no parent run")
        if parent.event not in SCHEDULED_PARENT_EVENTS:
            return Resolution(schedule=None, slot=None)
        if len(parent.entries) != 1:
            raise SlotError(
                f"the parent workflow carries {len(parent.entries)} cron lines, so a"
                " chained run cannot tell which one fired it"
            )
        schedule = utc_schedule(parent.entries[0])
        if not schedule.fires_at_most_daily:
            raise SlotError(
                f"the parent's cron {parent.entries[0].cron!r} fires more than once a"
                " day, so a chained run cannot be attributed to the slot that fired it"
            )
        slot = latest_slot(schedule, parent.created_at)
        if slot < now - RECORD_RETENTION:
            # The same bound as a dispatched slot's: a record this old may have
            # been pruned, so the ledger cannot say whether the work was done.
            raise SlotError(
                f"the parent's slot {format_slot(slot)} is older than the ledger keeps"
                f" records ({RECORD_RETENTION.days} days)"
            )
        return Resolution(schedule=schedule, slot=slot)
    return Resolution(schedule=None, slot=None)


def workflow_file_from_ref(workflow_ref: str) -> str:
    """The workflow's basename from GITHUB_WORKFLOW_REF (owner/repo/path@ref)."""
    path = workflow_ref.split("@", 1)[0]
    return path.rsplit("/", 1)[-1]


def _workflow_entries(
    repo: str, path: str, commit: str, read_text: ReadText
) -> tuple[ScheduleEntry, ...]:
    """The schedule entries of one workflow file as it was at `commit`."""
    try:
        workflow_files.check_workflow_path(path)
        text = read_text(repo, path, commit)
        document = workflow_files.parse(text, yq.executable())
    except (ValueError, GitHubError, yq.ParserUnavailable) as exc:
        # With no readable file there is no schedule entry to resolve the run
        # by; refuse it rather than guess.
        raise SlotError(f"cannot read workflow {path!r} at {commit!r}: {exc}") from exc
    workflow_file = path.rsplit("/", 1)[-1]
    return tuple(schedule_entries(workflow_file, triggers(document).get("schedule")))


def read_own_entries(
    repo: str, workflow_ref: str, workflow_sha: str, read_text: ReadText
) -> tuple[ScheduleEntry, ...]:
    """The run's own workflow's schedule entries, from the file
    GITHUB_WORKFLOW_REF (owner/repo/<path>@ref) names, at the commit the run
    executed (GITHUB_WORKFLOW_SHA)."""
    path = workflow_ref.split("@", 1)[0].split("/", 2)[-1]
    return _workflow_entries(repo, path, workflow_sha, read_text)


def _timestamp(text: str) -> datetime:
    # datetime.fromisoformat reads a trailing Z only from Python 3.11 on, and
    # GitHub writes one on every timestamp.
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        instant = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SlotError(f"parent created_at {text!r} is not a timestamp") from exc
    if instant.tzinfo is None:
        raise SlotError(f"parent created_at {text!r} carries no timezone")
    return instant


def read_parent(
    repo: str,
    path: str,
    head_sha: str,
    event: str,
    created_at: str,
    read_text: ReadText,
) -> Parent:
    """The parent run of a `workflow_run` run, with its workflow's schedule
    entries read from the file the parent ran, at the parent's commit."""
    created = _timestamp(created_at)
    entries = _workflow_entries(repo, path, head_sha, read_text)
    return Parent(event=event, entries=entries, created_at=created)


def _append(path: str, lines: Sequence[str]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(f"{line}\n" for line in lines)


def _outputs(path: str, outputs: dict[str, str]) -> None:
    _append(path, [f"{name}={value}" for name, value in outputs.items()])


def _slot_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github-cron-trigger slot",
        description="Resolve this run's slot and whether it is still owed.",
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--workflow-ref", required=True, help="GITHUB_WORKFLOW_REF")
    parser.add_argument("--workflow-sha", required=True, help="GITHUB_WORKFLOW_SHA")
    parser.add_argument("--event-name", required=True, help="github.event_name")
    parser.add_argument(
        "--payload-slot", default="", help="github.event.client_payload.slot"
    )
    parser.add_argument(
        "--payload-cron", default="", help="github.event.client_payload.cron"
    )
    parser.add_argument("--schedule-cron", default="", help="github.event.schedule")
    parser.add_argument(
        "--parent-path", default="", help="github.event.workflow_run.path"
    )
    parser.add_argument(
        "--parent-sha", default="", help="github.event.workflow_run.head_sha"
    )
    parser.add_argument(
        "--parent-event", default="", help="github.event.workflow_run.event"
    )
    parser.add_argument(
        "--parent-created-at", default="", help="github.event.workflow_run.created_at"
    )
    parser.add_argument(
        "--part", default="", help="record part, for runs recorded per leg"
    )
    parser.add_argument("--github-output", required=True, help="GITHUB_OUTPUT file")
    parser.add_argument(
        "--step-summary", default="", help="GITHUB_STEP_SUMMARY file, if any"
    )
    return parser


def _done_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github-cron-trigger done", description="Record this run's slot done."
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--workflow-ref", required=True, help="GITHUB_WORKFLOW_REF")
    parser.add_argument("--slot", required=True, help="the slot output of `slot`")
    parser.add_argument("--cron", required=True, help="the cron output of `slot`")
    parser.add_argument(
        "--part", default="", help="record part, for runs recorded per leg"
    )
    parser.add_argument("--sha", required=True, help="commit the run executed")
    return parser


def command_slot(args: argparse.Namespace, now: datetime, read_text: ReadText) -> int:
    workflow_file = workflow_file_from_ref(args.workflow_ref)
    parent = (
        read_parent(
            args.repo,
            args.parent_path,
            args.parent_sha,
            args.parent_event,
            args.parent_created_at,
            read_text,
        )
        if args.event_name == "workflow_run"
        else None
    )
    own_entries = (
        read_own_entries(args.repo, args.workflow_ref, args.workflow_sha, read_text)
        if args.event_name in OWN_LINE_EVENTS
        else ()
    )
    resolution = resolve(
        args.event_name,
        args.payload_slot,
        args.payload_cron,
        args.schedule_cron,
        own_entries,
        parent,
        now,
    )
    if resolution.schedule is None or resolution.slot is None:
        message = f"{workflow_file}: {args.event_name} run, not a scheduled delivery; running as usual."
        print(message)
        _outputs(
            args.github_output,
            {"scheduled": "false", "slot": "", "cron": "", "run": "true"},
        )
        if args.step_summary:
            _append(args.step_summary, [f"github-cron-trigger: {message}"])
        return 0
    schedule, slot = resolution.schedule, resolution.slot
    part = args.part or None
    what = f"{workflow_file} cron {schedule.canonical!r} slot {format_slot(slot)}" + (
        f" part {part}" if part else ""
    )
    done = slot_ledger.is_done(args.repo, workflow_file, schedule, slot, part)
    if done:
        message = f"{what}: already recorded done by another delivery; this run skips the work."
    else:
        message = f"{what}: not recorded yet; this run does the work."
    print(message)
    _outputs(
        args.github_output,
        {
            "scheduled": "true",
            "slot": format_slot(slot),
            "cron": schedule.canonical,
            "run": "false" if done else "true",
        },
    )
    if args.step_summary:
        _append(
            args.step_summary,
            [f"github-cron-trigger: {args.event_name} delivery of {message}"],
        )
    return 0


def command_done(args: argparse.Namespace) -> int:
    workflow_file = workflow_file_from_ref(args.workflow_ref)
    schedule = parse_cron(args.cron)
    slot = parse_slot(args.slot)
    if not is_occurrence(schedule, slot):
        raise SlotError(f"slot {args.slot} is not an instant of cron {args.cron!r}")
    part = args.part or None
    what = f"{workflow_file} cron {schedule.canonical!r} slot {args.slot}" + (
        f" part {part}" if part else ""
    )
    created = slot_ledger.record(
        args.repo, workflow_file, schedule, slot, args.sha, part
    )
    if created:
        print(f"{what}: recorded done at {args.sha}.")
    else:
        print(f"{what}: was already recorded done; nothing to add.")
    # Housekeeping, not the record: the record above already stands, so a
    # failure here warns rather than turning a finished run red. The next
    # record for this workflow retries it.
    try:
        removed = slot_ledger.prune(args.repo, workflow_file, slot - RECORD_RETENTION)
    except (GitHubError, ValueError) as exc:
        print(f"::warning::{workflow_file}: pruning old slot records failed: {exc}")
    else:
        if removed:
            print(
                f"{workflow_file}: pruned {len(removed)} record(s) older than {RECORD_RETENTION.days} days."
            )
    return 0


def _fail_closed(command: str, run: Callable[[], int]) -> int:
    try:
        return run()
    except (SlotError, GitHubError, yq.ParserUnavailable, ValueError) as exc:
        # A run that cannot establish or record its slot stops here, red,
        # rather than guessing - doing owed work twice, or skipping it, costs
        # more than a visible failure the other delivery can make up.
        print(f"::error::github-cron-trigger {command}: {exc}")
        return 1


def slot_main(
    argv: Sequence[str],
    now: datetime | None = None,
    read_text: ReadText = workflow_files.fetch_file,
) -> int:
    args = _slot_parser().parse_args(argv)
    current = now if now is not None else datetime.now(UTC)
    return _fail_closed("slot", lambda: command_slot(args, current, read_text))


def done_main(argv: Sequence[str]) -> int:
    args = _done_parser().parse_args(argv)
    return _fail_closed("done", lambda: command_done(args))
