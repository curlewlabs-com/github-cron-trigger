"""Unit tests for the clock's decisions: what is enrolled, and what a tick owes.

The failures these guard are the ones that make a delivery wrong without making
it loud: a workflow read as enrolled when it is not (the clock would send slots
nothing listens for) or missed when it is (it would never be delivered on
time); a first tick, or the first tick after an enrollment, that dispatches a
slot the workflow ran before it could record it; a wake after a long sleep that
bursts every missed slot; two clocks ticking at once that both send a slot; a
failed send that leaves its slot marked, so no clock retries it; and clean-up
that removes the marks a line needs. They also pin the refusal of a slot-action
workflow enrolled on a line firing more than once a day, whose every backstop
run would fail. Workflow files are real YAML read through the same reader the
clock uses, not hand-built mappings of what a parser returns. The repository's
marks, ledger and dispatches are an in-memory stand-in whose claims are atomic,
as GitHub's ref creates are. Every instant is hardcoded.
"""

import contextlib
import io
import unittest
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from github_cron_trigger import clock, workflow_files, yq
from github_cron_trigger.clock import (
    ScheduledWorkflow,
    plan,
    scheduled_workflow,
    stale_marks,
    tick,
)
from github_cron_trigger.clock_marks import Mark, mark_for
from github_cron_trigger.cron_slots import (
    UTC,
    CronSchedule,
    UnsupportedCron,
    parse_cron,
)
from github_cron_trigger.github import GitHubError
from github_cron_trigger.workflow_triggers import MisconfiguredWorkflow

REPO_ROOT = Path(__file__).resolve().parents[1]


def utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def parsed(files: dict[str, str]) -> dict[str, object]:
    """Parse each YAML text as the clock does."""
    documents, unreadable = workflow_files.parse_all(files, yq.executable())
    if unreadable:
        raise AssertionError(f"fixture did not parse: {unreadable}")
    return documents


ENROLLED = """\
name: Backup
on:
  schedule:
    - cron: "30 19 * * *"
  repository_dispatch:
    types: [github-cron-trigger/backup.yml]
  workflow_dispatch:
jobs: {}
"""

TRACKED = """\
name: Backup
on:
  schedule:
    - cron: "30 19 * * *"
  workflow_dispatch:
jobs: {}
"""


def enrolled_with_job(cron: str, uses: str) -> str:
    """An enrolled x.yml firing on `cron`, with one job whose step uses `uses`."""
    return (
        f'name: X\non:\n  schedule:\n    - cron: "{cron}"\n'
        "  repository_dispatch:\n    types: [github-cron-trigger/x.yml]\n"
        f"jobs:\n  slot:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: {uses}\n"
    )


