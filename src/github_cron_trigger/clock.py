"""The clock: deliver each workflow's due scheduled slots on time.

Run on a short interval by a scheduler outside GitHub (launchd, a systemd
timer, any cron), on as many hosts as you like. Each tick reads the workflow
files of the repository's default branch through the API, so the host needs no
clone of it; works out for every enrolled `cron:` line the latest slot that has
come due; and sends a `repository_dispatch` of that slot to the workflow when no
clock has sent it yet. GitHub's own `schedule` stays as the backstop; the slot
action and the ledger make every delivery of one slot do its work once
(README.md).

WHAT IT SENDS, AND WHEN. Per cron line, the newest due slot only: after a sleep
the clock sends one delivery for a line, never a burst, and a slot it slept
through entirely is left to GitHub's backstop. A line with no mark at all
(clock_marks.py) is baselined - its current latest slot is marked without
sending - because that slot may have come due while nothing could record it:
the workflow may already have run it, before any clock served it or before it
carried the slot action, and with no ledger record of that run a dispatch would
repeat finished work. A workflow that stops being enrolled has its marks
removed, so enrolling it again baselines it again (a host asleep across the
merge that enrolled it, say, sends nothing that predates the enrollment).

WHO SENDS. Every decision is made against the marks in the repository, not
anything on the host, and a slot is sent only by the clock whose claim of it
succeeded. So two hosts ticking the same repository at once send each slot
once, and a host can join, leave or be rebuilt without carrying anything over.

ENROLLED means the workflow declares `repository_dispatch` with the type
`github-cron-trigger/<its own file name>`. An enrolled workflow that runs the
slot action must fire each line at most once a day: the slot action refuses a
`schedule` delivery of any other line, so GitHub's backstop run of such a
workflow would fail every time, and the clock reports it rather than delivering
it. One without the slot action - a reconciler, for which a repeated run is
harmless - may fire more often, since each dispatch names its exact slot.

A DRY RUN (no `--send`) reads everything a tick reads and reports what it would
do, and writes nothing: no mark, no dispatch, no clean-up.
"""

import argparse
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from . import default_branch, slot_ledger, yq
from .clock_marks import KEEP_PER_LINE, GitHubMarks, Mark, mark_for
from .cron_slots import (
    UTC,
    CronSchedule,
    UnsupportedCron,
    format_slot,
    latest_slot,
)
from .github import GitHubError, check_repo, gh_api
from .slot_ledger import cron_segment
from .workflow_triggers import (
    MisconfiguredWorkflow,
    dispatch_types,
    schedule_entries,
    triggers,
    utc_schedule,
)

EVENT_TYPE_PREFIX = "github-cron-trigger/"

# The slot action, recognized by the end of a step's `uses:` path, so the match
# does not depend on the owner it is referenced from (a fork, or a copy vendored
# under .github/actions) or on the version.
SLOT_ACTION = "github-cron-trigger/slot"


@dataclass(frozen=True)
class ScheduledWorkflow:
    """One workflow file's cron lines, and whether the clock may deliver them."""

    workflow_file: str
    schedules: tuple[CronSchedule, ...]
    enrolled: bool


def uses_slot_action(document: object) -> bool:
    """Whether a step of one of the workflow's jobs runs the slot action.

    Only the workflow's own steps are read. A slot action reached through a
    reusable workflow or another action is not seen, which leaves that
    workflow's once-a-day rule to the slot step's own refusal at run time.
    """
    jobs = document.get("jobs") if isinstance(document, Mapping) else None
    if not isinstance(jobs, Mapping):
        return False
    for job in jobs.values():
        steps = job.get("steps") if isinstance(job, Mapping) else None
        if not isinstance(steps, list):
            continue
        for step in steps:
            uses = step.get("uses") if isinstance(step, Mapping) else None
            if not isinstance(uses, str):
                continue
            action = uses.split("@", 1)[0].rstrip("/")
            if action == SLOT_ACTION or action.endswith("/" + SLOT_ACTION):
                return True
    return False


