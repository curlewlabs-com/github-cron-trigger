"""Unit tests for how a run decides whether it is a scheduled delivery.

The decision routes a run: a scheduled delivery consults the ledger and may
skip its work, anything else runs exactly as it did before enrollment. The risks
are a manual or PR run mistaken for a scheduled one (it could skip real work);
a delivery attributed to the wrong slot (it skips work it owed, or records a
slot that another delivery then skips); a workflow path or commit, which arrive
in the event payload, reaching a file other than a workflow; and a ledger path
that escapes its namespace or conflates separate cron lines. Workflow files are
real YAML parsed by yq; only the API call that returns their text is
replaced. The ledger calls themselves go to GitHub and are exercised live by
tests/live_check.py, not here.
"""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from github_cron_trigger import steps, workflow_files
from github_cron_trigger.cron_slots import UTC, UnsupportedCron, parse_cron
from github_cron_trigger.github import HttpError, classify, transient
from github_cron_trigger.slot_ledger import (
    cron_segment,
    record_slot_key,
    ref_path,
)
from github_cron_trigger.steps import (
    Parent,
    ReadText,
    Resolution,
    SlotError,
    read_own_entries,
    read_parent,
    resolve,
    workflow_file_from_ref,
)
from github_cron_trigger.workflow_triggers import ScheduleEntry

NOW = datetime(2026, 9, 28, 6, 36, tzinfo=UTC)
DAILY = "0 1 * * *"
COMMIT = "1" * 40
UTC_WORKFLOW = 'name: Backup\non:\n  schedule:\n    - cron: "30 19 * * *"\njobs: {}\n'
ZONED_WORKFLOW = (
    "name: Zoned\non:\n  schedule:\n"
    '    - cron: "30 19 * * *"\n      timezone: "America/New_York"\njobs: {}\n'
)


def utc_lines(*crons: str) -> tuple[ScheduleEntry, ...]:
    """Schedule entries with no timezone, as most workflows write them."""
    return tuple(ScheduleEntry(cron, None) for cron in crons)


def repository(files: dict[str, str], commit: str = COMMIT) -> ReadText:
    """A stand-in for the contents API: these workflow files, at one commit."""

    def read_text(repo: str, path: str, ref: str) -> str:
        name = path.rsplit("/", 1)[-1]
        if repo != "owner/name" or ref != commit or name not in files:
            raise HttpError(404, "Not Found")
        return files[name]

    return read_text


NO_FILES = repository({})
WORKFLOWS = repository({"backup.yml": UTC_WORKFLOW, "zoned.yml": ZONED_WORKFLOW})