class EnrollmentTest(unittest.TestCase):
    def test_enrolled_by_declaring_its_own_dispatch_type(self) -> None:
        documents = parsed({"backup.yml": ENROLLED})
        workflow = scheduled_workflow("backup.yml", documents["backup.yml"])
        self.assertEqual(
            workflow,
            ScheduledWorkflow("backup.yml", (parse_cron("30 19 * * *"),), True),
        )

    def test_a_scheduled_workflow_without_the_type_is_tracked_not_enrolled(
        self,
    ) -> None:
        text = 'name: X\non:\n  schedule:\n    - cron: "0 1 * * *"\njobs: {}\n'
        workflow = scheduled_workflow("x.yml", parsed({"x.yml": text})["x.yml"])
        assert workflow is not None
        self.assertFalse(workflow.enrolled)

    def test_a_type_naming_another_file_is_refused(self) -> None:
        # Copied from backup.yml into audit.yml unchanged: the clock would send
        # audit.yml's slots under a type audit.yml does not listen for.
        documents = parsed({"audit.yml": ENROLLED})
        with self.assertRaises(MisconfiguredWorkflow):
            scheduled_workflow("audit.yml", documents["audit.yml"])

    def test_enrolled_without_a_cron_line_is_refused(self) -> None:
        text = (
            "name: X\non:\n  repository_dispatch:\n"
            "    types: [github-cron-trigger/x.yml]\njobs: {}\n"
        )
        with self.assertRaises(MisconfiguredWorkflow):
            scheduled_workflow("x.yml", parsed({"x.yml": text})["x.yml"])

    def test_an_unsupported_cron_blocks_only_an_enrolled_workflow(self) -> None:
        # Day of month and day of week restricted together: outside what can be
        # resolved (cron_slots.py).
        plain = 'name: X\non:\n  schedule:\n    - cron: "0 9 1 * 1"\njobs: {}\n'
        self.assertIsNone(
            scheduled_workflow("x.yml", parsed({"x.yml": plain})["x.yml"])
        )
        enrolled = (
            'name: X\non:\n  schedule:\n    - cron: "0 9 1 * 1"\n'
            "  repository_dispatch:\n    types: [github-cron-trigger/x.yml]\njobs: {}\n"
        )
        with self.assertRaises(UnsupportedCron):
            scheduled_workflow("x.yml", parsed({"x.yml": enrolled})["x.yml"])

    def test_a_zoned_schedule_blocks_only_an_enrolled_workflow(self) -> None:
        # GitHub fires a zoned entry on local time, which these UTC slots would
        # misread; an enrolled one is reported, one that is not is left out.
        zoned = (
            'name: X\non:\n  schedule:\n    - cron: "30 5 * * 1-5"\n'
            '      timezone: "America/New_York"\n'
        )
        plain = zoned + "jobs: {}\n"
        self.assertIsNone(
            scheduled_workflow("x.yml", parsed({"x.yml": plain})["x.yml"])
        )
        enrolled = (
            zoned
            + "  repository_dispatch:\n    types: [github-cron-trigger/x.yml]\njobs: {}\n"
        )
        with self.assertRaises(UnsupportedCron):
            scheduled_workflow("x.yml", parsed({"x.yml": enrolled})["x.yml"])

    def test_a_slot_action_workflow_must_fire_each_line_at_most_daily(self) -> None:
        # The slot action refuses a schedule delivery of a line firing more than
        # once a day, so GitHub's backstop run of this workflow would fail every
        # time while the clock's dispatches ran. However the step names the
        # action - from any owner, at any version, or vendored - it is
        # recognized.
        for uses in (
            "curlewlabs-com/github-cron-trigger/slot@v1",
            "a-fork/github-cron-trigger/slot@0123456789abcdef0123456789abcdef01234567",
            "./.github/actions/github-cron-trigger/slot",
        ):
            with self.subTest(uses=uses):
                text = enrolled_with_job("7,27,47 * * * *", uses)
                with self.assertRaises(MisconfiguredWorkflow):
                    scheduled_workflow("x.yml", parsed({"x.yml": text})["x.yml"])
        # Positive control: the same workflow on a daily line is enrolled.
        text = enrolled_with_job(
            "30 19 * * *", "curlewlabs-com/github-cron-trigger/slot@v1"
        )
        workflow = scheduled_workflow("x.yml", parsed({"x.yml": text})["x.yml"])
        assert workflow is not None
        self.assertTrue(workflow.enrolled)

    def test_an_enrolled_reconciler_without_the_slot_action_may_fire_more_often(
        self,
    ) -> None:
        # No slot action and no ledger: a repeated run is harmless, and each
        # dispatch names its exact slot, so nothing needs attributing by time.
        for uses in (
            "actions/checkout@v7.0.1",
            # Another project's action that happens to be called slot.
            "someone/scheduler/slot@v1",
            "someone/github-cron-trigger-extras/slot@v1",
        ):
            with self.subTest(uses=uses):
                text = enrolled_with_job("7,27,47 * * * *", uses)
                workflow = scheduled_workflow("x.yml", parsed({"x.yml": text})["x.yml"])
                assert workflow is not None
                self.assertTrue(workflow.enrolled)
                self.assertFalse(workflow.schedules[0].fires_at_most_daily)

    def test_trigger_forms_without_a_schedule_are_not_tracked(self) -> None:
        for text in (
            "name: X\non: push\njobs: {}\n",
            "name: X\non: [push, repository_dispatch]\njobs: {}\n",
        ):
            with self.subTest(text=text):
                self.assertIsNone(
                    scheduled_workflow("x.yml", parsed({"x.yml": text})["x.yml"])
                )

    def test_every_workflow_in_this_repository_is_readable(self) -> None:
        # This project's own workflow files, read as the clock would read them:
        # a parser that refused ordinary workflow YAML fails here.
        texts = {
            path.name: path.read_text(encoding="utf-8")
            for path in (REPO_ROOT / ".github" / "workflows").iterdir()
            if path.suffix in workflow_files.WORKFLOW_SUFFIXES
        }
        documents, unreadable = workflow_files.parse_all(texts, yq.executable())
        self.assertEqual(unreadable, {})
        tracked = [
            workflow
            for name, document in documents.items()
            if (workflow := scheduled_workflow(name, document)) is not None
        ]
        # Positive control: a reader that saw nothing would pass the check above.
        self.assertTrue(tracked)


