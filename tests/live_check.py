"""Exercise this project against the live GitHub API, then clean up.

Run from the repository root in CI, as `python3 -m tests.live_check`, with a
token holding `contents: write` - the grant an enrolled workflow's record step
holds.

THE LEDGER. Its correctness rests on GitHub behavior no unit test can
establish, and this checks each piece:

- a first create of a ref succeeds, and a second create of the same ref is
  refused, which is what makes a record atomic;
- a record points at a blob naming the run's commit (slot_ledger.py says why
  not at the commit itself);
- the exact-match read answers for a recorded slot and not for its neighbour;
- a part record sits beside the slot's own record without colliding with it;
- another cron line's record for the same instant is a separate record;
- the prefix listing returns this workflow's records and nothing else's;
- prune removes records before its cutoff and keeps later ones, and deleting an
  already-deleted record is not an error.

Everything is written under a workflow name unique to the run, so concurrent
checks cannot collide, and removed again whatever the outcome.

THE READERS. The slot command reads the running workflow's own file through the
contents API at the run's commit and parses it with the runner's yq; the clock
and the missed-slot check read every workflow of the default branch in one
GraphQL query. Each is run here as a step or a host would run it: a dispatched slot of this workflow's own line resolves and reads
the ledger, a line the workflow does not carry is refused, a shadow-mode tick
baselines this repository's lines without sending, and the missed-slot check
reads everything.
"""

import argparse
import contextlib
import io
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from github_cron_trigger import clock, freshness, slot_ledger, steps, workflow_files
from github_cron_trigger.cron_slots import UTC, format_slot, latest_slot, parse_cron
from github_cron_trigger.github import gh_api
from github_cron_trigger.workflow_triggers import utc_schedule

# Fixed, long-past slots: nothing enrolled can ever own records for them, and a
# fixed value keeps the check deterministic. 2000-01-01 was a Saturday, so the
# daily line and the Saturday line fire together at EARLY.
DAILY = parse_cron("0 0 * * *")
SATURDAY = parse_cron("0 0 * * 6")
EARLY = datetime(2000, 1, 1, 0, 0, tzinfo=UTC)
LATE = EARLY + timedelta(days=1)


def _expect(condition: bool, what: str) -> None:
    if not condition:
        raise AssertionError(what)
    print(f"ok: {what}")


def run(repo: str, sha: str, workflow_file: str) -> None:
    _expect(
        not slot_ledger.is_done(repo, workflow_file, DAILY, EARLY),
        "an unrecorded slot reads as not done",
    )
    _expect(
        slot_ledger.record(repo, workflow_file, DAILY, EARLY, sha),
        "the first record of a slot creates it",
    )
    _expect(
        not slot_ledger.record(repo, workflow_file, DAILY, EARLY, sha),
        "a second record of the same slot is refused and reported as already there",
    )
    path = slot_ledger.ref_path(workflow_file, DAILY, EARLY)
    target = gh_api([f"repos/{repo}/git/ref/{path}", "--jq", ".object.type"]).strip()
    _expect(target == "blob", "a record points at a blob, not at the run's commit")
    _expect(
        slot_ledger.is_done(repo, workflow_file, DAILY, EARLY),
        "a recorded slot reads as done",
    )
    _expect(
        not slot_ledger.is_done(repo, workflow_file, DAILY, LATE),
        "the neighbouring slot still reads as not done",
    )
    _expect(
        not slot_ledger.is_done(repo, workflow_file, SATURDAY, EARLY),
        "another line firing at the same instant still reads as not done",
    )
    _expect(
        slot_ledger.record(repo, workflow_file, SATURDAY, EARLY, sha),
        "another line's record for the same instant creates",
    )
    _expect(
        slot_ledger.record(repo, workflow_file, DAILY, EARLY, sha, part="leg"),
        "a part record beside the slot's own record creates",
    )
    _expect(
        slot_ledger.is_done(repo, workflow_file, DAILY, EARLY, part="leg"),
        "the part record reads as done",
    )
    slot_ledger.record(repo, workflow_file, DAILY, LATE, sha)
    listed = slot_ledger.records(repo, workflow_file)
    _expect(
        listed
        == [
            "0_0_x_x_6/20000101T0000Z",
            "0_0_x_x_x/20000101T0000Z",
            "0_0_x_x_x/20000101T0000Z.leg",
            "0_0_x_x_x/20000102T0000Z",
        ],
        f"the listing holds exactly this workflow's records ({listed})",
    )
    removed = slot_ledger.prune(repo, workflow_file, LATE)
    _expect(
        removed
        == [
            "0_0_x_x_6/20000101T0000Z",
            "0_0_x_x_x/20000101T0000Z",
            "0_0_x_x_x/20000101T0000Z.leg",
        ],
        f"prune removes the records before its cutoff, across lines ({removed})",
    )
    _expect(
        slot_ledger.records(repo, workflow_file) == ["0_0_x_x_x/20000102T0000Z"],
        "prune keeps the record at its cutoff",
    )
    slot_ledger.delete(repo, workflow_file, "0_0_x_x_x/20000101T0000Z")
    print("ok: deleting an already-deleted record is not an error")


