"""Unit tests for the clock's decisions: what is enrolled, and what a tick owes.

The failures these guard are the ones that make a delivery wrong without making
it loud: a workflow read as enrolled when it is not (the clock would send slots
nothing listens for) or missed when it is (it would never be delivered on
time); a first tick, or the first tick after an enrollment, that dispatches a
slot the workflow ran before it could record it; a wake after a long sleep that
bursts every missed slot; and a lost state file that sends instead of
re-baselining. They also pin the refusal of a slot-action workflow enrolled on a
line firing more than once a day, whose every backstop run would fail. Workflow
files are real YAML read through the same reader the clock uses, not hand-built
mappings of what a parser returns. Every instant is hardcoded.
"""

import contextlib
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from github_cron_trigger import clock, workflow_files, yq
from github_cron_trigger.clock import (
    Handled,
    ScheduledWorkflow,
    load_state,
    plan,
    save_state,
    scheduled_workflow,
    state_key,
    tick,
)
from github_cron_trigger.cron_slots import UTC, UnsupportedCron, parse_cron
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
DAILY_KEY = state_key("backup.yml", DAILY.schedules[0])
WEEKLY_AND_DAILY = ScheduledWorkflow(
    "two-lines.yml", (parse_cron("0 1 * * *"), parse_cron("0 1 * * 1")), True
)


class PlanTest(unittest.TestCase):
    def test_a_line_seen_for_the_first_time_is_baselined_not_delivered(self) -> None:
        # Its latest slot came due before the clock was watching; the workflow
        # may already have run it, before any ledger record of it existed.
        steps = plan([DAILY], {}, utc(2026, 9, 28, 20, 0))
        self.assertEqual(
            [(s.kind, s.slot) for s in steps], [("baseline", utc(2026, 9, 28, 19, 30))]
        )

    def test_a_handled_slot_is_not_owed_again(self) -> None:
        state = {DAILY_KEY: Handled(utc(2026, 9, 28, 19, 30), True)}
        self.assertEqual(plan([DAILY], state, utc(2026, 9, 28, 23, 0)), [])

    def test_a_new_slot_is_delivered_when_it_comes_due(self) -> None:
        state = {DAILY_KEY: Handled(utc(2026, 9, 27, 19, 30), True)}
        self.assertEqual(plan([DAILY], state, utc(2026, 9, 28, 19, 29)), [])
        steps = plan([DAILY], state, utc(2026, 9, 28, 19, 30))
        self.assertEqual(
            [(s.kind, s.slot) for s in steps], [("deliver", utc(2026, 9, 28, 19, 30))]
        )

    def test_a_long_sleep_owes_the_newest_slot_only(self) -> None:
        # Three days asleep: one delivery of the newest slot, not a burst of
        # every missed one; the older ones are left to GitHub's backstop.
        state = {DAILY_KEY: Handled(utc(2026, 9, 25, 19, 30), True)}
        steps = plan([DAILY], state, utc(2026, 9, 28, 20, 0))
        self.assertEqual(
            [(s.kind, s.slot) for s in steps], [("deliver", utc(2026, 9, 28, 19, 30))]
        )

    def test_a_line_whose_workflow_enrolled_since_it_was_handled_is_baselined(
        self,
    ) -> None:
        # The clock handled the 27th's slot while the workflow was not enrolled,
        # then slept across the merge that enrolled it. The 28th's slot may have
        # run before that merge, without the slot action and so with no ledger
        # record, and a dispatch would repeat it.
        state = {DAILY_KEY: Handled(utc(2026, 9, 27, 19, 30), False)}
        steps = plan([DAILY], state, utc(2026, 9, 28, 21, 0))
        self.assertEqual(
            [(s.kind, s.slot) for s in steps], [("baseline", utc(2026, 9, 28, 19, 30))]
        )

    def test_each_line_of_a_workflow_is_tracked_on_its_own(self) -> None:
        # Monday 2026-09-28 01:00 is a slot of each line; each is owed its own
        # delivery, and the daily line's state says nothing about the weekly's.
        daily, monday = WEEKLY_AND_DAILY.schedules
        state = {
            state_key("two-lines.yml", daily): Handled(utc(2026, 9, 27, 1, 0), True),
            state_key("two-lines.yml", monday): Handled(utc(2026, 9, 21, 1, 0), True),
        }
        steps = plan([WEEKLY_AND_DAILY], state, utc(2026, 9, 28, 1, 2))
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
        state = {
            state_key("logs.yml", line.schedules[0]): Handled(
                utc(2026, 9, 27, 1, 0), True
            )
        }
        now = datetime(2026, 9, 27, 18, 5, tzinfo=ZoneInfo("America/Los_Angeles"))
        steps = plan([line], state, now)
        self.assertEqual([s.slot for s in steps], [utc(2026, 9, 28, 1, 0)])