DAILY = ScheduledWorkflow("backup.yml", (parse_cron("30 19 * * *"),), True)
LINE = DAILY.schedules[0]
WEEKLY_AND_DAILY = ScheduledWorkflow(
    "two-lines.yml", (parse_cron("0 1 * * *"), parse_cron("0 1 * * 1")), True
)
REPO = "owner/name"


def marked(workflow: ScheduledWorkflow, *slots: datetime, line: int = 0) -> list[Mark]:
    return [
        mark_for(workflow.workflow_file, workflow.schedules[line], s) for s in slots
    ]


class PlanTest(unittest.TestCase):
    def test_a_line_no_clock_has_marked_is_baselined_not_delivered(self) -> None:
        # Its latest slot came due before any clock served it; the workflow
        # may already have run it, before any ledger record of it existed.
        steps = plan([DAILY], [], utc(2026, 9, 28, 20, 0))
        self.assertEqual(
            [(s.kind, s.slot) for s in steps], [("baseline", utc(2026, 9, 28, 19, 30))]
        )

    def test_a_marked_slot_is_not_owed_again(self) -> None:
        marks = marked(DAILY, utc(2026, 9, 28, 19, 30))
        self.assertEqual(plan([DAILY], marks, utc(2026, 9, 28, 23, 0)), [])

    def test_a_new_slot_is_delivered_when_it_comes_due(self) -> None:
        marks = marked(DAILY, utc(2026, 9, 27, 19, 30))
        self.assertEqual(plan([DAILY], marks, utc(2026, 9, 28, 19, 29)), [])
        steps = plan([DAILY], marks, utc(2026, 9, 28, 19, 30))
        self.assertEqual(
            [(s.kind, s.slot) for s in steps], [("deliver", utc(2026, 9, 28, 19, 30))]
        )

    def test_a_long_sleep_owes_the_newest_slot_only(self) -> None:
        # Three days with no clock up: one delivery of the newest slot, not a
        # burst of every missed one; the older ones are left to GitHub's backstop.
        marks = marked(DAILY, utc(2026, 9, 25, 19, 30))
        steps = plan([DAILY], marks, utc(2026, 9, 28, 20, 0))
        self.assertEqual(
            [(s.kind, s.slot) for s in steps], [("deliver", utc(2026, 9, 28, 19, 30))]
        )

    def test_a_workflow_that_is_not_enrolled_is_owed_nothing(self) -> None:
        tracked = ScheduledWorkflow("backup.yml", DAILY.schedules, False)
        self.assertEqual(plan([tracked], [], utc(2026, 9, 28, 20, 0)), [])

    def test_each_line_of_a_workflow_is_tracked_on_its_own(self) -> None:
        # Monday 2026-09-28 01:00 is a slot of each line; each is owed its own
        # delivery, and the daily line's mark says nothing about the weekly's.
        marks = marked(WEEKLY_AND_DAILY, utc(2026, 9, 27, 1, 0)) + marked(
            WEEKLY_AND_DAILY, utc(2026, 9, 21, 1, 0), line=1
        )
        steps = plan([WEEKLY_AND_DAILY], marks, utc(2026, 9, 28, 1, 2))
        self.assertEqual(
            [(s.schedule.canonical, s.slot) for s in steps],
            [
                ("0 1 * * *", utc(2026, 9, 28, 1, 0)),
                ("0 1 * * 1", utc(2026, 9, 28, 1, 0)),
            ],
        )

    def test_utc_date_ahead_of_local_date(self) -> None:
        # 18:05 Pacific on the 27th is 01:05 UTC on the 28th: the 01:00 UTC
        # line's 28th slot is due, whatever the host's own calendar says.
        line = ScheduledWorkflow("logs.yml", (parse_cron("0 1 * * *"),), True)
        now = datetime(2026, 9, 27, 18, 5, tzinfo=ZoneInfo("America/Los_Angeles"))
        steps = plan([line], marked(line, utc(2026, 9, 27, 1, 0)), now)
        self.assertEqual([s.slot for s in steps], [utc(2026, 9, 28, 1, 0)])


