"""The clock: deliver each workflow's due scheduled slots on time.

Run on a short interval by a scheduler outside GitHub (launchd, a systemd
timer, any cron). Each tick reads the workflow files of the repository's
default branch through the API, so the host needs no clone of it; works out for
every `cron:` line the latest slot that has come due; and sends a
`repository_dispatch` of that slot to the workflow when it is enrolled and the
slot has not been delivered yet. GitHub's own `schedule` stays as the backstop;
the slot action and the ledger make every delivery of one slot do its work
once (README.md).

WHAT IT SENDS, AND WHEN. Per cron line, the newest due slot only: after a sleep
the clock sends one delivery for a line, never a burst, and a slot it slept
through entirely is left to GitHub's backstop. A line is baselined - its current
latest slot is marked as handled without sending - when the clock sees it for
the first time, and when its workflow's enrollment has changed since the line
was last handled (the host slept across the merge that enrolled it, say).
Either way the slot may have come due while nothing could record it: the
workflow may already have run it, before the clock was watching or before it
carried the slot action, and with no ledger record of that run a dispatch would
repeat finished work.

ENROLLED means the workflow declares `repository_dispatch` with the type
`github-cron-trigger/<its own file name>`. An enrolled workflow that runs the
slot action must fire each line at most once a day: the slot action refuses a
`schedule` delivery of any other line, so GitHub's backstop run of such a
workflow would fail every time, and the clock reports it rather than delivering
it. One without the slot action - a reconciler, for which a repeated run is
harmless - may fire more often, since each dispatch names its exact slot. Every
other scheduled workflow is tracked too, and in shadow mode each line's due
slots are logged for all of them, which is how the slot math is checked against
GitHub's own schedule before anything is sent.

STATE is a local JSON file holding, per line, the last slot handled and whether
the line's workflow was enrolled then. It only decides what to send next;
losing it re-baselines every line, which sends nothing, so it is a cache rather
than a record.
"""

import argparse
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from . import slot_ledger, workflow_files, yq
from .cron_slots import (
    UTC,
    CronSchedule,
    UnsupportedCron,
    format_slot,
    latest_slot,
    parse_slot,
)
from .github import GitHubError, check_repo, gh_api
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


def state_key(workflow_file: str, schedule: CronSchedule) -> str:
    return f"{workflow_file} {schedule.canonical}"


@dataclass(frozen=True)
class Handled:
    """The last slot a tick handled for one line, and whether the line's
    workflow was enrolled at the time."""

    slot: datetime
    enrolled: bool


@dataclass(frozen=True)
class Step:
    """What a tick owes one cron line: `baseline` a line seen for the first
    time or since its workflow's enrollment changed, or `deliver` its newest
    slot."""

    workflow: ScheduledWorkflow
    schedule: CronSchedule
    slot: datetime
    kind: Literal["baseline", "deliver"]


def plan(
    workflows: list[ScheduledWorkflow], state: Mapping[str, Handled], now: datetime
) -> list[Step]:
    """The lines whose newest slot this tick owes a step, in workflow order."""
    steps: list[Step] = []
    for workflow in workflows:
        for schedule in workflow.schedules:
            slot = latest_slot(schedule, now)
            last = state.get(state_key(workflow.workflow_file, schedule))
            if last is None or last.enrolled != workflow.enrolled:
                steps.append(Step(workflow, schedule, slot, "baseline"))
            elif last.slot < slot:
                steps.append(Step(workflow, schedule, slot, "deliver"))
    return steps


def _read_handled(value: object) -> Handled:
    if not isinstance(value, Mapping):
        raise TypeError(f"entry {value!r} is not a map")
    slot, enrolled = value.get("slot"), value.get("enrolled")
    if not isinstance(slot, str) or not isinstance(enrolled, bool):
        raise TypeError(f"entry {value!r} lacks a slot string or an enrolled flag")
    return Handled(parse_slot(slot), enrolled)