class ResolveTest(unittest.TestCase):
    def test_dispatch_uses_the_line_and_slot_it_names(self) -> None:
        self.assertEqual(
            resolve(
                "repository_dispatch",
                "2026-09-28T01:00Z",
                DAILY,
                "",
                utc_lines(DAILY),
                None,
                NOW,
            ),
            Resolution(
                schedule=parse_cron(DAILY),
                slot=datetime(2026, 9, 28, 1, 0, tzinfo=UTC),
            ),
        )

    def test_dispatch_missing_its_slot_or_line_is_refused(self) -> None:
        for slot, cron in (("", DAILY), ("2026-09-28T01:00Z", ""), ("", "")):
            with self.subTest(slot=slot, cron=cron), self.assertRaises(SlotError):
                resolve(
                    "repository_dispatch", slot, cron, "", utc_lines(DAILY), None, NOW
                )

    def test_dispatch_slot_that_is_not_an_instant_of_its_line_is_refused(
        self,
    ) -> None:
        # Recording 01:03 for a line that fires at 01:00 would leave the real
        # slot unrecorded, so the other delivery would do the work again.
        with self.assertRaises(SlotError):
            resolve(
                "repository_dispatch",
                "2026-09-28T01:03Z",
                DAILY,
                "",
                utc_lines(DAILY),
                None,
                NOW,
            )

    def test_dispatch_slot_far_in_the_future_is_refused(self) -> None:
        with self.assertRaises(SlotError):
            resolve(
                "repository_dispatch",
                "2026-09-28T07:00Z",
                "0 7 * * *",
                "",
                utc_lines("0 7 * * *"),
                None,
                NOW,
            )

    def test_dispatch_slot_just_ahead_of_a_trailing_runner_clock_is_accepted(
        self,
    ) -> None:
        resolution = resolve(
            "repository_dispatch",
            "2026-09-28T06:40Z",
            "40 6 * * *",
            "",
            utc_lines("40 6 * * *"),
            None,
            NOW,
        )
        self.assertIsNotNone(resolution.slot)

    def test_dispatch_slot_older_than_the_ledger_keeps_is_refused(self) -> None:
        # A replayed or stale dispatch for a slot whose record may have been
        # pruned: the ledger cannot say whether it was done, so it must not run.
        with self.assertRaises(SlotError):
            resolve(
                "repository_dispatch",
                "2026-07-01T01:00Z",
                DAILY,
                "",
                utc_lines(DAILY),
                None,
                NOW,
            )

    def test_schedule_resolves_the_line_that_fired(self) -> None:
        self.assertEqual(
            resolve("schedule", "", "", DAILY, utc_lines(DAILY), None, NOW),
            Resolution(
                schedule=parse_cron(DAILY),
                slot=datetime(2026, 9, 28, 1, 0, tzinfo=UTC),
            ),
        )

    def test_late_schedule_delivery_of_a_sub_daily_line_is_refused(self) -> None:
        # GitHub fired the 01:07 instant and started the run hours late; "the
        # latest instant before now" would name 06:27 instead, so a run of a line
        # that fires more than once a day cannot be attributed and must refuse.
        for cron in ("7,27,47 * * * *", "0 1,13 * * *"):
            with self.subTest(cron=cron), self.assertRaises(SlotError):
                resolve("schedule", "", "", cron, utc_lines(cron), None, NOW)

    def test_schedule_without_its_line_is_refused(self) -> None:
        with self.assertRaises(SlotError):
            resolve("schedule", "", "", "", utc_lines(DAILY), None, NOW)

    def test_a_delivery_must_name_one_of_the_workflows_own_lines(self) -> None:
        # A dispatch naming a line the workflow does not carry - stale after a
        # cron change, or sent to the wrong file - would record a slot no
        # delivery of the workflow's real line ever looks for.
        with self.assertRaises(SlotError):
            resolve(
                "repository_dispatch",
                "2026-09-28T01:00Z",
                DAILY,
                "",
                utc_lines("30 19 * * *"),
                None,
                NOW,
            )
        with self.assertRaises(SlotError):
            resolve("schedule", "", "", DAILY, utc_lines("30 19 * * *"), None, NOW)

    def test_a_dispatch_names_its_line_by_canonical_form(self) -> None:
        # The clock sends the canonical line; the workflow may write it
        # differently, and it is still the same line.
        resolution = resolve(
            "repository_dispatch",
            "2026-09-28T01:00Z",
            "0 1 * * *",
            "",
            utc_lines("00 01 * * *"),
            None,
            NOW,
        )
        self.assertEqual(resolution.slot, datetime(2026, 9, 28, 1, 0, tzinfo=UTC))

    def test_a_zoned_entry_is_refused_whichever_delivery_names_it(self) -> None:
        # GitHub fires a zoned entry on local time; its delivery carries only
        # the cron text, so the entry is what shows it is zoned. A line written
        # twice, once zoned, is refused too: the delivery cannot say which fired.
        zoned = (ScheduleEntry(DAILY, "America/New_York"),)
        twice = (ScheduleEntry(DAILY, None), ScheduleEntry(DAILY, "Europe/Paris"))
        for entries in (zoned, twice):
            with self.subTest(entries=entries):
                with self.assertRaises(UnsupportedCron):
                    resolve("schedule", "", "", DAILY, entries, None, NOW)
                with self.assertRaises(UnsupportedCron):
                    resolve(
                        "repository_dispatch",
                        "2026-09-28T01:00Z",
                        DAILY,
                        "",
                        entries,
                        None,
                        NOW,
                    )

    def test_every_other_event_is_not_scheduled_even_with_stray_fields(self) -> None:
        for event in ("workflow_dispatch", "pull_request", "push"):
            with self.subTest(event=event):
                self.assertEqual(
                    resolve(event, "2026-09-28T01:00Z", DAILY, DAILY, (), None, NOW),
                    Resolution(schedule=None, slot=None),
                )