class StaleMarksTest(unittest.TestCase):
    def test_a_line_keeps_its_two_newest_marks(self) -> None:
        # Two, so a clock whose send fails can release its new mark and still
        # leave one behind to retry from.
        marks = marked(DAILY, *(utc(2026, 9, day, 19, 30) for day in (25, 26, 27, 28)))
        self.assertEqual(
            [m.slot for m in stale_marks([DAILY], marks)],
            [utc(2026, 9, 26, 19, 30), utc(2026, 9, 25, 19, 30)],
        )

    def test_a_line_no_enrolled_workflow_carries_loses_every_mark(self) -> None:
        # Disenrolled, its cron changed, or its file deleted: enrolling it
        # again later baselines it rather than sending.
        marks = marked(DAILY, utc(2026, 9, 28, 19, 30))
        disenrolled = ScheduledWorkflow("backup.yml", DAILY.schedules, False)
        self.assertEqual(stale_marks([disenrolled], marks), marks)
        self.assertEqual(stale_marks([], marks), marks)

    def test_a_file_that_could_not_be_judged_keeps_its_marks(self) -> None:
        # A file that failed to parse this tick is not a disenrolled one; its
        # lines must deliver, not baseline, once it parses again.
        marks = marked(DAILY, utc(2026, 9, 27, 19, 30), utc(2026, 9, 28, 19, 30))
        self.assertEqual(stale_marks([], marks, unjudged={"backup.yml"}), [])


class FakeRemote:
    """A repository's marks, ledger and dispatches, in memory. A claim of a mark
    that exists fails, as GitHub's ref create does."""

    def __init__(
        self,
        marks: Iterable[Mark] = (),
        done: Iterable[datetime] = (),
        failing_dispatches: int = 0,
    ) -> None:
        self.marks = set(marks)
        self.done = set(done)
        self.failing_dispatches = failing_dispatches
        self.dispatched: list[tuple[str, datetime]] = []
        self.snapshot: list[Mark] | None = None

    def read_marks(self, repo: str) -> list[Mark]:
        if self.snapshot is not None:
            return list(self.snapshot)
        return sorted(self.marks)

    def claim(self, repo: str, mark: Mark) -> bool:
        if mark in self.marks:
            return False
        self.marks.add(mark)
        return True

    def release(self, repo: str, mark: Mark) -> None:
        self.marks.discard(mark)

    def remove(self, repo: str, marks: Iterable[Mark]) -> None:
        self.marks.difference_update(marks)

    def is_done(
        self, repo: str, workflow_file: str, schedule: CronSchedule, slot: datetime
    ) -> bool:
        return slot in self.done

    def dispatch(
        self, repo: str, workflow_file: str, schedule: CronSchedule, slot: datetime
    ) -> None:
        if self.failing_dispatches:
            self.failing_dispatches -= 1
            raise GitHubError("gh: Server Error (HTTP 502)")
        self.dispatched.append((workflow_file, slot))