def load_state(path: Path) -> dict[str, Handled]:
    """The last slot handled per line; empty for a missing or unreadable file.

    An unreadable file is reported and treated as empty: every line then
    re-baselines, which sends nothing, so the safe reading of a lost state is
    no state.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("not a JSON object")
        return {key: _read_handled(value) for key, value in raw.items()}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        print(f"clock: state file {path} unreadable ({exc}); re-baselining every line")
        return {}


def save_state(path: Path, state: Mapping[str, Handled]) -> None:
    """Write the state atomically, so a crash mid-write leaves the old file."""
    serialized = {
        key: {"slot": format_slot(handled.slot), "enrolled": handled.enrolled}
        for key, handled in sorted(state.items())
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(serialized, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def dispatch(
    repo: str, workflow_file: str, schedule: CronSchedule, slot: datetime
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


def tick(
    repo: str,
    documents: Mapping[str, object],
    state: dict[str, Handled],
    now: datetime,
    send: bool,
) -> list[str]:
    """Run one tick: update `state` in place and return the problems met.

    A problem with one workflow or one dispatch is reported and the rest of the
    tick goes on; the caller turns any problem into a failed run.
    """
    problems: list[str] = []
    workflows: list[ScheduledWorkflow] = []
    for workflow_file, document in sorted(documents.items()):
        try:
            workflow = scheduled_workflow(workflow_file, document)
        except (MisconfiguredWorkflow, UnsupportedCron) as exc:
            problems.append(str(exc))
            continue
        if workflow is not None:
            workflows.append(workflow)
    for step in plan(workflows, state, now):
        workflow_file = step.workflow.workflow_file
        key = state_key(workflow_file, step.schedule)
        handled = Handled(step.slot, step.workflow.enrolled)
        what = f"{workflow_file} cron {step.schedule.canonical!r} slot {format_slot(step.slot)}"
        if step.kind == "baseline":
            reason = "first seen" if key not in state else "enrollment changed"
            print(f"clock: {what}: {reason}; baselined without sending")
            state[key] = handled
            continue
        if not send:
            enrolled = "enrolled" if step.workflow.enrolled else "not enrolled"
            print(f"clock: {what}: would send ({enrolled}; shadow mode)")
            state[key] = handled
            continue
        if not step.workflow.enrolled:
            state[key] = handled
            continue
        try:
            if slot_ledger.is_done(repo, workflow_file, step.schedule, step.slot):
                print(f"clock: {what}: already recorded done; not sent")
            else:
                dispatch(repo, workflow_file, step.schedule, step.slot)
                print(f"clock: {what}: sent")
        except (GitHubError, ValueError) as exc:
            # Left out of the state, so the next tick tries this slot again. A
            # ValueError is the ledger refusing this workflow's file name or the
            # repository; no retry fixes it, so it is reported every tick.
            problems.append(f"{what}: {exc}")
            continue
        state[key] = handled
    return problems


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github-cron-trigger tick",
        description="Deliver every enrolled workflow's due slots once.",
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument(
        "--state",
        required=True,
        type=Path,
        help="state file, one per repository (a cache; see the README)",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="dispatch due slots to enrolled workflows (default: shadow mode, log only)",
    )
    return parser


def main(
    argv: Sequence[str],
    now: datetime | None = None,
    load: workflow_files.Loader = workflow_files.load_default_branch,
) -> int:
    args = _parser().parse_args(argv)
    try:
        documents, unreadable = load(args.repo)
    except (GitHubError, ValueError, yq.ParserUnavailable) as exc:
        print(f"clock: cannot read any workflow of {args.repo}: {exc}")
        return 1
    state = load_state(args.state)
    problems = [f"{name}: unreadable: {reason}" for name, reason in unreadable.items()]
    current = now if now is not None else datetime.now(UTC)
    problems += tick(args.repo, documents, state, current, args.send)
    save_state(args.state, state)
    for problem in problems:
        print(f"clock: problem: {problem}")
    return 1 if problems else 0
