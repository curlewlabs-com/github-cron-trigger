"""The record, kept in GitHub, of which scheduled slots have been done.

One git ref per finished slot:

    refs/github-cron-trigger/<workflow file>/<cron line>/<YYYYMMDDTHHMMZ>[.<part>]

pointing at a blob that names the commit the run executed (record_note). A slot
is done when its ref exists; nothing reads what the ref points at.
<cron line> is the schedule's canonical expression with each space written as
'_' and each '*' as 'x', because git refuses a space and a '*' in a ref name:
`30 19 * * *` is `30_19_x_x_x`.

WHY THE CRON LINE IS IN THE KEY. A slot is one firing of one `cron:` line, and a
workflow may carry more than one line. Lines that fire in the same minute are
different slots, so a key holding only the workflow and the instant would let
one line's record make another line's run skip work it owes.

WHY GIT REFS. Creating a ref is atomic in GitHub's API: a second create of an
existing ref fails with 422 "Reference already exists", so of the runs
recording one slot, only the first create succeeds. A check is one read, a ref
sends no notification, and a ref outside refs/heads and refs/tags is not a
branch or tag, so nothing lists it in the UI or fetches it by default.

WHY A BLOB, NOT THE RUN'S COMMIT. The record step's workflow token can be
refused (HTTP 403) a ref at the run's own commit once workflow files on the
default branch have changed since that commit. The record step runs last, so
that refusal would leave a slot whose work is done unrecorded, for the other
delivery to do again. A record points at a blob that names the commit instead,
and this project's CI creates one with that same token (tests/live_check.py).

WHY LEAVES ONLY. Git refuses a ref beneath an existing one (the directory/file
conflict), so a part - one leg of a run whose legs are recorded separately - is
a suffix on the slot's leaf, never a child of it.

Every call goes through github.gh_api, so a ledger failure is a
github.GitHubError.
"""

import re
from datetime import datetime

from .cron_slots import CronSchedule, ledger_key
from .github import GitHubError, HttpError, check_repo, gh_api

REF_NAMESPACE = "github-cron-trigger"

# A workflow file's basename, and a part name. Anchored and narrow because each
# becomes a path segment of a ref and of an API URL: a '/' or '..' here would
# address another workflow's records, or somewhere outside the namespace.
_WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$")
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# A full git object name: the commit a record names, and the blob it points at.
_OBJECT = re.compile(r"^[0-9a-f]{40}$")

# A record below a workflow: the cron-line segment cron_segment writes, then the
# leaf ledger_key writes, optionally followed by a part suffix.
_SEGMENT_FIELD = r"(?:x|\d{1,2}(?:,\d{1,2})*)"
_RECORD = re.compile(
    rf"^(?P<segment>{_SEGMENT_FIELD}(?:_{_SEGMENT_FIELD}){{4}})"
    r"/(?P<key>\d{8}T\d{4}Z)(?:\.(?P<part>[A-Za-z0-9][A-Za-z0-9_-]*))?$"
)


def cron_segment(schedule: CronSchedule) -> str:
    """The cron line as a ref path segment, from its canonical expression."""
    return schedule.canonical.replace(" ", "_").replace("*", "x")


def _check_workflow_file(workflow_file: str) -> str:
    if not _WORKFLOW_FILE.match(workflow_file):
        raise ValueError(f"workflow file {workflow_file!r} is not a workflow basename")
    return workflow_file


def ref_path(
    workflow_file: str,
    schedule: CronSchedule,
    slot: datetime,
    part: str | None = None,
) -> str:
    """The ref path under `refs/` that records this slot (and part) done."""
    leaf = ledger_key(slot)
    if part:
        if not _PART.match(part):
            raise ValueError(f"part {part!r} must be letters, digits, '-' or '_'")
        leaf = f"{leaf}.{part}"
    segment = cron_segment(schedule)
    return f"{REF_NAMESPACE}/{_check_workflow_file(workflow_file)}/{segment}/{leaf}"


def is_done(
    repo: str,
    workflow_file: str,
    schedule: CronSchedule,
    slot: datetime,
    part: str | None = None,
) -> bool:
    """Whether the ledger records this slot (and part) as done."""
    path = ref_path(workflow_file, schedule, slot, part)
    try:
        # git/ref/ (singular) matches exactly; git/matching-refs/ would also
        # answer for a longer ref that merely starts with this one.
        gh_api([f"repos/{check_repo(repo)}/git/ref/{path}"])
    except HttpError as exc:
        if exc.status == 404:
            return False
        raise
    return True


def record_note(sha: str) -> str:
    """The text of a record's blob: which commit the recording run executed.
    Nothing reads it back; it answers a person asking where a record came from."""
    return f"github-cron-trigger: recorded by a run at commit {sha}\n"