def run_tick(
    remote: FakeRemote,
    documents: dict[str, object],
    now: datetime,
    send: bool = True,
    unreadable: Iterable[str] = (),
) -> tuple[list[str], str]:
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        problems = tick(REPO, documents, now, send, remote, list(unreadable))
    return problems, printed.getvalue()


class TickTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enrolled = parsed({"backup.yml": ENROLLED})

    def test_a_due_slot_is_claimed_then_sent(self) -> None:
        remote = FakeRemote(marked(DAILY, utc(2026, 9, 27, 19, 30)))
        problems, printed = run_tick(remote, self.enrolled, utc(2026, 9, 28, 19, 31))
        self.assertEqual(problems, [])
        self.assertEqual(remote.dispatched, [("backup.yml", utc(2026, 9, 28, 19, 30))])
        self.assertIn(
            mark_for("backup.yml", LINE, utc(2026, 9, 28, 19, 30)), remote.marks
        )
        self.assertIn(": sent", printed)

    def test_two_clocks_ticking_at_once_send_a_slot_once(self) -> None:
        # Both read the marks before either claims: the race the claim exists
        # for. Exactly one claim succeeds, and only that clock sends.
        remote = FakeRemote(marked(DAILY, utc(2026, 9, 27, 19, 30)))
        remote.snapshot = remote.read_marks(REPO)
        now = utc(2026, 9, 28, 19, 31)
        first, _ = run_tick(remote, self.enrolled, now)
        second, printed = run_tick(remote, self.enrolled, now)
        self.assertEqual(first + second, [])
        self.assertEqual(remote.dispatched, [("backup.yml", utc(2026, 9, 28, 19, 30))])
        self.assertIn("claimed by another clock; not sent", printed)

    def test_two_clocks_baselining_at_once_send_nothing(self) -> None:
        remote = FakeRemote()
        remote.snapshot = []
        now = utc(2026, 9, 28, 19, 31)
        run_tick(remote, self.enrolled, now)
        run_tick(remote, self.enrolled, now)
        self.assertEqual(remote.dispatched, [])
        self.assertEqual(
            remote.marks, {mark_for("backup.yml", LINE, utc(2026, 9, 28, 19, 30))}
        )

    def test_a_failed_send_releases_its_mark_so_the_next_tick_retries(self) -> None:
        remote = FakeRemote(
            marked(DAILY, utc(2026, 9, 27, 19, 30)), failing_dispatches=1
        )
        now = utc(2026, 9, 28, 19, 31)
        problems, _ = run_tick(remote, self.enrolled, now)
        self.assertEqual(len(problems), 1)
        self.assertNotIn(
            mark_for("backup.yml", LINE, utc(2026, 9, 28, 19, 30)), remote.marks
        )
        # The previous mark is still there, so the line delivers rather than
        # baselining.
        problems, _ = run_tick(remote, self.enrolled, utc(2026, 9, 28, 19, 36))
        self.assertEqual(problems, [])
        self.assertEqual(remote.dispatched, [("backup.yml", utc(2026, 9, 28, 19, 30))])

    def test_a_slot_the_ledger_records_is_marked_and_not_sent(self) -> None:
        remote = FakeRemote(
            marked(DAILY, utc(2026, 9, 27, 19, 30)), done=[utc(2026, 9, 28, 19, 30)]
        )
        _, printed = run_tick(remote, self.enrolled, utc(2026, 9, 28, 19, 31))
        self.assertEqual(remote.dispatched, [])
        self.assertIn(
            mark_for("backup.yml", LINE, utc(2026, 9, 28, 19, 30)), remote.marks
        )
        self.assertIn("already recorded done; not sent", printed)

    def test_an_enrollment_is_delivered_from_the_slot_after_its_baseline(
        self,
    ) -> None:
        # Not enrolled, the workflow has no marks; enrolled, its first tick
        # baselines and the next slot is sent.
        remote = FakeRemote()
        run_tick(remote, parsed({"backup.yml": TRACKED}), utc(2026, 9, 27, 20, 0))
        self.assertEqual(remote.marks, set())
        run_tick(remote, self.enrolled, utc(2026, 9, 28, 21, 0))
        run_tick(remote, self.enrolled, utc(2026, 9, 29, 19, 31))
        self.assertEqual(remote.dispatched, [("backup.yml", utc(2026, 9, 29, 19, 30))])

    def test_disenrolling_removes_the_marks(self) -> None:
        remote = FakeRemote(marked(DAILY, utc(2026, 9, 28, 19, 30)))
        run_tick(remote, parsed({"backup.yml": TRACKED}), utc(2026, 9, 28, 20, 0))
        self.assertEqual(remote.marks, set())

    def test_an_unreadable_file_keeps_its_marks(self) -> None:
        marks = marked(DAILY, utc(2026, 9, 28, 19, 30))
        remote = FakeRemote(marks)
        run_tick(remote, {}, utc(2026, 9, 28, 20, 0), unreadable=["backup.yml"])
        self.assertEqual(remote.marks, set(marks))

    def test_a_dry_run_writes_nothing(self) -> None:
        marks = marked(DAILY, *(utc(2026, 9, day, 19, 30) for day in (25, 26, 27)))
        remote = FakeRemote(marks)
        _, printed = run_tick(
            remote, self.enrolled, utc(2026, 9, 28, 19, 31), send=False
        )
        self.assertEqual((remote.marks, remote.dispatched), (set(marks), []))
        self.assertIn("would send (dry run)", printed)
        self.assertIn("would remove 1 old mark(s) (dry run)", printed)

    def test_marks_that_cannot_be_read_send_nothing(self) -> None:
        class Unreadable(FakeRemote):
            def read_marks(self, repo: str) -> list[Mark]:
                raise GitHubError("gh: Server Error (HTTP 502)")

        remote = Unreadable()
        problems, _ = run_tick(remote, self.enrolled, utc(2026, 9, 28, 19, 31))
        self.assertEqual(len(problems), 1)
        self.assertEqual((remote.marks, remote.dispatched), (set(), []))