def cleanup(repo: str, workflow_file: str) -> None:
    for record_name in slot_ledger.records(repo, workflow_file):
        slot_ledger.delete(repo, workflow_file, record_name)
    remaining = slot_ledger.records(repo, workflow_file)
    if remaining:
        raise AssertionError(f"cleanup left records behind: {remaining}")
    print(f"cleaned up every record under {workflow_file}")


def _slot(arguments: list[str]) -> tuple[int, dict[str, str], str]:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "output"
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            status = steps.slot_main([*arguments, "--github-output", str(output)])
        lines = output.read_text().splitlines() if output.exists() else []
    print(printed.getvalue(), end="")
    return status, dict(line.split("=", 1) for line in lines), printed.getvalue()


def readers(repo: str, workflow_ref: str, workflow_sha: str) -> None:
    entries = steps.read_own_entries(
        repo, workflow_ref, workflow_sha, workflow_files.fetch_file
    )
    _expect(bool(entries), f"this workflow's own schedule entries are read ({entries})")
    schedule = utc_schedule(entries[0])
    slot = latest_slot(schedule, datetime.now(UTC))
    own = [
        "--repo",
        repo,
        "--workflow-ref",
        workflow_ref,
        "--workflow-sha",
        workflow_sha,
        "--event-name",
        "repository_dispatch",
        "--payload-slot",
        format_slot(slot),
    ]
    status, outputs, _ = _slot([*own, "--payload-cron", schedule.canonical])
    _expect(
        status == 0
        and outputs.get("scheduled") == "true"
        and outputs.get("cron") == schedule.canonical
        and outputs.get("slot") == format_slot(slot)
        and outputs.get("run") == "true",
        f"a dispatch of this workflow's line resolves and is owed ({outputs})",
    )
    other = "0 0 31 12 *" if schedule.canonical != "0 0 31 12 *" else "1 0 31 12 *"
    status, outputs, printed = _slot([*own, "--payload-cron", other])
    _expect(
        status == 1 and not outputs and "is not one of" in printed,
        "a dispatch of a line this workflow does not carry is refused",
    )
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        status = clock.main(["--repo", repo, "--state", str(state)])
        handled = clock.load_state(state)
    _expect(
        status == 0 and bool(handled),
        f"a shadow-mode tick reads the default branch and baselines {len(handled)} line(s)",
    )
    status = freshness.main(["--repo", repo])
    _expect(
        status & freshness.EXIT_INCOMPLETE == 0,
        "the missed-slot check reads everything",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--sha", required=True, help="the commit the records name")
    parser.add_argument(
        "--workflow-file", required=True, help="a name unique to this run, ending .yml"
    )
    parser.add_argument("--workflow-ref", required=True, help="GITHUB_WORKFLOW_REF")
    parser.add_argument("--workflow-sha", required=True, help="GITHUB_WORKFLOW_SHA")
    args = parser.parse_args()
    readers(args.repo, args.workflow_ref, args.workflow_sha)
    try:
        run(args.repo, args.sha, args.workflow_file)
    finally:
        cleanup(args.repo, args.workflow_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
