"""Check a checkout's workflow files for what the clock or the slot action would
refuse, so a workflow that can never be delivered fails before it merges.

The clock reads the default branch, and the slot action reads a workflow only
when a delivery of it arrives, so a file either one refuses merges green and
then fails on every tick or every scheduled run. This reads the files from
disk, through the same parser and the same rules, and names each problem:

- a file the parser refuses (workflow_files.parse), as the clock reports one;
- in an enrolled workflow, whatever the clock refuses
  (clock.scheduled_workflow), a line firing more than once a day in one that
  runs the slot action included;
- in a workflow that runs the slot action without enrolling, a line the slot
  action refuses: GitHub's own `schedule` still delivers it, and the slot step
  fails each such delivery (steps.resolve);
- in a workflow that runs the slot action and chains through `workflow_run`,
  a parent whose line the slot action cannot attribute
  (freshness.chained_lines).

It reads nothing from GitHub, so a pull request's checks can run it with no
token.
"""

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import workflow_files, yq
from .clock import frequent_line_problem, scheduled_workflow, uses_slot_action
from .cron_slots import CronSchedule, UnsupportedCron
from .freshness import chained_lines
from .workflow_triggers import (
    MisconfiguredWorkflow,
    schedule_entries,
    triggers,
    utc_schedule,
)


def unenrolled_slot_problems(workflow_file: str, document: object) -> list[str]:
    """What the slot action refuses in the schedule of a workflow the clock does
    not deliver; scheduled_workflow already holds an enrolled one to the same
    rules."""
    try:
        entries = schedule_entries(workflow_file, triggers(document).get("schedule"))
    except MisconfiguredWorkflow as exc:
        return [str(exc)]
    problems: list[str] = []
    schedules: list[CronSchedule] = []
    for entry in entries:
        try:
            schedules.append(utc_schedule(entry))
        except UnsupportedCron as exc:
            problems.append(f"{workflow_file}: {exc}")
    frequent = frequent_line_problem(workflow_file, schedules)
    if frequent is not None:
        problems.append(frequent)
    return problems


def problems(documents: Mapping[str, object]) -> list[str]:
    """Every problem in the parsed workflows, by file name; `documents` holds
    the whole directory, since a chained workflow's parent is another file."""
    found: list[str] = []
    for workflow_file, document in sorted(documents.items()):
        slot_action = uses_slot_action(document)
        try:
            scheduled = scheduled_workflow(workflow_file, document)
        except MisconfiguredWorkflow as exc:
            found.append(str(exc))
        except UnsupportedCron as exc:
            # Raised from the line alone, so its message names no file.
            found.append(f"{workflow_file}: {exc}")
        else:
            if slot_action and (scheduled is None or not scheduled.enrolled):
                found += unenrolled_slot_problems(workflow_file, document)
        if slot_action:
            found += chained_lines(workflow_file, document, documents)[1]
    return found


def read_directory(directory: Path) -> dict[str, str]:
    """The text of each workflow file directly in `directory`, by file name.
    GitHub reads no subdirectory of .github/workflows, so neither does this."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.iterdir())
        if path.is_file() and path.name.endswith(workflow_files.WORKFLOW_SUFFIXES)
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github-cron-trigger check",
        description=(
            "Name what the clock or the slot action would refuse in a"
            " checkout's workflow files."
        ),
    )
    parser.add_argument(
        "--workflows",
        type=Path,
        default=Path(workflow_files.WORKFLOW_DIRECTORY),
        help="the workflow directory (default: %(default)s, from the current directory)",
    )
    return parser


def main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        texts = read_directory(args.workflows)
        documents, unreadable = workflow_files.parse_all(texts, yq.executable())
    except (OSError, UnicodeDecodeError, yq.ParserUnavailable) as exc:
        print(f"check: cannot read the workflows in {args.workflows}: {exc}")
        return 1
    found = [f"{name}: unreadable: {reason}" for name, reason in unreadable.items()]
    found += problems(documents)
    for problem in found:
        print(f"check: problem: {problem}")
    if not found:
        print(f"check: {len(texts)} workflow file(s) in {args.workflows}, no problem")
    return 1 if found else 0