class MainTest(unittest.TestCase):
    """A tick that could not read what it had to deliver must fail, so whatever
    runs the clock reports it; a file it could not read must not stop the rest."""

    def run_main(
        self, load: workflow_files.Loader, remote: FakeRemote
    ) -> tuple[int, str]:
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            status = clock.main(
                ["--repo", REPO, "--send"],
                now=utc(2026, 9, 29, 19, 31),
                load=load,
                remote=remote,
            )
        return status, printed.getvalue()

    def test_a_tick_that_reads_nothing_fails_and_sends_nothing(self) -> None:
        def unreachable(_repo: str) -> tuple[dict[str, object], dict[str, str]]:
            raise GitHubError("gh: Server Error (HTTP 502)")

        remote = FakeRemote(marked(DAILY, utc(2026, 9, 28, 19, 30)))
        status, printed = self.run_main(unreachable, remote)
        self.assertEqual(status, 1)
        self.assertIn("HTTP 502", printed)
        self.assertEqual(remote.dispatched, [])

    def test_an_unreadable_file_fails_the_tick_but_not_the_rest(self) -> None:
        def load(_repo: str) -> tuple[dict[str, object], dict[str, str]]:
            return parsed({"backup.yml": ENROLLED}), {"broken.yml": "bad YAML"}

        remote = FakeRemote(marked(DAILY, utc(2026, 9, 28, 19, 30)))
        status, printed = self.run_main(load, remote)
        self.assertEqual(status, 1)
        self.assertIn("broken.yml: unreadable: bad YAML", printed)
        self.assertEqual(remote.dispatched, [("backup.yml", utc(2026, 9, 29, 19, 30))])


if __name__ == "__main__":
    unittest.main()
