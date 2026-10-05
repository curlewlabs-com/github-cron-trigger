"""The clock's record of what it has sent, kept in the served repository.

One git ref per slot a clock claimed:

    refs/github-cron-trigger-clock/<workflow file>/<cron line>/<YYYYMMDDTHHMMZ>

in the ledger's own encoding (slot_ledger.py), pointing at a blob that says
which slot it marks. Nothing reads what a mark points at.

WHY IN THE REPOSITORY. Kept on the host, a clock's memory ties delivery to that
one host: while it sleeps every slot waits for GitHub's backstop, and a second
host would send every slot again. Kept here, any number of hosts can tick the
same repository, a host can be replaced with nothing to copy, and the record
can be read from the repository it describes.

WHY A CLAIM BEFORE THE SEND. Creating a ref is atomic: of the clocks that
create the same mark, exactly one succeeds and the rest are told it already
exists. A clock sends only after its create succeeded, so each slot is sent by
one clock however many tick at once. A send that fails releases its mark, so
the next tick on any host tries the slot again. A clock stopped between its
claim and its send leaves a slot marked and not sent; GitHub's schedule still
delivers that one, late.

This is not the slot ledger. The ledger records slots whose work finished,
whichever delivery did it, and the slot action reads it; marks record slots a
clock sent, for every enrolled workflow, reconcilers included, and only clocks
read them.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .cron_slots import CronSchedule, format_slot, ledger_key, parse_ledger_key
from .slot_ledger import create_ref, cron_segment, delete_ref, list_refs

MARK_NAMESPACE = "github-cron-trigger-clock"

# How many of a line's newest marks pruning keeps. Two, not one: a clock whose
# send fails releases its new mark, and the one before it must still be there,
# or the line would read as never sent and baseline instead of retrying.
KEEP_PER_LINE = 2

_WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$")
_SEGMENT_FIELD = r"(?:x|\d{1,2}(?:,\d{1,2})*)"
_MARK = re.compile(
    rf"^(?P<file>[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml)"
    rf"/(?P<segment>{_SEGMENT_FIELD}(?:_{_SEGMENT_FIELD}){{4}})"
    r"/(?P<key>\d{8}T\d{4}Z)$"
)


@dataclass(frozen=True, order=True)
class Mark:
    """One slot a clock claimed: the workflow file, the cron line as its ref
    segment (slot_ledger.cron_segment), and the slot."""

    workflow_file: str
    segment: str
    slot: datetime

    @property
    def path(self) -> str:
        """The ref path under `refs/`."""
        return f"{MARK_NAMESPACE}/{self.workflow_file}/{self.segment}/{ledger_key(self.slot)}"


def mark_for(workflow_file: str, schedule: CronSchedule, slot: datetime) -> Mark:
    if not _WORKFLOW_FILE.match(workflow_file):
        raise ValueError(f"workflow file {workflow_file!r} is not a workflow basename")
    return Mark(workflow_file, cron_segment(schedule), slot)


def parse_mark(name: str) -> Mark | None:
    """The mark a ref name under the namespace holds, or None for a name the
    clock did not write: another form, or a key naming no instant."""
    match = _MARK.match(name)
    if match is None:
        return None
    try:
        slot = parse_ledger_key(match.group("key"))
    except ValueError:
        return None
    return Mark(match.group("file"), match.group("segment"), slot)


def note(mark: Mark) -> str:
    """The text of a mark's blob. It names the slot and nothing about the host,
    since a public repository's refs are readable by anyone."""
    return f"github-cron-trigger clock: claimed {mark.workflow_file} slot {format_slot(mark.slot)}\n"


class GitHubMarks:
    """Marks read and written through the GitHub API."""

    def read(self, repo: str) -> list[Mark]:
        """Every mark the repository holds. A ref in the namespace that is not
        in the clock's form is left out, and never deleted."""
        marks = [parse_mark(name) for name in list_refs(repo, f"{MARK_NAMESPACE}/")]
        return sorted(mark for mark in marks if mark is not None)

    def claim(self, repo: str, mark: Mark) -> bool:
        """Create the mark; True when this call created it."""
        return create_ref(repo, mark.path, note(mark))

    def release(self, repo: str, mark: Mark) -> None:
        delete_ref(repo, mark.path)

    def remove(self, repo: str, marks: Iterable[Mark]) -> None:
        for mark in marks:
            delete_ref(repo, mark.path)