class ChainedResolveTest(unittest.TestCase):
    """A workflow_run run takes its slot from its parent, so every delivery of
    the parent's slot - the clock's on time and GitHub's hours later - makes the
    chained run resolve to the same slot, and its records dedupe them."""

    ON_TIME = datetime(2026, 9, 28, 1, 3, tzinfo=UTC)
    HOURS_LATE = datetime(2026, 9, 28, 6, 36, tzinfo=UTC)

    def chained(self, parent: Parent) -> Resolution:
        return resolve("workflow_run", "", "", "", (), parent, NOW)

    def test_every_delivery_of_the_parent_resolves_to_one_slot(self) -> None:
        clock = self.chained(
            Parent("repository_dispatch", utc_lines(DAILY), self.ON_TIME)
        )
        backstop = self.chained(Parent("schedule", utc_lines(DAILY), self.HOURS_LATE))
        expected = Resolution(
            schedule=parse_cron(DAILY), slot=datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
        )
        self.assertEqual(clock, expected)
        self.assertEqual(backstop, expected)

    def test_a_parent_that_was_not_scheduled_leaves_the_run_unscheduled(self) -> None:
        for event in ("workflow_dispatch", "push", "pull_request"):
            with self.subTest(event=event):
                self.assertEqual(
                    self.chained(Parent(event, utc_lines(DAILY), self.ON_TIME)),
                    Resolution(schedule=None, slot=None),
                )

    def test_a_parent_with_other_than_one_cron_line_is_refused(self) -> None:
        # The parent run does not say which of its lines fired it.
        for lines in (utc_lines(), utc_lines(DAILY, "0 13 * * *")):
            with self.subTest(lines=lines), self.assertRaises(SlotError):
                self.chained(Parent("schedule", lines, self.ON_TIME))

    def test_a_parent_line_firing_more_than_daily_is_refused(self) -> None:
        with self.assertRaises(SlotError):
            self.chained(
                Parent("schedule", utc_lines("7,27,47 * * * *"), self.HOURS_LATE)
            )

    def test_a_workflow_run_event_without_its_parent_is_refused(self) -> None:
        with self.assertRaises(SlotError):
            resolve("workflow_run", "", "", "", (), None, NOW)

    def test_a_zoned_parent_line_is_refused(self) -> None:
        # The parent fires on local time, which the UTC slot math would misread
        # into a slot neither of the parent's deliveries belongs to.
        zoned = (ScheduleEntry(DAILY, "America/New_York"),)
        with self.assertRaises(UnsupportedCron):
            self.chained(Parent("schedule", zoned, self.ON_TIME))

    def test_a_parent_slot_older_than_the_ledger_keeps_is_refused(self) -> None:
        # A re-run of a long-finished chained run carries its original parent:
        # the record of that slot may be pruned, so its work may already be done.
        old = Parent(
            "schedule", utc_lines(DAILY), datetime(2026, 7, 1, 6, 0, tzinfo=UTC)
        )
        with self.assertRaises(SlotError):
            self.chained(old)


class ReadParentTest(unittest.TestCase):
    """The parent's path and commit arrive in the event payload and name the
    file to read, so only a workflow file at a full commit may be read; and its
    cron lines come from the real YAML parser, not a hand-built mapping."""

    def test_the_parent_workflow_file_gives_its_cron_lines(self) -> None:
        parent = read_parent(
            "owner/name",
            ".github/workflows/backup.yml",
            COMMIT,
            "schedule",
            "2026-09-28T22:04:11Z",
            WORKFLOWS,
        )
        self.assertEqual(
            parent,
            Parent(
                "schedule",
                utc_lines("30 19 * * *"),
                datetime(2026, 9, 28, 22, 4, 11, tzinfo=UTC),
            ),
        )

    def test_a_path_that_is_not_a_workflow_file_is_refused(self) -> None:
        for path in (
            "",
            "backup.yml",
            ".github/workflows/../../backup.yml",
            ".github/workflows/sub/backup.yml",
            "/etc/backup.yml",
        ):
            with self.subTest(path=path), self.assertRaises(SlotError):
                read_parent(
                    "owner/name",
                    path,
                    COMMIT,
                    "schedule",
                    "2026-09-28T22:04:11Z",
                    WORKFLOWS,
                )

    def test_a_commit_that_is_not_a_full_object_name_is_refused(self) -> None:
        # The commit becomes a query parameter of the contents API; a branch
        # name there would read whatever the branch holds now, not what ran.
        for commit in ("", "main", "abc123", "1" * 39, "G" * 40):
            with self.subTest(commit=commit), self.assertRaises(SlotError):
                read_parent(
                    "owner/name",
                    ".github/workflows/backup.yml",
                    commit,
                    "schedule",
                    "2026-09-28T22:04:11Z",
                    workflow_files.fetch_file,
                )

    def test_a_file_the_api_does_not_return_is_refused(self) -> None:
        with self.assertRaises(SlotError):
            read_parent(
                "owner/name",
                ".github/workflows/missing.yml",
                COMMIT,
                "schedule",
                "2026-09-28T22:04:11Z",
                WORKFLOWS,
            )

    def test_a_creation_time_without_a_zone_or_unreadable_is_refused(self) -> None:
        for created_at in ("2026-09-28T22:04:11", "yesterday", ""):
            with self.subTest(created_at=created_at), self.assertRaises(SlotError):
                read_parent(
                    "owner/name",
                    ".github/workflows/backup.yml",
                    COMMIT,
                    "schedule",
                    created_at,
                    WORKFLOWS,
                )