def scheduled_workflow(
    workflow_file: str, document: object
) -> ScheduledWorkflow | None:
    """The workflow's cron lines and enrollment, or None when the clock has
    nothing to track for it.

    For an enrolled workflow, a schedule or enrollment the clock cannot read
    raises MisconfiguredWorkflow (or UnsupportedCron) rather than being guessed
    at, so it is reported instead of silently never delivered. So does a line
    firing more than once a day in an enrolled workflow that runs the slot
    action, whose backstop runs would all fail (this module's header). A
    workflow that is not enrolled and carries a cron line outside the accepted
    grammar, or a schedule entry that sets a timezone, is not the clock's to
    deliver, and is left out.
    """
    on = triggers(document)
    own_type = EVENT_TYPE_PREFIX + workflow_file
    ours = [
        t
        for t in dispatch_types(workflow_file, on.get("repository_dispatch"))
        if t.startswith(EVENT_TYPE_PREFIX)
    ]
    # A type naming another file is a copied enrollment: the clock would send
    # this workflow's slots under a type it does not listen to.
    strays = [t for t in ours if t != own_type]
    if strays:
        raise MisconfiguredWorkflow(
            f"{workflow_file}: repository_dispatch types {strays} are not {own_type!r}"
        )
    enrolled = own_type in ours
    schedules: list[CronSchedule] = []
    for entry in schedule_entries(workflow_file, on.get("schedule")):
        try:
            schedules.append(utc_schedule(entry))
        except UnsupportedCron:
            if enrolled:
                raise
            return None
    if enrolled and not schedules:
        raise MisconfiguredWorkflow(
            f"{workflow_file}: enrolled as {own_type!r} but declares no cron: line"
        )
    if enrolled and uses_slot_action(document):
        frequent = [s.expression for s in schedules if not s.fires_at_most_daily]
        if frequent:
            raise MisconfiguredWorkflow(
                f"{workflow_file}: runs the slot action, so each cron line must fire"
                f" at most once a day, but {frequent} fire more often"
            )
    if not schedules:
        return None
    return ScheduledWorkflow(workflow_file, tuple(schedules), enrolled)


class Remote(Protocol):
    """What a tick reads and writes outside the host: the repository's marks,
    its slot ledger, and its dispatches. GitHubRemote is the real one."""

    def read_marks(self, repo: str) -> list[Mark]: ...

    def claim(self, repo: str, mark: Mark) -> bool: ...

    def release(self, repo: str, mark: Mark) -> None: ...

    def remove(self, repo: str, marks: Iterable[Mark]) -> None: ...

    def is_done(
        self, repo: str, workflow_file: str, schedule: CronSchedule, slot: datetime
    ) -> bool: ...

    def dispatch(
        self, repo: str, workflow_file: str, schedule: CronSchedule, slot: datetime
    ) -> None: ...


class GitHubRemote(GitHubMarks):
    """The repository through the GitHub API."""

    def read_marks(self, repo: str) -> list[Mark]:
        return self.read(repo)

    def is_done(
        self, repo: str, workflow_file: str, schedule: CronSchedule, slot: datetime
    ) -> bool:
        return slot_ledger.is_done(repo, workflow_file, schedule, slot)

    def dispatch(
        self, repo: str, workflow_file: str, schedule: CronSchedule, slot: datetime
    ) -> None:
        """Send one slot to one workflow as a repository_dispatch."""
        gh_api(
            [
                "--method",
                "POST",
                f"repos/{check_repo(repo)}/dispatches",
                "-f",
                f"event_type={EVENT_TYPE_PREFIX}{workflow_file}",
                "-f",
                f"client_payload[slot]={format_slot(slot)}",
                "-f",
                f"client_payload[cron]={schedule.canonical}",
            ]
        )


@dataclass(frozen=True)
class Step:
    """What a tick owes one enrolled cron line: `baseline` a line no clock has
    marked, or `deliver` its newest slot."""

    workflow: ScheduledWorkflow
    schedule: CronSchedule
    slot: datetime
    kind: Literal["baseline", "deliver"]


def newest_marks(marks: Iterable[Mark]) -> dict[tuple[str, str], datetime]:
    """The newest marked slot of each line, keyed (workflow file, segment)."""
    newest: dict[tuple[str, str], datetime] = {}
    for mark in marks:
        key = (mark.workflow_file, mark.segment)
        if key not in newest or newest[key] < mark.slot:
            newest[key] = mark.slot
    return newest


def plan(
    workflows: Sequence[ScheduledWorkflow], marks: Iterable[Mark], now: datetime
) -> list[Step]:
    """The enrolled lines whose newest slot this tick owes a step, in workflow
    order."""
    newest = newest_marks(marks)
    steps: list[Step] = []
    for workflow in workflows:
        if not workflow.enrolled:
            continue
        for schedule in workflow.schedules:
            slot = latest_slot(schedule, now)
            last = newest.get((workflow.workflow_file, cron_segment(schedule)))
            if last is None:
                steps.append(Step(workflow, schedule, slot, "baseline"))
            elif last < slot:
                steps.append(Step(workflow, schedule, slot, "deliver"))
    return steps