class TickTest(unittest.TestCase):
    def test_an_enrollment_is_delivered_from_the_slot_after_its_baseline(
        self,
    ) -> None:
        # The baseline for a changed enrollment must record the line as handled
        # while enrolled; otherwise every later slot would baseline again and
        # the enrolled workflow would never be delivered.
        enrolled = parsed({"backup.yml": ENROLLED})
        state: dict[str, Handled] = {}
        with contextlib.redirect_stdout(io.StringIO()):
            problems = tick(
                "owner/name",
                parsed({"backup.yml": TRACKED}),
                state,
                utc(2026, 9, 27, 20, 0),
                send=False,
            )
            problems += tick(
                "owner/name", enrolled, state, utc(2026, 9, 28, 21, 0), send=False
            )
        self.assertEqual(problems, [])
        self.assertEqual(state, {DAILY_KEY: Handled(utc(2026, 9, 28, 19, 30), True)})
        workflow = scheduled_workflow("backup.yml", enrolled["backup.yml"])
        assert workflow is not None
        steps = plan([workflow], state, utc(2026, 9, 29, 19, 30))
        self.assertEqual(
            [(s.kind, s.slot) for s in steps], [("deliver", utc(2026, 9, 29, 19, 30))]
        )


class MainTest(unittest.TestCase):
    """A tick that could not read what it had to deliver must fail, so whatever
    runs the clock reports it, and must not lose the state it already holds."""

    def run_main(
        self, load: workflow_files.Loader, state: dict[str, Handled]
    ) -> tuple[int, str, dict[str, Handled]]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(path, state)
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                status = clock.main(
                    ["--repo", "owner/name", "--state", str(path)],
                    now=utc(2026, 9, 29, 19, 31),
                    load=load,
                )
            return status, printed.getvalue(), load_state(path)

    def test_a_tick_that_reads_nothing_fails_and_keeps_the_state(self) -> None:
        def unreachable(_repo: str) -> tuple[dict[str, object], dict[str, str]]:
            raise GitHubError("gh: Server Error (HTTP 502)")

        state = {DAILY_KEY: Handled(utc(2026, 9, 28, 19, 30), True)}
        status, printed, after = self.run_main(unreachable, state)
        self.assertEqual(status, 1)
        self.assertIn("HTTP 502", printed)
        self.assertEqual(after, state)

    def test_an_unreadable_file_fails_the_tick_but_not_the_rest(self) -> None:
        # The readable workflow is still delivered (in shadow mode, logged);
        # the unreadable one is named, and the tick goes red over it.
        def load(_repo: str) -> tuple[dict[str, object], dict[str, str]]:
            return parsed({"backup.yml": ENROLLED}), {"broken.yml": "bad YAML"}

        state = {DAILY_KEY: Handled(utc(2026, 9, 28, 19, 30), True)}
        status, printed, after = self.run_main(load, state)
        self.assertEqual(status, 1)
        self.assertIn("broken.yml: unreadable: bad YAML", printed)
        self.assertIn("would send", printed)
        self.assertEqual(after[DAILY_KEY].slot, utc(2026, 9, 29, 19, 30))


class StateFileTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            state = {
                "backup.yml 30 19 * * *": Handled(utc(2026, 9, 28, 19, 30), True),
                "x.yml 0 1 * * *": Handled(utc(2026, 9, 28, 1, 0), False),
            }
            save_state(path, state)
            self.assertEqual(load_state(path), state)

    def test_missing_or_damaged_state_reads_as_empty(self) -> None:
        # Empty state re-baselines every line, which sends nothing - the safe
        # reading of a state that cannot be trusted.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertEqual(load_state(path), {})
            for damaged in (
                "{not json",
                '["k"]',
                '{"k": 1}',
                '{"k": "2026-09-28T19:30Z"}',
                '{"k": {"slot": "2026-09-28T19:30", "enrolled": true}}',
                '{"k": {"slot": "2026-09-28T19:30Z", "enrolled": "yes"}}',
            ):
                with self.subTest(damaged=damaged):
                    path.write_text(damaged, encoding="utf-8")
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(load_state(path), {})


if __name__ == "__main__":
    unittest.main()