class OwnEntriesTest(unittest.TestCase):
    """A `schedule` or `repository_dispatch` delivery's line is checked against
    the run's own workflow file, named by its workflow ref and read at the
    commit the run executed: the delivery's cron text cannot show that its
    entry sets a timezone, so without this read GitHub's local-time run of a
    zoned entry would resolve to a UTC slot it does not belong to."""

    def test_the_workflow_ref_names_the_file_whose_entries_are_read(self) -> None:
        self.assertEqual(
            read_own_entries(
                "owner/name",
                "owner/name/.github/workflows/zoned.yml@refs/heads/main",
                COMMIT,
                WORKFLOWS,
            ),
            (ScheduleEntry("30 19 * * *", "America/New_York"),),
        )
        self.assertEqual(
            read_own_entries(
                "owner/name",
                "owner/name/.github/workflows/backup.yml@refs/heads/main",
                COMMIT,
                WORKFLOWS,
            ),
            utc_lines("30 19 * * *"),
        )

    def test_a_workflow_ref_that_names_no_workflow_file_is_refused(self) -> None:
        for workflow_ref in (
            "owner/name/backup.yml@refs/heads/main",
            "owner/name/.github/workflows/../../backup.yml@refs/heads/main",
            "backup.yml",
        ):
            with self.subTest(workflow_ref=workflow_ref), self.assertRaises(SlotError):
                read_own_entries("owner/name", workflow_ref, COMMIT, WORKFLOWS)

    def test_a_delivery_of_a_zoned_entry_fails_before_the_ledger(self) -> None:
        # The slot command, end to end: the zoned entry is what it names in its
        # refusal, and no outputs are written, so the work cannot run on them.
        deliveries = {
            "schedule": ["--schedule-cron", "30 19 * * *"],
            "repository_dispatch": [
                "--payload-cron",
                "30 19 * * *",
                "--payload-slot",
                "2026-09-27T19:30Z",
            ],
        }
        for event, fields in deliveries.items():
            with self.subTest(event=event), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "out"
                printed = io.StringIO()
                with redirect_stdout(printed):
                    status = steps.slot_main(
                        [
                            "--repo",
                            "owner/name",
                            "--workflow-ref",
                            "owner/name/.github/workflows/zoned.yml@refs/heads/main",
                            "--workflow-sha",
                            COMMIT,
                            "--event-name",
                            event,
                            *fields,
                            "--github-output",
                            str(output),
                        ],
                        now=NOW,
                        read_text=WORKFLOWS,
                    )
                self.assertEqual(status, 1)
                self.assertIn("sets timezone", printed.getvalue())
                self.assertFalse(output.exists())


class SlotCommandTest(unittest.TestCase):
    def test_unscheduled_run_is_told_to_run_without_a_read(self) -> None:
        # The reader refuses every file and no ledger is reachable from a unit
        # test, so this passing also shows the unscheduled path reads neither.
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out"
            summary = Path(tmp) / "summary"
            with redirect_stdout(io.StringIO()):
                status = steps.slot_main(
                    [
                        "--repo",
                        "owner/name",
                        "--workflow-ref",
                        "owner/name/.github/workflows/example.yml@refs/heads/main",
                        "--workflow-sha",
                        COMMIT,
                        "--event-name",
                        "workflow_dispatch",
                        "--github-output",
                        str(output),
                        "--step-summary",
                        str(summary),
                    ],
                    now=NOW,
                    read_text=NO_FILES,
                )
            self.assertEqual(status, 0)
            self.assertEqual(
                output.read_text().splitlines(),
                ["scheduled=false", "slot=", "cron=", "run=true"],
            )
            self.assertIn("not a scheduled delivery", summary.read_text())

    def test_done_refuses_a_slot_that_is_not_an_instant_of_its_line(self) -> None:
        # The record step re-checks what it was handed: a mismatched slot and
        # line would write a record no delivery of the real slot would find.
        with redirect_stdout(io.StringIO()):
            status = steps.done_main(
                [
                    "--repo",
                    "owner/name",
                    "--workflow-ref",
                    "owner/name/.github/workflows/example.yml@refs/heads/main",
                    "--slot",
                    "2026-09-28T01:03Z",
                    "--cron",
                    DAILY,
                    "--sha",
                    "0" * 40,
                ]
            )
        self.assertEqual(status, 1)