def stale_marks(
    workflows: Sequence[ScheduledWorkflow],
    marks: Iterable[Mark],
    unjudged: Collection[str] = (),
) -> list[Mark]:
    """The marks a tick removes: every mark of a line no enrolled workflow
    carries any more - disenrolled, changed or deleted - and, of each line's
    marks, all but the KEEP_PER_LINE newest.

    A workflow file in `unjudged` - present, but unreadable or misconfigured
    this tick - keeps every mark: removing them would make its lines baseline,
    not deliver, once the file reads again.
    """
    current = {
        (workflow.workflow_file, cron_segment(schedule))
        for workflow in workflows
        if workflow.enrolled
        for schedule in workflow.schedules
    }
    by_line: dict[tuple[str, str], list[Mark]] = {}
    for mark in marks:
        if mark.workflow_file in unjudged:
            continue
        by_line.setdefault((mark.workflow_file, mark.segment), []).append(mark)
    stale: list[Mark] = []
    for line, line_marks in sorted(by_line.items()):
        ordered = sorted(line_marks, key=lambda mark: mark.slot, reverse=True)
        stale.extend(ordered if line not in current else ordered[KEEP_PER_LINE:])
    return stale


def tick(
    repo: str,
    documents: Mapping[str, object],
    now: datetime,
    send: bool,
    remote: Remote,
    unreadable: Collection[str] = (),
) -> list[str]:
    """Run one tick against one repository and return the problems met.

    `unreadable` names the workflow files present on the default branch that
    could not be read or parsed. A problem with one workflow or one dispatch is
    reported and the rest of the tick goes on; the caller turns any problem into
    a failed run.
    """
    problems: list[str] = []
    workflows: list[ScheduledWorkflow] = []
    unjudged = set(unreadable)
    for workflow_file, document in sorted(documents.items()):
        try:
            workflow = scheduled_workflow(workflow_file, document)
        except (MisconfiguredWorkflow, UnsupportedCron) as exc:
            problems.append(str(exc))
            unjudged.add(workflow_file)
            continue
        if workflow is not None:
            workflows.append(workflow)
    try:
        marks = remote.read_marks(repo)
    except (GitHubError, ValueError) as exc:
        # With no marks there is no telling what was sent; sending anyway could
        # repeat a slot, so this tick sends nothing.
        problems.append(f"cannot read the clock's marks: {exc}")
        return problems
    for step in plan(workflows, marks, now):
        workflow_file = step.workflow.workflow_file
        what = f"{workflow_file} cron {step.schedule.canonical!r} slot {format_slot(step.slot)}"
        mark = mark_for(workflow_file, step.schedule, step.slot)
        if not send:
            verb = "would baseline" if step.kind == "baseline" else "would send"
            print(f"clock: {what}: {verb} (dry run)")
            continue
        try:
            if not remote.claim(repo, mark):
                print(f"clock: {what}: claimed by another clock; not sent")
                continue
            if step.kind == "baseline":
                print(
                    f"clock: {what}: no clock has marked this line; baselined without sending"
                )
                continue
            if remote.is_done(repo, workflow_file, step.schedule, step.slot):
                print(f"clock: {what}: already recorded done; not sent")
                continue
        except (GitHubError, ValueError) as exc:
            problems.append(f"{what}: {exc}")
            continue
        try:
            remote.dispatch(repo, workflow_file, step.schedule, step.slot)
        except (GitHubError, ValueError) as exc:
            # The mark comes off again, so the next tick on any host sends the
            # slot. A ValueError is a name no retry fixes; it is reported on
            # every tick.
            problems.append(f"{what}: {exc}")
            try:
                remote.release(repo, mark)
            except (GitHubError, ValueError) as release_exc:
                problems.append(f"{what}: releasing its mark failed: {release_exc}")
            continue
        print(f"clock: {what}: sent")
    stale = stale_marks(workflows, marks, unjudged)
    if stale and send:
        try:
            remote.remove(repo, stale)
        except (GitHubError, ValueError) as exc:
            # Housekeeping: a mark left behind only costs a listing entry, and
            # the next tick tries again.
            print(f"clock: removing {len(stale)} old mark(s) failed: {exc}")
    elif stale:
        print(f"clock: would remove {len(stale)} old mark(s) (dry run)")
    return problems


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github-cron-trigger tick",
        description="Deliver every enrolled workflow's due slots once.",
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument(
        "--send",
        action="store_true",
        help="claim and dispatch due slots (default: a dry run, which writes nothing)",
    )
    return parser


def main(
    argv: Sequence[str],
    now: datetime | None = None,
    load: default_branch.Loader = default_branch.load,
    remote: Remote | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        documents, unreadable = load(args.repo)
    except (GitHubError, ValueError, yq.ParserUnavailable) as exc:
        print(f"clock: cannot read any workflow of {args.repo}: {exc}")
        return 1
    problems = [f"{name}: unreadable: {reason}" for name, reason in unreadable.items()]
    current = now if now is not None else datetime.now(UTC)
    problems += tick(
        args.repo, documents, current, args.send, remote or GitHubRemote(), unreadable
    )
    for problem in problems:
        print(f"clock: problem: {problem}")
    return 1 if problems else 0
