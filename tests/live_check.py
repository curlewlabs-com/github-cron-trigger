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
GraphQL query. Each is run here as a step or a host would run it: a dispatched
slot of this workflow's own line resolves and reads the ledger, a line the
workflow does not carry is refused, and the missed-slot check reads everything.

THE CLOCKS. Two clock processes tick this repository with --send at the same
moment, as two hosts would, against clock-check.yml, a workflow enrolled for
this. With no marks, they baseline its newest slot once and send nothing; with
an older slot marked, exactly one of them sends the newest. The marks live in
the repository (clock_marks.py), and are removed again whatever the outcome.
"""

import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from github_cron_trigger import (
    clock,
    default_branch,
    freshness,
    slot_ledger,
    steps,
    workflow_files,
    yq,
)
from github_cron_trigger.clock_marks import GitHubMarks, Mark, mark_for
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
        cache = default_branch.Cache(Path(tmp))
        calls: list[str] = []
        parses: list[str] = []

        def counted(query: str, variables: Mapping[str, str]) -> Any:
            calls.append(query)
            return default_branch.graphql(query, variables)

        def parser() -> str:
            parses.append("yq")
            return yq.executable()

        first = default_branch.load(repo, cache, counted, parser)
        calls.clear()
        parses.clear()
        second = default_branch.load(repo, cache, counted, parser)
    _expect(
        second == first and len(calls) == 1 and not parses,
        f"an idle read of {len(first[0])} workflow file(s) is one query and no parse",
    )
    status = freshness.main(["--repo", repo])
    _expect(
        status & freshness.EXIT_INCOMPLETE == 0,
        "the missed-slot check reads everything",
    )


# The enrolled workflow the clock check runs real clocks against. Its yearly line
# always has a newest slot, and the slot before it, to claim; the workflow says
# why it exists.
CLOCK_CHECK = "clock-check.yml"
YEARLY = parse_cron("0 0 1 1 *")
MARKS = GitHubMarks()


def _clock_marks(repo: str) -> list[Mark]:
    return [mark for mark in MARKS.read(repo) if mark.workflow_file == CLOCK_CHECK]


def _concurrent_ticks(repo: str) -> list[str]:
    """Two clocks ticking the repository with --send at the same moment, as two
    hosts would; their output."""
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    command = [
        sys.executable,
        "-m",
        "github_cron_trigger",
        "tick",
        "--repo",
        repo,
        "--send",
    ]
    clocks = [
        subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=environment)
        for _ in range(2)
    ]
    outputs = [clock_process.communicate(timeout=600)[0] for clock_process in clocks]
    for output in outputs:
        print(output, end="")
    return outputs


def _default_branch_has(repo: str, workflow_file: str) -> bool:
    names = gh_api([f"repos/{repo}/contents/.github/workflows", "--jq", ".[].name"])
    return workflow_file in names.split()


def _sends(outputs: list[str]) -> int:
    return sum(
        line.startswith(f"clock: {CLOCK_CHECK} cron") and line.endswith(": sent")
        for output in outputs
        for line in output.splitlines()
    )


def clocks(repo: str, required: bool) -> None:
    """Two clocks at once: a line no clock has marked is baselined once and
    sent nothing, and a due slot is sent exactly once.

    The clock reads the default branch, so the pull request that adds or changes
    clock-check.yml cannot run this against its own copy. A pull request whose
    default branch lacks the workflow skips it, saying so; any other run
    (`required`) fails instead, so the default branch always runs it.
    """
    if not _default_branch_has(repo, CLOCK_CHECK):
        if required:
            raise AssertionError(f"{CLOCK_CHECK} is not on the default branch")
        print(f"skipped: {CLOCK_CHECK} is not on the default branch yet")
        return
    MARKS.remove(repo, _clock_marks(repo))
    now = datetime.now(UTC)
    newest = latest_slot(YEARLY, now)
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        status = clock.main(["--repo", repo])
    _expect(
        status == 0
        and "would baseline (dry run)" in printed.getvalue()
        and not _clock_marks(repo),
        "a dry run reads the repository, reports a baseline, and writes nothing",
    )
    outputs = _concurrent_ticks(repo)
    _expect(
        _sends(outputs) == 0 and [mark.slot for mark in _clock_marks(repo)] == [newest],
        "two clocks baselining at once mark the newest slot once and send nothing",
    )
    MARKS.remove(repo, _clock_marks(repo))
    MARKS.claim(
        repo,
        mark_for(
            CLOCK_CHECK, YEARLY, latest_slot(YEARLY, newest - timedelta(minutes=1))
        ),
    )
    outputs = _concurrent_ticks(repo)
    _expect(
        _sends(outputs) == 1 and newest in [mark.slot for mark in _clock_marks(repo)],
        "two clocks ticking a due slot at once send it exactly once",
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
    parser.add_argument(
        "--require-clock-check",
        action="store_true",
        help=f"fail rather than skip when {CLOCK_CHECK} is not on the default branch",
    )
    args = parser.parse_args()
    readers(args.repo, args.workflow_ref, args.workflow_sha)
    try:
        clocks(args.repo, args.require_clock_check)
    finally:
        MARKS.remove(args.repo, _clock_marks(args.repo))
    try:
        run(args.repo, args.sha, args.workflow_file)
    finally:
        cleanup(args.repo, args.workflow_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