def record(
    repo: str,
    workflow_file: str,
    schedule: CronSchedule,
    slot: datetime,
    sha: str,
    part: str | None = None,
) -> bool:
    """Record the slot done: a ref pointing at a blob that names `sha`, the
    commit the run executed (the module docstring says why not at `sha`).

    True when this call created the record, False when it already existed -
    another delivery of the same slot got there first, or this call's own retry
    found its earlier attempt applied (create_ref). Either way the slot is
    recorded.
    """
    if not _OBJECT.match(sha):
        raise ValueError(f"commit {sha!r} is not a full object name")
    return create_ref(
        repo, ref_path(workflow_file, schedule, slot, part), record_note(sha)
    )


def create_ref(repo: str, path: str, note: str) -> bool:
    """Create `refs/<path>` pointing at a blob holding `note`, atomically.

    True when this call created the ref, False when it already existed - another
    caller got there first, or this call's own retry found its earlier attempt
    applied. The blob is written first, so a failed ref create can leave it
    behind, unreferenced and inert. Writing the same note again names the same
    blob, which is what makes a retried call harmless.
    """
    repo = check_repo(repo)
    blob = gh_api(
        [
            "--method",
            "POST",
            f"repos/{repo}/git/blobs",
            "-f",
            f"content={note}",
            "-f",
            "encoding=utf-8",
            "--jq",
            ".sha",
        ]
    ).strip()
    if not _OBJECT.match(blob):
        raise GitHubError(f"blob create for {path} answered {blob!r}, not a blob name")
    try:
        gh_api(
            [
                "--method",
                "POST",
                f"repos/{repo}/git/refs",
                "-f",
                f"ref=refs/{path}",
                "-f",
                f"sha={blob}",
            ]
        )
    except HttpError as exc:
        if exc.status == 422 and "already exists" in exc.message.lower():
            return False
        raise
    return True


def delete_ref(repo: str, path: str) -> None:
    """Delete `refs/<path>`. An already-absent ref is not an error."""
    try:
        gh_api(["--method", "DELETE", f"repos/{check_repo(repo)}/git/refs/{path}"])
    except HttpError as exc:
        # GitHub answers a delete of a missing ref with 422 "Reference does not
        # exist" rather than a 404; any other 422 is a real failure.
        missing = exc.status == 404 or (
            exc.status == 422 and "does not exist" in exc.message.lower()
        )
        if not missing:
            raise


def list_refs(repo: str, prefix: str) -> list[str]:
    """Every ref under `refs/<prefix>`, as the part after the prefix, sorted.
    `prefix` ends in '/', so a prefix match cannot reach a sibling whose name
    merely starts with the last segment."""
    if not prefix.endswith("/"):
        raise ValueError(f"ref prefix {prefix!r} must end in '/'")
    out = gh_api(
        [
            "--paginate",
            f"repos/{check_repo(repo)}/git/matching-refs/{prefix}",
            "--jq",
            ".[].ref",
        ]
    )
    full = f"refs/{prefix}"
    return sorted(
        line[len(full) :] for line in out.splitlines() if line.startswith(full)
    )


def records(repo: str, workflow_file: str) -> list[str]:
    """Every record one workflow holds, as `<cron line>/<leaf>`, sorted."""
    return list_refs(repo, f"{REF_NAMESPACE}/{_check_workflow_file(workflow_file)}/")


def delete(repo: str, workflow_file: str, record_name: str) -> None:
    """Remove one record. An already-absent record is not an error."""
    if not _RECORD.match(record_name):
        raise ValueError(f"record {record_name!r} is not a ledger record")
    delete_ref(
        repo, f"{REF_NAMESPACE}/{_check_workflow_file(workflow_file)}/{record_name}"
    )


def record_slot_key(record_name: str) -> str | None:
    """The YYYYMMDDTHHMMZ key of a record, or None for one not in the ledger's form."""
    match = _RECORD.match(record_name)
    return match.group("key") if match else None


def prune(repo: str, workflow_file: str, before: datetime) -> list[str]:
    """Delete one workflow's records for slots before `before`; return them.

    Covers every cron line of the workflow. A record not in the ledger's own
    form is left alone: this only removes what the ledger itself wrote.
    """
    cutoff = ledger_key(before)
    removed = []
    for record_name in records(repo, workflow_file):
        key = record_slot_key(record_name)
        # The key format sorts lexically in time order, so string comparison is
        # slot order.
        if key is not None and key < cutoff:
            delete(repo, workflow_file, record_name)
            removed.append(record_name)
    return removed