class GhFailureClassificationTest(unittest.TestCase):
    """Retry a busy or unreachable API; fail at once on a refusal.

    Retrying a refusal only delays a failure that is coming anyway, but failing
    on a transient error turns a gateway blip into a red run and, for a record
    step, a slot the other delivery then does again. The 404 and 422 lines are
    `gh api` output captured live against a repository's refs API; the
    secondary-rate-limit and connection lines are the forms GitHub and `gh`
    print for those failures.
    """

    def test_refusals_are_not_retried(self) -> None:
        for stderr, status in (
            ("gh: Not Found (HTTP 404)\n", 404),
            ("gh: Reference already exists (HTTP 422)", 422),
            ("gh: Reference does not exist (HTTP 422)", 422),
            ("gh: Resource not accessible by integration (HTTP 403)", 403),
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(classify(stderr)[0], status)
                self.assertFalse(transient(*classify(stderr)))

    def test_busy_and_unreachable_are_retried(self) -> None:
        for stderr in (
            "gh: Server Error (HTTP 502)",
            "gh: API rate limit exceeded (HTTP 429)",
            (
                "gh: You have exceeded a secondary rate limit. Please wait a few"
                " minutes before you try again. (HTTP 403)"
            ),
            "error connecting to api.github.com\ncheck your internet connection",
        ):
            with self.subTest(stderr=stderr):
                self.assertTrue(transient(*classify(stderr)))


class LedgerPathTest(unittest.TestCase):
    SLOT = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)

    def test_workflow_file_comes_from_the_workflow_ref(self) -> None:
        self.assertEqual(
            workflow_file_from_ref(
                "owner/name/.github/workflows/d1-backup.yml@refs/heads/main"
            ),
            "d1-backup.yml",
        )

    def test_record_path_carries_the_line_and_a_part_suffix(self) -> None:
        daily = parse_cron(DAILY)
        self.assertEqual(
            ref_path("backup.yml", daily, self.SLOT),
            "github-cron-trigger/backup.yml/0_1_x_x_x/20260928T0100Z",
        )
        # A part is a suffix on the leaf: git refuses a ref beneath an existing
        # one, so a child path would collide with the slot's own record.
        self.assertEqual(
            ref_path("audit.yml", daily, self.SLOT, "production"),
            "github-cron-trigger/audit.yml/0_1_x_x_x/20260928T0100Z.production",
        )
        self.assertEqual(
            record_slot_key("0_1_x_x_x/20260928T0100Z.production"), "20260928T0100Z"
        )
        self.assertIsNone(record_slot_key("20260928T0100Z"))
        self.assertIsNone(record_slot_key("not-a-record"))

    def test_lines_firing_in_the_same_minute_get_separate_records(self) -> None:
        # A daily 01:00 line and a Monday 01:00 line fire together on a Monday at
        # 01:00. Were the record keyed on the instant alone, the first run to
        # finish would make the other line's run skip its own work.
        daily = ref_path("wf.yml", parse_cron("0 1 * * *"), self.SLOT)
        monday = ref_path("wf.yml", parse_cron("0 1 * * 1"), self.SLOT)
        self.assertNotEqual(daily, monday)

    def test_one_schedule_written_differently_is_one_record(self) -> None:
        # The clock reads the line from the workflow file and GitHub echoes it
        # back verbatim, so every delivery must reduce any written form of it to
        # the same key.
        self.assertEqual(
            cron_segment(parse_cron("30,0  1 * * *")),
            cron_segment(parse_cron("0,30 1 * * *")),
        )
        every_hour = ",".join(str(h) for h in range(24))
        self.assertEqual(
            cron_segment(parse_cron(f"5 {every_hour} * * *")),
            cron_segment(parse_cron("5 * * * *")),
        )

    def test_names_that_would_leave_the_namespace_are_refused(self) -> None:
        daily = parse_cron(DAILY)
        for workflow_file in (
            "../heads/main.yml",
            "a/b.yml",
            ".hidden.yml",
            "noext",
            "",
        ):
            with (
                self.subTest(workflow_file=workflow_file),
                self.assertRaises(ValueError),
            ):
                ref_path(workflow_file, daily, self.SLOT)
        for part in ("a/b", "..", "a.b", "-x"):
            with self.subTest(part=part), self.assertRaises(ValueError):
                ref_path("audit.yml", daily, self.SLOT, part)


if __name__ == "__main__":
    unittest.main()
